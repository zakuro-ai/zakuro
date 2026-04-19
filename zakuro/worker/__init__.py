"""Zakuro worker package.

Submodules are imported lazily so that ``import zakuro`` (or
``from zakuro.worker.runner import Worker``) does not drag in FastAPI /
uvicorn / psutil — those are only required to *run* the worker server,
not to call into a running one.
"""

__all__: list[str] = []
