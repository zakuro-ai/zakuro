//! Shared wire format for the Zakuro distributed-ML runtime.
//!
//! This crate is the **single source of truth** for the postcard-encoded
//! types that travel between:
//!
//! * the **Zakuro worker** (Python, this repo), which runs `@zk.fn`-decorated
//!   callables and emits results;
//! * the **zc broker** (Rust, <https://github.com/zakuro-ai/zc>), which
//!   routes dispatches between clients and workers and meters credits.
//!
//! Putting the schema in its own crate (rather than duplicating the struct
//! definitions in each repo) makes the wire contract mechanically
//! enforceable: both consumers depend on the same crate version via
//! Cargo's resolver, and any schema-breaking change forces a coordinated
//! version bump. See [zakuro RFC 0001](https://github.com/zakuro-ai/zakuro/blob/master/docs/rfcs/0001-wire-format-postcard.md)
//! for the architecture decision behind this layout.
//!
//! # What's covered (v0.1)
//!
//! * [`WireVersion`] — top-level enum that every dispatch payload carries
//!   so consumers can detect a mismatched peer cleanly.
//! * [`Envelope`] — dispatch payload: caller-supplied callable body +
//!   args + signed metadata.
//! * [`Result`] — worker-side reply payload.
//! * [`ErrorKind`] — structured error variants both sides understand.
//!
//! # What's intentionally not in v0.1
//!
//! * Gossip messages (RFC 0008) — those live in the broker today. Will be
//!   added in 0.2 once the broker-side implementation is ready to consume
//!   them from a shared definition.
//! * The HMAC verification helper itself — the schema carries the HMAC
//!   bytes; the *verification* path lives in `zakuro.wire.safe_loads`
//!   (Python) and the equivalent Rust side in `zc`. Keeping the crate
//!   schema-only avoids tying it to a specific HMAC implementation.
//!
//! # Wire format invariants
//!
//! Every value that crosses the wire is encoded with `postcard`'s standard
//! `to_allocvec` / `from_bytes` (default-feature config). The schema is
//! **append-only** within a major version: new fields are added as
//! `#[serde(default)]`, and old peers must accept payloads with unknown
//! trailing fields. A truly breaking change requires a major version bump
//! of this crate **and** a new ALPN string on the QUIC transport so
//! peers don't accidentally talk past each other.
//!
//! # Quick start
//!
//! ```
//! use zakuro_wire::{Envelope, ResourceLimits, WireVersion};
//!
//! let env = Envelope {
//!     version: WireVersion::V1,
//!     job_id: "j-1".into(),
//!     tenant_id: "tenant-acme".into(),
//!     callable: vec![0xde, 0xad, 0xbe, 0xef],   // cloudpickle body, signed
//!     args: vec![],
//!     hmac: [0u8; 32],
//!     resource_limits: ResourceLimits { cpus: 1.0, memory_mb: 1024, gpus: 0, timeout_seconds: 60 },
//! };
//!
//! let bytes = postcard::to_allocvec(&env).unwrap();
//! let back: Envelope = postcard::from_bytes(&bytes).unwrap();
//! assert_eq!(env.job_id, back.job_id);
//! ```

#![cfg_attr(not(test), no_std)]

extern crate alloc;

use alloc::string::String;
use alloc::vec::Vec;
use serde::{Deserialize, Serialize};

/// Top-level version tag carried by every dispatch payload.
///
/// A worker that receives a payload with an unrecognised variant rejects
/// it with [`ErrorKind::UnknownWireVersion`] before deserialising further.
/// New variants are added in a new minor release; removing a variant
/// requires a major version bump.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum WireVersion {
    /// Initial wire format, shipped with zakuro 0.4 / zc 0.1.
    V1,
}

/// Per-job resource budget enforced by the worker container.
///
/// `gpus = 0` means CPU-only (the worker pool advertises `gpus_total`
/// separately; this is the *requested* allocation, not the worker's
/// capacity). `timeout_seconds` is the wall-clock deadline applied by
/// the worker; if it's exceeded, the worker returns a [`Result`] with
/// [`ErrorKind::TimedOut`] and tears down the executing container.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ResourceLimits {
    /// Fractional CPU cores requested. `1.5` means "one and a half
    /// cores". Mapped to Docker's `--cpus` at runtime.
    pub cpus: f32,
    /// Memory in megabytes. Mapped to Docker's `--memory <N>m`.
    pub memory_mb: u32,
    /// Number of GPUs requested. Single-tenant per worker per zakuro
    /// RFC 0007, so this is bounded by the worker's `/info.gpus_total`.
    pub gpus: u32,
    /// Wall-clock deadline in seconds. Must be `> 0`.
    pub timeout_seconds: u32,
}

