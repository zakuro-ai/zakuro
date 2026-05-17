"""
Zakuro - Distributed computing made simple.

A kubetorch-like distributed computing library for the Zakuro platform.

Example:
    >>> import zakuro as zk
    >>>
    >>> def hello_world():
    ...     return "Hello from Zakuro!"
    >>>
    >>> compute = zk.Compute(cpus=0.5, memory="2Gi")
    >>> remote_fn = zk.fn(hello_world).to(compute)
    >>> result = remote_fn()  # Runs on Zakuro cluster
"""

__version__ = "0.2.17"
__build__ = "2026-02-15T15:47:22+0000"

# Stable subset — see `docs/stability.md`. `zakuro.public` is the preferred
# import for applications that want a stability guarantee.
from zakuro import public as public  # noqa: F401
from zakuro.adaptive import AdaptiveCompute
from zakuro.compute import Compute
from zakuro.config import Config
from zakuro.fn import Fn, cls, fn
from zakuro.processors.registry import available_processors
from zakuro.standalone import detect_backend, is_standalone
from zakuro.worker.runner import Worker

__all__ = [
    "AdaptiveCompute",
    "Compute",
    "Config",
    "Fn",
    "fn",
    "cls",
    "available_processors",
    "detect_backend",
    "is_standalone",
    "Worker",
    "__version__",
]
