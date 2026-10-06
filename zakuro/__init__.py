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

__version__ = "0.2.25"
__build__ = "2026-10-04T00:00:00+0000"

# Stable subset — see `docs/stability.md`. `zakuro.public` is the preferred
# import for applications that want a stability guarantee.
from zakuro import public as public  # noqa: F401
from zakuro.adaptive import AdaptiveCompute
from zakuro.compute import Compute
from zakuro.config import Config
from zakuro.fn import Fn, cls, fn
from zakuro.mesh import NodeInfo, nodes, resolve_node
from zakuro.model import ChatResult, Model, TranscriptResult, model
from zakuro.processors.registry import available_processors
from zakuro.reconcile import Charge, Reconciliation
from zakuro.replay import replay_decisions
from zakuro.standalone import detect_backend, is_standalone
from zakuro.task import Job, JobResult, Plan, PlanStep, Task
from zakuro.worker.runner import Worker

__all__ = [
    "AdaptiveCompute",
    "Compute",
    "Config",
    "Fn",
    "fn",
    "nodes",
    "Task",
    "Reconciliation",
    "Charge",
    "Job",
    "JobResult",
    "Plan",
    "PlanStep",
    "NodeInfo",
    "resolve_node",
    "cls",
    "ChatResult",
    "Model",
    "model",
    "TranscriptResult",
    "available_processors",
    "detect_backend",
    "is_standalone",
    "Worker",
    "replay_decisions",
    "__version__",
]
