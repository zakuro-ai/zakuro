# CI workflows — required vs advisory

Single source of truth for which GitHub Actions lanes block PRs and which are advisory. Branch-protection settings on the `master` branch must match the **required** column below.

Closes [#167](https://github.com/zakuro-ai/zakuro/issues/167) (the CI-blocking initiative).

CI is consolidated in [`.github/workflows/ci.yml`](https://github.com/zakuro-ai/zakuro/blob/master/.github/workflows/ci.yml) and runs on the self-hosted CPU pool tagged `[self-hosted, cpu]`.

## Required (must pass to merge)

| Workflow / Job | What it runs | Why it must block |
|---|---|---|
| `CI / lint-and-test (3.10)` | `task ci:lint`, `task ci:format-check`, `task test:unit` on Python 3.10 | Catches lint regressions and broken unit tests on the lowest supported runtime. |
| `CI / lint-and-test (3.11)` | same on 3.11 | Cross-version coverage. |
| `CI / lint-and-test (3.12)` | same on 3.12 | Cross-version coverage; primary dev target. |
| `CI / typecheck` | `task ci:typecheck` (mypy) | Strict-required since #167. Baseline = 0 errors; new errors fail the build. |
| `CI / rust` | `cargo fmt --check`, `cargo clippy -- -D warnings`, `cargo test --all-features` | The `zakuro-wire` crate is the cross-repo contract with zc. Drift breaks the broker. |
| `Docs / strict-build` | `mkdocs build --strict` | Broken doc links bleed into the published site. Strict-required since the mike-versioned site went live. |
| `CI / notice` | `task notice:check` | NOTICE must match the resolved Python + Rust dependency tree. Required when pyproject / uv.lock / Cargo.toml / Cargo.lock change. |
| `integration / smoke` | docker-compose smoke + zc cross-repo dispatch | Catches drift between the broker image digest pinned here and the actual zc release. **Currently required only on PRs touching `docker/**` or `crates/zakuro-wire/**`.** Flip to always-required once zc#42 lands and we trust the runner pool. |

## Advisory (report-only)

These lanes upload SARIF / log findings but do **not** block PRs. Each one is annotated in its workflow file with the ticket / condition that unlocks the ratchet to required.

| Workflow / Job | Why still advisory | Ratchet condition |
|---|---|---|
| `security-scan / pip-audit` | Catalogue has pre-existing findings the Dependabot rollups have not zeroed yet. | Two consecutive weeks with zero new High/Critical from the rollup PRs. |
| `security-scan / osv-scanner` | OSV's DB updates daily — the lane can flip red on a quiet master with no code change. | Same as pip-audit. |
| `security-scan / trivy-fs` | Same upstream-noise concern as osv-scanner. | Same. |
| `security-scan / trivy-image` | Trivy step currently fails at `docker build` because the wheel artefact isn't wired into this lane yet. Tracked separately. | Wheel-build wiring lands + two-week noise-free window. |
| `sast / semgrep` | Three intentional cloudpickle call-sites flagged. Migration is #117. | #117 lands. |
| `sast / codeql` | No findings today; reporting-only so maintainers can review the GitHub code-scanning view before flipping required. | One full release cycle with zero blocking findings. |

## Branch protection — what to set on `master`

In **Settings → Branches → master** these toggles must be on:

- ✅ Require a pull request before merging (1 reviewer minimum).
- ✅ Require status checks to pass before merging.
  - The required-checks list is exactly the **Required** table above.
- ✅ Require branches to be up to date before merging.
- ✅ Require signed commits.
- ✅ Require linear history (squash-merge produces this automatically).
- ✅ Do not allow bypassing the above settings.
- ❌ Allow force pushes.
- ❌ Allow deletions.

The advisory lanes are intentionally **not** in the required list so a noisy upstream advisory doesn't block emergency fixes.

## How to add a new lane

1. Land the workflow file. Default to **advisory** (`continue-on-error: true`) for the first two weeks so any noise surfaces without blocking.
2. Add the lane to the **Advisory** table here with the explicit ratchet condition.
3. When the condition holds, raise a PR that:
   - removes `continue-on-error` (and any `|| true` shielding the failing step),
   - moves the lane to the **Required** table here,
   - bumps the branch-protection required-checks list in the same PR description (it has to be applied via the org settings UI; the PR diff is just the docs change).

## How to remove a lane

Don't, without a replacement. Every advisory lane corresponds to a specific class of regression. If a lane is "too noisy", fix the noise — don't drop the lane.
