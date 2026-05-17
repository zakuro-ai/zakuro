//! Frozen byte-vector snapshots.
//!
//! The encoded bytes of canonical [`Envelope`] / [`ExecutionResult`]
//! values are committed here as hex strings. A schema change that breaks
//! wire compatibility — adding a non-default field, reordering fields,
//! changing the discriminant of a `#[derive(Serialize)]` enum — will
//! flip these checks red **before** a release that would silently break
//! interop between zakuro and zc.
//!
//! To **intentionally** rebless a snapshot:
//!   1. confirm the change is wire-compatible (`#[serde(default)]` for
//!      new fields; append-only enum variants).
//!   2. run `cargo test -p zakuro-wire -- --nocapture frozen_envelope_bytes`,
//!      grab the new hex from the panic message, and overwrite the
//!      constant below.
//!   3. bump the crate to the appropriate version per semver.

use zakuro_wire::{Envelope, ErrorKind, ExecutionResult, ResourceLimits, Status, WireVersion};

fn canonical_envelope() -> Envelope {
    Envelope {
        version: WireVersion::V1,
        job_id: "canonical-job".into(),
        tenant_id: "canonical-tenant".into(),
        callable: vec![0xde, 0xad, 0xbe, 0xef],
        args: vec![0x00, 0x01, 0x02],
        hmac: [0xAA; 32],
        resource_limits: ResourceLimits {
            cpus: 1.0,
            memory_mb: 1024,
            gpus: 0,
            timeout_seconds: 30,
        },
    }
}

fn canonical_result_ok() -> ExecutionResult {
    ExecutionResult {
        version: WireVersion::V1,
        job_id: "canonical-job".into(),
        status: Status::Succeeded,
        exit_code: Some(0),
        duration_ms: 100,
        stdout: "ok".into(),
        stderr: String::new(),
        error: None,
    }
}

fn canonical_result_err() -> ExecutionResult {
    ExecutionResult {
        version: WireVersion::V1,
        job_id: "canonical-job".into(),
        status: Status::Failed,
        exit_code: Some(127),
        duration_ms: 5,
        stdout: String::new(),
        stderr: "no such file".into(),
        error: Some(ErrorKind::NonZeroExit { code: 127 }),
    }
}

/// Round-trip is the cheap safety net; if this regresses, postcard or
/// serde was bumped to an incompatible major.
#[test]
fn envelope_roundtrip_canonical() {
    let env = canonical_envelope();
    let bytes = postcard::to_allocvec(&env).expect("encode");
    let back: Envelope = postcard::from_bytes(&bytes).expect("decode");
    assert_eq!(env, back);
}

#[test]
fn result_ok_roundtrip_canonical() {
    let res = canonical_result_ok();
    let bytes = postcard::to_allocvec(&res).expect("encode");
    let back: ExecutionResult = postcard::from_bytes(&bytes).expect("decode");
    assert_eq!(res, back);
}

#[test]
fn result_err_roundtrip_canonical() {
    let res = canonical_result_err();
    let bytes = postcard::to_allocvec(&res).expect("encode");
    let back: ExecutionResult = postcard::from_bytes(&bytes).expect("decode");
    assert_eq!(res, back);
}

/// **Frozen-bytes snapshot for the canonical envelope.**
///
/// When this regresses the schema has changed in a way that breaks
/// existing peers. Either the change is intentional (see top-of-file
/// rebless instructions + crate version bump) or it's an accidental
/// re-ordering of fields / enum variants that needs reverting.
const FROZEN_ENVELOPE_HEX: &str = "\
00\
0d63616e6f6e6963616c2d6a6f62\
1063616e6f6e6963616c2d74656e616e74\
04deadbeef\
03000102\
aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa\
0000803f\
8008\
00\
1e";

#[test]
fn frozen_envelope_bytes() {
    let env = canonical_envelope();
    let actual = hex::encode(postcard::to_allocvec(&env).expect("encode"));
    assert_eq!(
        actual, FROZEN_ENVELOPE_HEX,
        "envelope wire format drifted. If intentional, see tests/snapshots.rs for the rebless flow."
    );
}
