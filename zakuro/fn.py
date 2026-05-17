"""Function wrapper for remote execution."""

from __future__ import annotations

import functools
from collections.abc import Callable
from typing import Any, Generic, TypeVar, overload

import cloudpickle

from zakuro.compute import Compute

T = TypeVar("T")
R = TypeVar("R")


class Fn(Generic[R]):
    """
    Wrapper around a callable for remote execution.

    Example:
        >>> @fn
        ... def hello(name: str) -> str:
        ...     return f"Hello, {name}!"
        >>>
        >>> remote_hello = hello.to(Compute(cpus=0.5))
        >>> result = remote_hello("World")  # Runs remotely
    """

    def __init__(self, func: Callable[..., R]) -> None:
        self._func = func
        self._compute: Any | None = None  # Compute or AdaptiveCompute
        self._serialized: bytes | None = None
        functools.update_wrapper(self, func)

    def to(self, compute: Any) -> Fn[R]:
        """Attach a compute target.

        Accepts a :class:`~zakuro.compute.Compute` (single worker) or a
        :class:`~zakuro.adaptive.AdaptiveCompute` (multi-worker, Adam-style
        allocator). Returns self so calls chain: ``fn.to(compute)(arg)``.
        """
        self._compute = compute
        return self

    def local(self) -> Fn[R]:
        """Force local execution."""
        self._compute = None
        return self

    def __call__(self, *args: Any, **kwargs: Any) -> R:
        """Execute the function locally or remotely."""
        if self._compute is None:
            # Local execution
            return self._func(*args, **kwargs)

        # AdaptiveCompute handles its own dispatch (pick a worker, time it,
        # record stats). It calls _execute_single_compute on us after
        # re-pointing ``self._compute`` at the chosen worker.
        from zakuro.adaptive import AdaptiveCompute

        if isinstance(self._compute, AdaptiveCompute):
            return self._compute.dispatch(self, args, kwargs)

        # Remote execution with a single Compute target.
        return self._execute_single_compute(*args, **kwargs)

    def _execute_single_compute(self, *args: Any, **kwargs: Any) -> R:
        """Dispatch to a single Compute target — the original single-worker path."""
        if self._compute is None:
            raise RuntimeError("No compute target. Use .to(compute) first.")

        uri = self._compute.uri
        if uri is None and self._compute.host is not None:
            uri = f"zakuro://{self._compute.host}:{self._compute.port}"

        if uri is None:
            from zakuro.standalone import (
                applied_resource_limits,
                detect_backend,
                ensure_standalone_is_safe,
            )

            uri = detect_backend()
            if uri is None:
                ensure_standalone_is_safe(self._compute)
                # Standalone fallback: execute in-process under advisory limits.
                with applied_resource_limits(self._compute):
                    return self._func(*args, **kwargs)
            self._compute.uri = uri

        from zakuro.processors.registry import get_processor

        func_bytes = cloudpickle.dumps(self._func)
        processor = get_processor(uri, self._compute)
        with processor:
            return processor.execute(func_bytes, args, kwargs)

    # Retained as an alias for any external callers that hooked into the
    # pre-adaptive name.
    _execute_remote = _execute_single_compute

    def serialize(self) -> bytes:
        """Serialize the function for transport."""
        if self._serialized is None:
            self._serialized = cloudpickle.dumps(self._func)
        return self._serialized

    @property
    def is_remote(self) -> bool:
        """Check if function is configured for remote execution."""
        return self._compute is not None


@overload
def fn(func: Callable[..., R]) -> Fn[R]: ...


@overload
def fn(func: None = None) -> Callable[[Callable[..., R]], Fn[R]]: ...


def fn(
    func: Callable[..., R] | None = None,
) -> Fn[R] | Callable[[Callable[..., R]], Fn[R]]:
    """
    Decorator to make a function remotely executable.

    Can be used with or without parentheses:

        @fn
        def my_func():
            pass

        @fn()
        def my_func():
            pass

    Example:
        >>> @fn
        ... def compute_pi(n: int) -> float:
        ...     # Expensive computation
        ...     return 3.14159
        >>>
        >>> compute = Compute(cpus=4, memory="8Gi")
        >>> result = compute_pi.to(compute)(1000000)
    """
    if func is not None:
        return Fn(func)

    def decorator(f: Callable[..., R]) -> Fn[R]:
        return Fn(f)

    return decorator


class Cls(Generic[T]):
    """
    Wrapper for classes to be instantiated remotely.

    Example:
        >>> @cls
        ... class Model:
        ...     def __init__(self, path: str):
        ...         self.model = load_model(path)
        ...
        ...     def predict(self, x):
        ...         return self.model(x)
        >>>
        >>> remote_model = Model.to(Compute(gpus=1))("weights.pth")
    """

    def __init__(self, klass: type[T]) -> None:
        self._klass = klass
        self._compute: Compute | None = None
        functools.update_wrapper(self, klass)

    def to(self, compute: Compute) -> Cls[T]:
        """Attach compute resources."""
        self._compute = compute
        return self

    def local(self) -> Cls[T]:
        """Force local instantiation."""
        self._compute = None
        return self

    @property
    def is_remote(self) -> bool:
        """Check if class is configured for remote instantiation."""
        return self._compute is not None

    def __call__(self, *args: Any, **kwargs: Any) -> T:
        """Instantiate the class locally or remotely."""
        if self._compute is None:
            return self._klass(*args, **kwargs)

        if self._compute.uri is None and self._compute.host is None:
            from zakuro.standalone import (
                applied_resource_limits,
                detect_backend,
                ensure_standalone_is_safe,
            )

            uri = detect_backend()
            if uri is None:
                ensure_standalone_is_safe(self._compute)
                # Standalone fallback: instantiate in-process under advisory limits.
                with applied_resource_limits(self._compute):
                    return self._klass(*args, **kwargs)
            self._compute.uri = uri

        # Remote instantiation - returns a proxy
        from zakuro.proxy import RemoteProxy

        return RemoteProxy(self._klass, args, kwargs, self._compute)  # type: ignore[return-value]


@overload
def cls(klass: type[T]) -> Cls[T]: ...


@overload
def cls(klass: None = None) -> Callable[[type[T]], Cls[T]]: ...


def cls(
    klass: type[T] | None = None,
) -> Cls[T] | Callable[[type[T]], Cls[T]]:
    """
    Decorator to make a class remotely instantiable.

    Example:
        >>> @cls
        ... class MyModel:
        ...     def __init__(self, size: int):
        ...         self.size = size
        >>>
        >>> remote_model = MyModel.to(Compute(gpus=1))(size=1024)
    """
    if klass is not None:
        return Cls(klass)

    def decorator(k: type[T]) -> Cls[T]:
        return Cls(k)

    return decorator
