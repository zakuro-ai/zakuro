# Secrets management

Closes #118.

Zakuro encrypts in-tree secrets at rest with [SOPS](https://github.com/getsops/sops) + [age](https://github.com/FiloSottile/age). This keeps secrets versioned with the code, auditable through git history, and decryptable both locally (by developers with the team key) and in CI/CD (by a runner-scoped key in repo secrets).

## Quick start (developer)

1. **Install tooling.** macOS:

   ```bash
   brew install sops age
   ```

   Linux: download binaries from the [SOPS releases](https://github.com/getsops/sops/releases) and [age releases](https://github.com/FiloSottile/age/releases) pages.

2. **Generate your age key pair once.**

   ```bash
   mkdir -p ~/.config/sops/age
   age-keygen -o ~/.config/sops/age/keys.txt
   # The public key (starts with `age1...`) is printed on stderr.
   ```

3. **Get added to the recipient list.** Share your `age1...` public key with a maintainer; they will append it to [`.sops.yaml`](../.sops.yaml) under `creation_rules.age` and run `sops updatekeys` on every existing encrypted file so you can decrypt them too.

4. **Edit / read a secret.**

   ```bash
   sops secrets/sentry-dsn.sops.yaml   # opens decrypted in $EDITOR
   sops -d secrets/sentry-dsn.sops.yaml > /tmp/dsn.yaml   # decrypt to file
   ```

5. **Create a new secret.** Drop a plaintext YAML at the target path, then encrypt in place:

   ```bash
   sops --encrypt --in-place secrets/foo.sops.yaml
   ```

   The `.sops.yaml` config picks the right age recipients automatically based on the path.

## File naming convention

The `.sops.yaml` `creation_rules` rule matches:

- Any file under `secrets/`
- Any file matching `*.sops.yaml`, `*.sops.json`, `*.sops.env`

Within each file, only keys named `data`, `password`, `token`, `secret`, `key`, `dsn`, `credentials`, or any name ending in `_KEY`, `_TOKEN`, `_PASSWORD`, `_SECRET`, `_DSN` are encrypted. Everything else (structural metadata, comments, schema version) stays in plaintext so the file is still readable in code review.

## CI / production loading

For workers + the broker in production:

1. The runner / pod is given a single age private key via the platform's secret-store (k8s Secret, GH Actions `secrets.SOPS_AGE_KEY`, GCP Secret Manager — see [Verification flow](#verification-flow)).
2. At boot, an init script decrypts the configured secret files into a memory-backed location (tmpfs) and exports them as env vars.

A minimal init wrapper lives at `scripts/sops-decrypt-runtime.sh` and is invoked by the entrypoint of the worker / broker images.

## Verification flow

- **Local:** `sops --decrypt secrets/<file>.sops.yaml | jq '.'` returns plaintext if your age key is in the recipient list, fails cleanly otherwise.
- **CI:** the `secrets-pre-commit-check` workflow refuses to merge a PR that contains a file matching the secret naming convention but is *not* actually encrypted (see [`.github/workflows/sast.yml`](../.github/workflows/sast.yml) — `secrets-encryption-check` job).
- **Releases:** the supply-chain pipeline (PR #148) does not embed any secret in shipped artifacts; secrets are only injected at boot.

## Rotation

Rotating an age recipient (departure, key compromise):

1. Remove the recipient's public key from `creation_rules.age` in `.sops.yaml`.
2. Run `sops updatekeys` on every encrypted file:

   ```bash
   find . \( -path './secrets/*' -o -name '*.sops.yaml' -o -name '*.sops.json' -o -name '*.sops.env' \) -print0 \
     | xargs -0 -I {} sops updatekeys --yes {}
   ```

3. Commit the re-encrypted files and the updated `.sops.yaml` in a single PR.

Rotating an actual secret value (e.g., regenerated DSN):

1. `sops <path>` to open the file in your editor.
2. Replace the value, save, and commit. SOPS handles the re-encryption transparently.

## Out of scope here

- **Dynamic / short-lived secrets** (per-request tokens, database creds rotated every 24 h) — that's a HashiCorp Vault story or a cloud-KMS-backed STS, not in scope for the v1 secrets refactor.
- **HSM-backed keys** — same; track separately if compliance requires it.
- **Cross-environment secret promotion** (dev → staging → prod) — for now, each environment has its own age recipient set and its own encrypted files under `secrets/<env>/`.
