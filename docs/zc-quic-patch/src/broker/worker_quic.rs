//! QUIC worker caller — broker-side counterpart to the Python
//! `zakuro/worker/quic_server.py`.
//!
//! Mirrors the byte-for-byte semantics of the HTTP `/execute` path:
//! the body is an opaque cloudpickle blob going in, and the response is
//! an opaque cloudpickle blob coming out. The only thing that changes
//! from the existing `forward_to_worker` is the transport.
//!
//! See `docs/PROTOCOL.md` in the `zakuro` repo for the authoritative
//! wire spec.

use std::collections::HashMap;
use std::net::SocketAddr;
use std::sync::Arc;
use std::time::Duration;

use dashmap::DashMap;
use quinn::{ClientConfig, Connection, Endpoint, VarInt};
use tokio::sync::Mutex;

// ---------------------------------------------------------------------------
// Protocol constants — must match docs/PROTOCOL.md
// ---------------------------------------------------------------------------

const OP_EXECUTE: u8 = 1;
const OP_INFO: u8 = 2;
const OP_HEALTH: u8 = 3;

const STAT_OK: u8 = 0;
const STAT_USER_ERROR: u8 = 1;
const STAT_PROTOCOL_ERROR: u8 = 2;

const ALPN: &[u8] = b"zk-worker";
const DEFAULT_PORT: u16 = 4433;

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

#[derive(Debug, thiserror::Error)]
pub enum WorkerQuicError {
    #[error("bad uri: {0}")]
    BadUri(String),
    #[error("dns: {0}")]
    Dns(String),
    #[error("connect: {0}")]
    Connect(String),
    #[error("stream: {0}")]
    Stream(String),
    #[error("malformed frame: {0}")]
    Frame(String),
    #[error("protocol error from worker: {0}")]
    Protocol(String),
    #[error("user exception from worker (cloudpickle bytes)")]
    UserException(Vec<u8>),
    #[error("timeout after {0}s")]
    Timeout(f64),
}

// ---------------------------------------------------------------------------
// TLS — skip verification (self-signed worker certs, identity is out-of-band)
// ---------------------------------------------------------------------------

struct SkipVerify;

impl rustls::client::ServerCertVerifier for SkipVerify {
    fn verify_server_cert(
        &self,
        _: &rustls::Certificate,
        _: &[rustls::Certificate],
        _: &rustls::ServerName,
        _: &mut dyn Iterator<Item = &[u8]>,
        _: &[u8],
        _: std::time::SystemTime,
    ) -> Result<rustls::client::ServerCertVerified, rustls::Error> {
        Ok(rustls::client::ServerCertVerified::assertion())
    }
}

fn make_client_config() -> ClientConfig {
    let mut cc = rustls::ClientConfig::builder()
        .with_safe_defaults()
        .with_custom_certificate_verifier(Arc::new(SkipVerify))
        .with_no_client_auth();
    cc.alpn_protocols = vec![ALPN.to_vec()];
    ClientConfig::new(Arc::new(cc))
}

// ---------------------------------------------------------------------------
// Connection pool
// ---------------------------------------------------------------------------

/// One QUIC connection per `host:port`, cached for the lifetime of the broker.
#[derive(Clone, Default)]
pub struct WorkerQuicPool {
    conns: Arc<DashMap<String, Arc<Mutex<Option<Connection>>>>>,
    endpoint: Arc<Mutex<Option<Endpoint>>>,
}

impl WorkerQuicPool {
    pub fn new() -> Self {
        Self::default()
    }

    async fn endpoint(&self) -> Result<Endpoint, WorkerQuicError> {
        let mut slot = self.endpoint.lock().await;
        if let Some(ep) = slot.as_ref() {
            return Ok(ep.clone());
        }
        let addr: SocketAddr = "0.0.0.0:0"
            .parse()
            .map_err(|e: std::net::AddrParseError| WorkerQuicError::Connect(e.to_string()))?;
        let mut endpoint = Endpoint::client(addr)
            .map_err(|e| WorkerQuicError::Connect(e.to_string()))?;
        endpoint.set_default_client_config(make_client_config());
        *slot = Some(endpoint.clone());
        Ok(endpoint)
    }