/// Dispatch envelope: the payload a client sends through the broker to
/// the worker that will execute the job.
///
/// The `callable` and `args` byte blobs are opaque to the broker: it
/// only routes by `tenant_id` + `job_id` and meters credits by the
/// resulting [`Result.duration_ms`]. The worker is responsible for
/// verifying `hmac` before deserialising `callable` — see the comment
/// on the `hmac` field.
///
/// The struct is `Serialize + Deserialize` only; the encryption /
/// signing surface is intentionally not in this crate (it depends on
/// per-tenant key material that lives in the broker).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Envelope {
    /// Wire version. Always populated; non-default for future-compat.
    pub version: WireVersion,
    /// Caller-supplied, monotonically-allocated job identifier. The
    /// broker uses this for idempotency on retried dispatches.
    pub job_id: String,
    /// Tenant identifier matching the `tenant_id` claim of the JWT
    /// used to authenticate the dispatch (see zakuro RFC 0002).
    pub tenant_id: String,
    /// Cloudpickle-serialised callable body. The worker treats this
    /// as opaque bytes *until* the [`Envelope::hmac`] verification
    /// succeeds against the per-tenant key.
    #[serde(with = "serde_bytes")]
    pub callable: Vec<u8>,
    /// Cloudpickle-serialised positional + keyword arguments. Same
    /// treatment as `callable`.
    #[serde(with = "serde_bytes")]
    pub args: Vec<u8>,
    /// HMAC-SHA-256 over `callable || args || job_id`, computed with
    /// the per-tenant key derived from the JWT signing key. The
    /// worker rejects the envelope with [`ErrorKind::HmacMismatch`]
    /// if verification fails — that's the single gate keeping a
    /// crafted byte stream from triggering `cloudpickle.loads()`.
    pub hmac: [u8; 32],
    /// Resource budget the worker must enforce while the job runs.
    pub resource_limits: ResourceLimits,
}

/// Status of a completed job, as seen by the worker that ran it.
///
/// Stable across minor versions; new variants are appended.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
pub enum Status {
    /// Process exited with code 0 in time.
    Succeeded,
    /// Process exited with a non-zero code in time.
    Failed,
    /// `resource_limits.timeout_seconds` elapsed; the worker killed
    /// the container.
    TimedOut,
    /// Worker dropped the envelope before invoking the callable
    /// (HMAC verification failed, lease expired, etc.).
    Rejected,
}

/// Worker-side response to a dispatch.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct ExecutionResult {
    /// Wire version. Matches the [`Envelope::version`] it replies to.
    pub version: WireVersion,
    /// Echoed from [`Envelope::job_id`] so callers can correlate.
    pub job_id: String,
    /// Worker-emitted status.
    pub status: Status,
    /// Process exit code, when applicable (None for `Rejected` /
    /// `TimedOut`).
    pub exit_code: Option<i32>,
    /// Wall-clock duration between worker accepting the dispatch and
    /// emitting this struct.
    pub duration_ms: u64,
    /// Captured stdout, truncated by the worker if it crossed the
    /// configured byte budget.
    pub stdout: String,
    /// Captured stderr, same truncation rule as stdout.
    pub stderr: String,
    /// Structured failure information for `Failed` / `Rejected` /
    /// `TimedOut` statuses. `None` on `Succeeded`.
    pub error: Option<ErrorKind>,
}

/// Structured error a worker can attach to an [`ExecutionResult`].
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub enum ErrorKind {
    /// The HMAC over `callable || args || job_id` didn't match the
    /// expected per-tenant key.
    HmacMismatch,
    /// `WireVersion` not understood by this worker.
    UnknownWireVersion,
    /// `resource_limits.timeout_seconds` elapsed.
    TimedOut,
    /// Cloudpickle deserialisation raised an exception inside the
    /// callable body. The message field carries the exception text,
    /// already PII-scrubbed by the worker's structlog processor.
    DeserialisationError {
        /// Human-readable description of the failure.
        message: String,
    },
    /// Process exited non-zero.
    NonZeroExit {
        /// Captured exit code.
        code: i32,
    },
    /// Worker is no longer accepting jobs (lease expired, draining,
    /// etc.). Caller should re-dispatch through the broker.
    WorkerUnavailable {
        /// Reason string for telemetry.
        reason: String,
    },
}

#[cfg(test)]
mod tests {
    use super::*;
    extern crate std;
    use std::vec;

    #[test]
    fn envelope_roundtrip() {
        let env = Envelope {
            version: WireVersion::V1,
            job_id: "j-001".into(),
            tenant_id: "tenant-acme".into(),
            callable: vec![0xde, 0xad, 0xbe, 0xef],
            args: vec![0x00, 0x01, 0x02],
            hmac: [0u8; 32],
            resource_limits: ResourceLimits {
                cpus: 1.5,
                memory_mb: 1024,
                gpus: 0,
                timeout_seconds: 60,
            },
        };
        let bytes = postcard::to_allocvec(&env).expect("encode");
        let back: Envelope = postcard::from_bytes(&bytes).expect("decode");
        assert_eq!(env, back);
    }

    #[test]
    fn result_roundtrip_succeeded() {
        let res = ExecutionResult {
            version: WireVersion::V1,
            job_id: "j-001".into(),
            status: Status::Succeeded,
            exit_code: Some(0),
            duration_ms: 142,
            stdout: "hello\n".into(),
            stderr: String::new(),
            error: None,
        };
        let bytes = postcard::to_allocvec(&res).expect("encode");
        let back: ExecutionResult = postcard::from_bytes(&bytes).expect("decode");
        assert_eq!(res, back);
    }

    #[test]
    fn result_roundtrip_with_error() {
        let res = ExecutionResult {
            version: WireVersion::V1,
            job_id: "j-002".into(),
            status: Status::Failed,
            exit_code: Some(127),
            duration_ms: 5,
            stdout: String::new(),
            stderr: "command not found\n".into(),
            error: Some(ErrorKind::NonZeroExit { code: 127 }),
        };
        let bytes = postcard::to_allocvec(&res).expect("encode");
        let back: ExecutionResult = postcard::from_bytes(&bytes).expect("decode");
        assert_eq!(res, back);
    }
}
