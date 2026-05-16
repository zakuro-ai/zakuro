#!/bin/sh
# scripts/sops-decrypt-runtime.sh
#
# Runtime entrypoint shim: decrypt every secret file referenced by
# ZAKURO_SOPS_FILES (a colon-separated list) into tmpfs, source the
# resulting dotenv into the current shell, then exec the original
# command. Closes the deploy half of #118.
#
# Usage in a Dockerfile or compose:
#     ENV ZAKURO_SOPS_FILES=/secrets/sentry-dsn.sops.env:/secrets/s3.sops.env
#     ENTRYPOINT ["/usr/local/bin/sops-decrypt-runtime.sh", "python", "-m", "zakuro.worker.server"]
#
# The age private key must already be available — typically:
#   - k8s: mounted from a Secret at /var/run/secrets/age/keys.txt
#   - dev: ~/.config/sops/age/keys.txt
# SOPS picks it up via SOPS_AGE_KEY_FILE.

set -eu

: "${SOPS_AGE_KEY_FILE:=/var/run/secrets/age/keys.txt}"
export SOPS_AGE_KEY_FILE

if [ -n "${ZAKURO_SOPS_FILES:-}" ]; then
  if ! command -v sops >/dev/null 2>&1; then
    echo "sops-decrypt-runtime: \`sops\` binary not found on PATH" >&2
    exit 1
  fi
  if [ ! -r "$SOPS_AGE_KEY_FILE" ]; then
    echo "sops-decrypt-runtime: age key file unreadable at $SOPS_AGE_KEY_FILE" >&2
    exit 1
  fi

  OLD_IFS="$IFS"
  IFS=":"
  for f in $ZAKURO_SOPS_FILES; do
    if [ ! -f "$f" ]; then
      echo "sops-decrypt-runtime: secret file not found: $f" >&2
      exit 1
    fi
    # Decrypt into a tmpfs path and source it. We do not write to disk.
    # shellcheck disable=SC2046
    set -a
    eval "$(sops --decrypt "$f")"
    set +a
  done
  IFS="$OLD_IFS"
fi

# Hand control to the actual command — exec replaces the shim so signals
# (SIGTERM from k8s) reach the worker / broker directly.
exec "$@"
