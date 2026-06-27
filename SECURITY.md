# Security policy

Thank you for helping keep Zakuro and its users safe.

## Supported versions

Zakuro is pre-1.0. Only the **current minor release on `master`** receives security fixes. Once 1.0 ships, the project will adopt a 24-month LTS window for the latest minor (see [`docs/STABILITY.md`](docs/STABILITY.md)).

| Version | Supported |
|---|---|
| `master` (latest minor) | ✅ |
| Older 0.x | ❌ — please upgrade |

## Reporting a vulnerability

**Do not file a public issue.** Email **security@zakuro.ai** with the details listed below. We reply within **2 business days** to acknowledge the report and within **10 business days** with a triage classification (accept / mitigate / decline) and a tentative fix window.

For especially sensitive reports, request our GPG public key in your initial message and we will send it before you transmit any proof-of-concept.

Include in your report:

- A description of the issue and the attacker capability it gives.
- A minimal reproduction (command, code snippet, or HTTP request).
- The affected version (`zakuro.__version__`), Python version, and platform.
- Whether the issue is already public anywhere (mailing list, blog, CVE database).
- Optional: a suggested fix or mitigation.

## What counts as a vulnerability

In scope:

- Remote code execution, sandbox escape, or privilege escalation in the worker or broker.
- Authentication or authorization bypass — anything that lets an unauthenticated caller invoke a worker, or lets one tenant act as another.
- Cryptographic weaknesses (TLS / JWT / signing) in the published artifacts.
- Supply-chain integrity issues — tampered wheels, tampered images, signature-verification bypasses against the published cosign/SLSA attestations.
- Sensitive-data exposure (credentials, tokens, request bodies) in logs, errors, or telemetry.
- DoS amplification specific to Zakuro (a worker can be made to consume unbounded resources from a single small request).

Out of scope:

- Vulnerabilities in dependencies that have a public CVE and a published fix — please file a regular issue or open a PR to bump the pin.
- Issues that require pre-existing root / admin on the worker host.
- Social-engineering or physical-access attacks.
- DoS against `zakuro-ai.com` infrastructure — report those directly to operations.
- Findings against a fork or a release older than the current minor.

## Worker network exposure (fail-closed bind)

A Zakuro worker's `/execute` endpoint deserialises cloudpickle. Treat any
worker reachable off a trusted, network-isolated mesh as a remote-code-execution
surface unless a caller-authentication control is enabled. From v0.2.24 the
worker **refuses to bind a non-loopback address** when none of these is on:

| Control | Enable with |
| --- | --- |
| JWT auth (RFC 0002) | `ZAKURO_AUTH_REQUIRED=1` |
| HMAC-signed wire (RFC 0001) | `ZAKURO_WIRE=v1` + `ZAKURO_HMAC_KEY` or `ZAKURO_HMAC_KEY_FILE` |
| Mutual TLS | `ZAKURO_CERT_DIR=<dir>` |

Escape hatch for a trusted, isolated mesh: `ZAKURO_INSECURE_BIND=1` (logs a
startup WARNING). Loopback binds (`--host 127.0.0.1`) are always allowed.

Other hardening env vars:

- `ZAKURO_MAX_PAYLOAD_BYTES` — inbound `/execute` body cap (default 256 MiB).

> **Roadmap (v0.4, cross-repo):** `ZAKURO_WIRE=v1` and `ZAKURO_AUTH_REQUIRED=1`
> will become the *defaults* once the SDK client wraps signed envelopes and the
> `zc` broker rollout is coordinated. Until then they are opt-in and this
> fail-closed bind guard is the safety net.

## Disclosure policy

We follow **coordinated disclosure**.

1. We acknowledge the report.
2. We confirm or decline the vulnerability and agree a fix window with you. Typical windows: 30 days (Critical), 60 days (High), 90 days (Moderate).
3. We prepare a patched release, a CVE (where applicable), and a credit line.
4. We coordinate a disclosure date with you. Default is **30 days after a fix ships**, or sooner if the issue is already public.
5. We publish a GitHub Security Advisory and add the CVE to [`docs/security/advisories.md`](docs/security/advisories.md).

If we miss our triage SLA you are free to disclose publicly without further notice; in that case please give us a heads-up email so we can prepare.

## Verifying releases

Every wheel and container image is signed (Cosign keyless) and accompanied by a SLSA L3 provenance attestation. The verification one-liners live at [`docs/security/verifying-releases.md`](docs/security/verifying-releases.md).

## Hall of fame

Security researchers who have responsibly disclosed issues to us are credited in [`docs/security/hall-of-fame.md`](docs/security/hall-of-fame.md) (with their permission). We do not run a paid bug-bounty program at this time.
