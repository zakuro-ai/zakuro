"""Processor backends for Zakuro distributed computing.

This module provides a unified interface for different compute backends:
- HTTP (zakuro://): Default HTTP-based execution via ZakuroClient
- Ray (ray://): Ray distributed computing
- Dask (dask://): Dask distributed computing
- Spark (spark://): Apache Spark

Example:
    >>> from zakuro.processors import get_processor, available_processors
    >>>
    >>> # Check what's available
    >>> print(available_processors())
    ['dask', 'ray', 'zakuro']
    >>>
    >>> # Get a processor by URI
    >>> processor = get_processor("ray://head:10001", compute)
    >>> with processor:
    ...     result = processor.execute(func_bytes, args, kwargs)
"""

from zakuro.processors.base import Processor, ProcessorConfig
from zakuro.processors.registry import (
    ProcessorRegistry,
    available_processors,
    get_best_processor,
    get_processor,
)

__all__ = [
    "Processor",
    "ProcessorConfig",
    "ProcessorRegistry",
    "get_processor",
    "get_best_processor",
    "available_processors",
]
