"""Demo: exercise the standalone fallback path."""

import os
import warnings

import zakuro as zk
from zakuro.standalone import detect_backend


def observe_threads():
    return {
        "OMP_NUM_THREADS": os.environ.get("OMP_NUM_THREADS"),
        "MKL_NUM_THREADS": os.environ.get("MKL_NUM_THREADS"),
        "CUDA_VISIBLE_DEVICES": os.environ.get("CUDA_VISIBLE_DEVICES"),
    }


if __name__ == "__main__":
    print(f"detect_backend() -> {detect_backend()!r}")

    # Memory omitted: standalone can't enforce memory and will raise if set.
    compute = zk.Compute(cpus=2, gpus=0)
    print(f"Compute.uri      -> {compute.uri!r}")
    print(f"Compute.host     -> {compute.host!r}")
    print(f"Compute.scheme   -> {compute.scheme!r}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        endpoint = compute.endpoint
    print(f"Compute.endpoint -> {endpoint!r}")
    print(f"  warning: {caught[0].message}" if caught else "  (no warning)")

    print(f"env before call  -> {observe_threads()}")
    result = zk.fn(observe_threads).to(compute)()
    print(f"env during call  -> {result}")
    print(f"env after call   -> {observe_threads()}")
