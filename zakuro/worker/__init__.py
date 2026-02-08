"""Zakuro worker module for executing remote functions."""

from zakuro.worker.executor import execute_function
from zakuro.worker.server import app

__all__ = ["app", "execute_function"]
