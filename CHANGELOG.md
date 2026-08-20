# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

> **Version reconciliation (see [#233]).** The only published GitHub Release is
> `v0.2.0`. Versions `0.2.1`–`0.2.23` were stamped into `zakuro/__init__.py` by
> the `chore(release): … [skip release]` bot commits but were never published as
> GitHub Releases. The **[0.2.23]** entry below consolidates everything
> developed across that range. Going forward, the single source of truth for the
> version is `__version__` in `zakuro/__init__.py`, and each published release
> gets its own section here.

## [Unreleased]

### Added
- BSD-3-Clause `LICENSE` file ([#236]).
- Repository governance: `CODEOWNERS`, issue/PR templates, `SECURITY.md`, `CONTRIBUTING.md` ([#223]).

### Changed
- Public-repo CI now runs on GitHub-hosted runners; the benchmark job is manual-only ([#214], [#215], [#219], [#220]).
- All GitHub Actions are pinned to commit SHAs ([#237]).

### Fixed
- `pip-audit` CVE exit codes are surfaced instead of being swallowed ([#238]).
- Removed broken notebook links from the README ([#239]).
- Added the missing `ci-local` task and clarified the contributor docs ([#240]).
- Repaired the PR pipeline (uv/go-task setup, deterministic NOTICE, SOPS check) ([#217], [#221]).

## [0.2.23] - 2026-05-17

Consolidated development line since `v0.2.0` (supersedes the unpublished
`0.2.1`–`0.2.22` source bumps).

### Added
- **Adaptive compute** — Adam-style context-aware worker allocator ([#93]); node lifecycle, warmup, and mesh-adaptation bench ([#95]); health-aware dispatch with a background probe thread ([#96]); performance-drift detection with soft demotion ([#97]); bandwidth- and price-aware routing plus public API ([#104]); region/rack-aware topology hints ([#108]); per-dispatch allocation decision log with a non-blocking writer ([#176]).
- **Transports** — QUIC transport, `zk.Worker`, and in-process standalone mode ([#92]); QUIC retry-on-failure with a 5 s idle timeout ([#100]).
- **Wire protocol** — `zakuro-wire` v0.2 `EnvelopeV2` + `ChunkFrame` substrate and streaming reassembler ([#174], [#175]); Python postcard + HMAC codec ([#117]); `safe_loads` on `/execute` and the QUIC handler ([#117]); `PayloadCache` with `bsdiff4` delta-apply ([#174]).
- **Auth & transport security** — Ed25519 JWT verification with FastAPI scope middleware, wired onto `/execute` and `/info` ([#116]); mTLS substrate (cert loading, peer identity) wired into uvicorn and aioquic ([#115]); SOPS + age in-tree secret encryption ([#118]).
- **Observability** — structured JSON logging via structlog ([#125]); Prometheus `/metrics` endpoint and collectors ([#124]); OpenTelemetry tracing via OTLP/HTTP ([#123]); Sentry integration for worker and client ([#128]); SLOs, burn-rate alerts, and a Grafana overview ([#127], [#129]).
- **Worker & ops** — `/live` and `/ready` probes, continuous-bench CI, the stability policy, and mkdocs docs site ([#151]); hardware-capability reporting on `/info`; CLI args and a module entry point for programmatic launch; full platform image on the compute base with an s6 worker service; `linux/arm64` multi-arch images; two-node Tailscale isolation for dev meshes.

### Changed
- Cross-repo coherence mechanisms locking `zakuro` ↔ `zc` state ([#166]).

## [0.2.0] - 2026-02-15

Distributed compute with broker integration — the first published release on the
0.2 line. See the [v0.2.0 GitHub Release](https://github.com/zakuro-ai/zakuro/releases/tag/v0.2.0).

[Unreleased]: https://github.com/zakuro-ai/zakuro/compare/v0.2.0...HEAD
[0.2.23]: https://github.com/zakuro-ai/zakuro/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/zakuro-ai/zakuro/releases/tag/v0.2.0

[#233]: https://github.com/zakuro-ai/zakuro/issues/233
[#92]: https://github.com/zakuro-ai/zakuro/pull/92
[#93]: https://github.com/zakuro-ai/zakuro/pull/93
[#95]: https://github.com/zakuro-ai/zakuro/pull/95
[#96]: https://github.com/zakuro-ai/zakuro/pull/96
[#97]: https://github.com/zakuro-ai/zakuro/pull/97
[#100]: https://github.com/zakuro-ai/zakuro/pull/100
[#104]: https://github.com/zakuro-ai/zakuro/pull/104
[#108]: https://github.com/zakuro-ai/zakuro/pull/108
[#115]: https://github.com/zakuro-ai/zakuro/issues/115
[#116]: https://github.com/zakuro-ai/zakuro/issues/116
[#117]: https://github.com/zakuro-ai/zakuro/issues/117
[#118]: https://github.com/zakuro-ai/zakuro/issues/118
[#123]: https://github.com/zakuro-ai/zakuro/issues/123
[#124]: https://github.com/zakuro-ai/zakuro/issues/124
[#125]: https://github.com/zakuro-ai/zakuro/issues/125
[#127]: https://github.com/zakuro-ai/zakuro/issues/127
[#128]: https://github.com/zakuro-ai/zakuro/issues/128
[#129]: https://github.com/zakuro-ai/zakuro/issues/129
[#151]: https://github.com/zakuro-ai/zakuro/pull/151
[#166]: https://github.com/zakuro-ai/zakuro/pull/166
[#174]: https://github.com/zakuro-ai/zakuro/issues/174
[#175]: https://github.com/zakuro-ai/zakuro/issues/175
[#176]: https://github.com/zakuro-ai/zakuro/issues/176
[#214]: https://github.com/zakuro-ai/zakuro/pull/214
[#215]: https://github.com/zakuro-ai/zakuro/pull/215
[#217]: https://github.com/zakuro-ai/zakuro/pull/217
[#219]: https://github.com/zakuro-ai/zakuro/pull/219
[#220]: https://github.com/zakuro-ai/zakuro/pull/220
[#221]: https://github.com/zakuro-ai/zakuro/pull/221
[#223]: https://github.com/zakuro-ai/zakuro/pull/223
[#236]: https://github.com/zakuro-ai/zakuro/pull/236
[#237]: https://github.com/zakuro-ai/zakuro/pull/237
[#238]: https://github.com/zakuro-ai/zakuro/pull/238
[#239]: https://github.com/zakuro-ai/zakuro/pull/239
[#240]: https://github.com/zakuro-ai/zakuro/pull/240
