# Contributing to Zakuro

Thanks for your interest. This document covers everything you need to send a patch that has a good chance of being merged quickly.

## Code of Conduct

Participation in this project is governed by the [Contributor Covenant](CODE_OF_CONDUCT.md). By contributing you agree to abide by it.

## Quick links

- **Bugs and feature requests** → [GitHub Issues](https://github.com/zakuro-ai/zakuro/issues)
- **Security issues** → see [`SECURITY.md`](SECURITY.md) (do **not** file a public issue)
- **Architectural decisions** → [`docs/rfcs/`](docs/rfcs/)
- **Active roadmap** → [Runtime tracking board](https://github.com/orgs/zakuro-ai/projects/6)

## Development setup

You'll need Python 3.10+ and [`uv`](https://github.com/astral-sh/uv).

```bash
git clone git@github.com:zakuro-ai/zakuro.git
cd zakuro
uv sync --extra all                  # full dev install (worker + processors + dev tools)
uv run pre-commit install            # ruff + mypy + gitleaks on every commit
uv run pytest                        # 90+ tests, ~30 s on a laptop
```

For the Rust workspace (`crates/zakuro-wire`) you'll also need a stable Rust toolchain. `cargo test --workspace` from the repo root.

The full dev loop runs in Docker too:

```bash
task ci-local                        # lint, format-check, typecheck, pytest, cargo test
```

## Branch + commit conventions

- Branch names: `kind/short-slug-NNN` where `NNN` is the issue number when applicable.
  - `feat/`, `fix/`, `chore/`, `docs/`, `refactor/`, `test/`, `ci/`, `perf/`, `security/`.
- Commit messages: [Conventional Commits](https://www.conventionalcommits.org/).
  - `fix(quic): close connection cleanly on worker shutdown (#142)`
  - `feat(adaptive): add price-aware routing (#173)`
- **Do not add `Co-Authored-By` trailers to commits or PR bodies.** Squash-merge rewrites authorship on the merge commit; trailers cause duplicate credit.
- Sign your commits (`git commit -S`) when you can — not required, but appreciated.

## Pull-request checklist

Before you click "Create PR":

- [ ] `task ci-local` passes (ruff, mypy, pytest, cargo test).
- [ ] New code has tests in `tests/` (Python) or `crates/*/tests/` (Rust).
- [ ] Public-API changes update [`docs/STABILITY.md`](docs/STABILITY.md) and the typed surface in `zakuro/public.py`.
- [ ] Wire-format / broker-contract changes are paired with a `zakuro-ai/zc` PR — these two repos move together via the `zakuro-wire` crate (see [RFC 0001](docs/rfcs/0001-wire-format-postcard.md)).
- [ ] Dockerfile / image / SBOM changes go in [`zakuro-ai/zakuro-image`](https://github.com/zakuro-ai/zakuro-image), not here.
- [ ] PR title follows Conventional Commits and references the issue (`feat(x): … (#N)`).
- [ ] PR body has a "Summary" + "Test plan" section. The PR template enforces this.

## What gets reviewed

- **Correctness** before style. Reviewer questions on logic land first; reviewer nits on naming land later.
- **Public-API stability.** Anything reachable from `zakuro.public` is subject to the SemVer policy in `docs/STABILITY.md` — breaking changes need a deprecation cycle.
- **Cross-repo coherence.** If your change moves the wire format or the broker contract, the reviewer will check for a paired zc PR. If it touches the container image, they'll check for a paired zakuro-image PR.
- **Test coverage.** New code without tests is a request-changes; we can help you write them if the framework feels unfamiliar.
- **Observability and security.** New endpoints need auth scopes (RFC 0002), new metrics need cardinality budgets (RFC 0003), new dependencies need an SBOM-friendly licence.

We aim for a first review within **2 business days**. If a PR sits idle longer than that, ping `@zakuro-ai/maintainers` on the PR — it's not impolite, it's expected.

## Local testing tips

- `pytest -x -k <substring>` is the fastest way to iterate on a single failure.
- `pytest -m integration` runs the docker-compose smoke lane locally (needs Docker).
- For QUIC-related changes, `pytest -m quic --tb=short` runs only those tests.
- Set `ZAKURO_LOG_LEVEL=DEBUG` for structured-log verbosity during a test run.

## RFCs

If you're proposing a non-trivial change — new public API, new on-wire field, new dependency, new deployment shape — open a short RFC under `docs/rfcs/`. Use the existing RFCs as a template. RFCs land via PR, like code.

## Releasing

Releases are cut from a `release/vX.Y.Z` branch — there is no `release-please`. The single source of truth for the version is `__version__` in `zakuro/__init__.py`; the worker reports it on its `/info` and `/` endpoints, and the wheel reads it via Hatch (`[tool.hatch.version]`).

The pipeline, in order:

1. Push a `release/vX.Y.Z` branch. CI runs against it (`.github/workflows/ci.yml`).
2. On CI success, **Release Build** (`release.yml`) builds, signs, and pushes the worker image and its SBOM.
3. Dispatch **Publish Packages** (`publish.yml`, manual `workflow_dispatch`) to publish the wheel to PyPI (Trusted Publishing) and the `zakuro-wire` crate to crates.io.
4. On publish success, **GitHub Release** (`github-release.yml`) stamps `__version__`, commits `chore(release): vX.Y.Z`, tags `vX.Y.Z`, and creates the GitHub Release with auto-generated notes.

The `chore(release): vX.Y.Z` commits are bot output — don't write them by hand. Per-release notes are auto-generated from merged PRs; `CHANGELOG.md` is the human-curated rollup — add a one-line entry under `## [Unreleased]` in the same PR as any user-visible change.

## Questions

- Open a GitHub Discussion for design questions or longer-form conversations.
- Use issues for bugs and concrete feature asks.
- For private questions email `dev@zakuro.ai`.

We're happy you're here. Send the patch.
