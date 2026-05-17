# Zakuro

A context-aware distributed-ML runtime. Decorate a Python function, declare a pool of workers, and the framework routes each call to the worker with the lowest expected time-to-serve — learning from every dispatch, reacting to node failures and performance drift, and never making training wait on things that can be decoupled.

For the long-form pitch, phase status, and measured engineering numbers, see the [runtime tracking board](https://github.com/orgs/zakuro-ai/projects/6) (formerly the in-tree `PRD.md` + `PLAN.md`, migrated to the GitHub Project so the source of truth lives next to the issues).

## Get going

```bash
pip install zakuro-ai
```

```python
import zakuro as zk

@zk.fn
def square(x: int) -> int:
    return x * x

result = square.to(zk.Compute(uri="quic://worker:4433"))(7)  # → 49
```

Then either:

- the [quick-start tour](quick-start.md) (laptop-only, three workers, mesh adaptation), or
- the [full getting-started guide](getting-started.md) (networked path, broker, multi-node).

## Stability

The public surface lives at [`zakuro.public`](STABILITY.md). It follows [SemVer](https://semver.org/); deprecations get at least two minor versions of warning before removal; the first 1.0 release ships with a 24-month LTS commitment.

## Verifying releases

Every wheel and image is signed (Cosign keyless) and attested (SLSA L3). The full verification flow lands as part of the supply-chain hardening PR; the one-liners will live at `/security/verifying-releases.md` once it merges.
