# Verifying Zakuro releases

Every Zakuro release is signed and attested:

| Artefact | Signed with | Provenance | SBOM |
|---|---|---|---|
| PyPI wheel `zakuro-ai-<version>-py3-none-any.whl` | SLSA L3 (built-in) | SLSA generator | CycloneDX, in `dist/sbom.cyclonedx.json` + GitHub Release asset |
| Container image `zakuroai/zakuro-worker:<tag>` | Cosign keyless (Sigstore / Rekor) | Cosign SLSA attestation | CycloneDX, attached as `cosign attest` predicate + Release asset |
| Container image `zakuroai/zakuro-broker:<tag>` | Cosign keyless (Sigstore / Rekor) | Cosign SLSA attestation | CycloneDX, attached as `cosign attest` predicate + Release asset |
| Same images mirrored to `ghcr.io/zakuro-ai/...` | same | same | same |

All signatures are recorded in the public Rekor transparency log.

The expected signing identity for every release is:

| field | value |
|---|---|
| `certificate-identity-regexp` | `^https://github\.com/zakuro-ai/zakuro/\.github/workflows/publish\.yml@refs/tags/.*$` |
| `certificate-oidc-issuer` | `https://token.actions.githubusercontent.com` |

## Wheel — verify SLSA provenance

```bash
TAG=v0.2.5                                                      # adjust
WHEEL=zakuro_ai-${TAG#v}-py3-none-any.whl
ATT=${WHEEL}.intoto.jsonl

# Download both from the release page:
gh release download "$TAG" -R zakuro-ai/zakuro -p "$WHEEL" -p "$ATT"

# slsa-verifier checks: signed by the right workflow, builder == GitHub Actions,
# the wheel SHA matches what the workflow attested.
slsa-verifier verify-artifact \
    --provenance-path "$ATT" \
    --source-uri github.com/zakuro-ai/zakuro \
    --source-tag "$TAG" \
    "$WHEEL"
```

If the wheel was tampered with, the digest comparison fails. If the attestation was forged, the Sigstore cert chain fails.

## Container image — verify Cosign signature

```bash
IMAGE=ghcr.io/zakuro-ai/zakuro-worker:0.2.5   # or zakuroai/zakuro-worker:0.2.5

cosign verify "$IMAGE" \
    --certificate-identity-regexp '^https://github\.com/zakuro-ai/zakuro/\.github/workflows/publish\.yml@refs/tags/.*$' \
    --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

Unsigned or tampered images fail verification. There is no fall-through path.

## Container image — retrieve the SBOM

```bash
# CycloneDX SBOM attached as a Cosign attestation
cosign download attestation "$IMAGE" --predicate-type=cyclonedx \
    | jq -r .payload | base64 -d | jq .
```

## Pin to a digest for production

After verifying once, prefer immutable pinning:

```bash
DIGEST=$(docker buildx imagetools inspect "$IMAGE" --format '{{.Manifest.Digest}}')
echo "$IMAGE@$DIGEST"
```

Use `$IMAGE@$DIGEST` in `docker-compose.yml`, Helm values, Kubernetes manifests, etc. — the tag can be reassigned, the digest cannot.

## Reporting a verification failure

If `cosign verify` or `slsa-verifier verify-artifact` fails on a wheel or image we appear to have released, **do not run the artefact**. Report to **security@zakuro.ai** — a verification failure on a legitimate Zakuro release is either a supply-chain incident or a packaging bug, and we treat both at the same priority.