    async fn get(&self, host: &str, port: u16) -> Result<Connection, WorkerQuicError> {
        let key = format!("{}:{}", host, port);
        let slot = self
            .conns
            .entry(key.clone())
            .or_insert_with(|| Arc::new(Mutex::new(None)))
            .clone();

        let mut guard = slot.lock().await;
        if let Some(conn) = guard.as_ref() {
            if conn.close_reason().is_none() {
                return Ok(conn.clone());
            }
        }

        let endpoint = self.endpoint().await?;
        let addr = tokio::net::lookup_host((host, port))
            .await
            .map_err(|e| WorkerQuicError::Dns(e.to_string()))?
            .next()
            .ok_or_else(|| WorkerQuicError::Dns(format!("no addr for {}", host)))?;

        let connecting = endpoint
            .connect(addr, "localhost")
            .map_err(|e| WorkerQuicError::Connect(e.to_string()))?;
        let connection = connecting
            .await
            .map_err(|e| WorkerQuicError::Connect(e.to_string()))?;

        *guard = Some(connection.clone());
        Ok(connection)
    }

    async fn invalidate(&self, host: &str, port: u16) {
        let key = format!("{}:{}", host, port);
        if let Some((_, slot)) = self.conns.remove(&key) {
            let mut guard = slot.lock().await;
            if let Some(conn) = guard.take() {
                conn.close(VarInt::from_u32(0), b"stale");
            }
        }
    }
}

// ---------------------------------------------------------------------------
// URI parsing
// ---------------------------------------------------------------------------

fn parse_uri(uri: &str) -> Result<(String, u16), WorkerQuicError> {
    let stripped = uri
        .strip_prefix("quic://")
        .ok_or_else(|| WorkerQuicError::BadUri(format!("expected quic://, got {}", uri)))?;
    let (host, port) = match stripped.rsplit_once(':') {
        Some((h, p)) => {
            let port: u16 = p
                .parse()
                .map_err(|_| WorkerQuicError::BadUri(format!("bad port in {}", uri)))?;
            (h.to_string(), port)
        }
        None => (stripped.to_string(), DEFAULT_PORT),
    };
    Ok((host, port))
}

// ---------------------------------------------------------------------------
// Framing helpers
// ---------------------------------------------------------------------------

async fn write_frame(
    send: &mut quinn::SendStream,
    op: u8,
    payload: &[u8],
) -> Result<(), WorkerQuicError> {
    let len = payload.len() as u32;
    let mut header = [0u8; 5];
    header[0] = op;
    header[1..5].copy_from_slice(&len.to_be_bytes());
    send.write_all(&header)
        .await
        .map_err(|e| WorkerQuicError::Stream(e.to_string()))?;
    send.write_all(payload)
        .await
        .map_err(|e| WorkerQuicError::Stream(e.to_string()))?;
    send.finish()
        .await
        .map_err(|e| WorkerQuicError::Stream(e.to_string()))?;
    Ok(())
}

async fn read_frame(
    recv: &mut quinn::RecvStream,
) -> Result<(u8, Vec<u8>), WorkerQuicError> {
    let mut header = [0u8; 5];
    recv.read_exact(&mut header)
        .await
        .map_err(|e| WorkerQuicError::Frame(format!("header: {}", e)))?;
    let status = header[0];
    let len = u32::from_be_bytes([header[1], header[2], header[3], header[4]]) as usize;
    let mut body = vec![0u8; len];
    if len > 0 {
        recv.read_exact(&mut body)
            .await
            .map_err(|e| WorkerQuicError::Frame(format!("body: {}", e)))?;
    }
    Ok((status, body))
}

// ---------------------------------------------------------------------------
// Public API — parallels server.rs::forward_to_worker
// ---------------------------------------------------------------------------

