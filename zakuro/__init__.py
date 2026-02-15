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

__version__ = "0.2.0"
__build__ = "2026-02-12T15:37:02+0900"

from zakuro.compute import Compute
from zakuro.config import Config
from zakuro.fn import Fn, cls, fn
from zakuro.processors.registry import available_processors

__all__ = [
    "Compute",
    "Config",
    "Fn",
    "fn",
    "cls",
    "available_processors",
    "__version__",
]