/// Forward an opaque cloudpickle EXECUTE body to a QUIC worker.
///
/// Drop-in replacement for `forward_to_worker` in `src/broker/server.rs` when
/// the worker URI begins with `quic://`.
pub async fn forward(
    pool: &WorkerQuicPool,
    worker_uri: &str,
    body: &[u8],
    _request_id: &str,
    effective_timeout_secs: f64,
) -> Result<Vec<u8>, WorkerQuicError> {
    let (host, port) = parse_uri(worker_uri)?;
    let work = async {
        let conn = pool.get(&host, port).await?;
        let (mut send, mut recv) = match conn.open_bi().await {
            Ok(s) => s,
            Err(e) => {
                pool.invalidate(&host, port).await;
                return Err(WorkerQuicError::Stream(e.to_string()));
            }
        };
        write_frame(&mut send, OP_EXECUTE, body).await?;
        let (status, payload) = read_frame(&mut recv).await?;
        match status {
            STAT_OK => Ok(payload),
            STAT_USER_ERROR => Err(WorkerQuicError::UserException(payload)),
            STAT_PROTOCOL_ERROR => Err(WorkerQuicError::Protocol(
                String::from_utf8_lossy(&payload).into_owned(),
            )),
            other => Err(WorkerQuicError::Frame(format!(
                "unknown status byte {}",
                other
            ))),
        }
    };
    if effective_timeout_secs > 0.0 {
        match tokio::time::timeout(
            Duration::from_secs_f64(effective_timeout_secs + 5.0),
            work,
        )
        .await
        {
            Ok(r) => r,
            Err(_) => Err(WorkerQuicError::Timeout(effective_timeout_secs)),
        }
    } else {
        work.await
    }
}

/// Issue a HEALTH probe; returns `true` iff the worker answered `STAT_OK`.
pub async fn health(pool: &WorkerQuicPool, worker_uri: &str) -> bool {
    async fn probe(
        pool: &WorkerQuicPool,
        worker_uri: &str,
    ) -> Result<(), WorkerQuicError> {
        let (host, port) = parse_uri(worker_uri)?;
        let conn = pool.get(&host, port).await?;
        let (mut send, mut recv) = conn
            .open_bi()
            .await
            .map_err(|e| WorkerQuicError::Stream(e.to_string()))?;
        write_frame(&mut send, OP_HEALTH, &[]).await?;
        let (status, _) = read_frame(&mut recv).await?;
        if status == STAT_OK {
            Ok(())
        } else {
            Err(WorkerQuicError::Protocol(format!(
                "health status = {}",
                status
            )))
        }
    }
    tokio::time::timeout(Duration::from_secs(2), probe(pool, worker_uri))
        .await
        .ok()
        .and_then(|r| r.ok())
        .is_some()
}

/// Fetch the `/info`-equivalent JSON from a QUIC worker.
pub async fn info(
    pool: &WorkerQuicPool,
    worker_uri: &str,
) -> Result<String, WorkerQuicError> {
    let (host, port) = parse_uri(worker_uri)?;
    let conn = pool.get(&host, port).await?;
    let (mut send, mut recv) = conn
        .open_bi()
        .await
        .map_err(|e| WorkerQuicError::Stream(e.to_string()))?;
    write_frame(&mut send, OP_INFO, &[]).await?;
    let (status, payload) = read_frame(&mut recv).await?;
    if status != STAT_OK {
        return Err(WorkerQuicError::Protocol(
            String::from_utf8_lossy(&payload).into_owned(),
        ));
    }
    String::from_utf8(payload)
        .map_err(|e| WorkerQuicError::Frame(format!("info utf8: {}", e)))
}

// ---------------------------------------------------------------------------
// Unit tests — rely on a running Python worker. Skipped unless enabled.
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    //! To run: `ZAKURO_QUIC_TEST_URI=quic://127.0.0.1:4433 cargo test worker_quic`
    //! with a Python worker started via `zakuro-worker --transport quic`.

    use super::*;

    fn uri() -> Option<String> {
        std::env::var("ZAKURO_QUIC_TEST_URI").ok()
    }

    #[tokio::test]
    async fn health_roundtrip() {
        let Some(u) = uri() else { return };
        let pool = WorkerQuicPool::new();
        assert!(health(&pool, &u).await);
    }

    #[tokio::test]
    async fn info_roundtrip() {
        let Some(u) = uri() else { return };
        let pool = WorkerQuicPool::new();
        let json = info(&pool, &u).await.expect("info");
        assert!(json.contains("\"transport\":\"quic\""));
    }
}
