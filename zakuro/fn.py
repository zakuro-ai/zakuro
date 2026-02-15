"""Function wrapper for remote execution."""

from __future__ import annotations

import functools
from typing import Any, Callable, Generic, Optional, TypeVar, overload

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
        self._compute: Optional[Compute] = None
        self._serialized: Optional[bytes] = None
        functools.update_wrapper(self, func)

    def to(self, compute: Compute) -> Fn[R]:
        """Attach compute resources for remote execution."""
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

        # Remote execution
        return self._execute_remote(*args, **kwargs)

    def _execute_remote(self, *args: Any, **kwargs: Any) -> R:
        """Send function to worker for execution."""
        if self._compute is None:
            raise RuntimeError("No compute target. Use .to(compute) first.")

        from zakuro.processors.registry import get_processor

        # Serialize function
        func_bytes = cloudpickle.dumps(self._func)

        # Get processor based on URI scheme
        uri = self._compute.uri
        if uri is None:
            uri = f"zakuro://{self._compute.host}:{self._compute.port}"

        processor = get_processor(uri, self._compute)

        # Execute via processor
        with processor:
            return processor.execute(func_bytes, args, kwargs)

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
    func: Optional[Callable[..., R]] = None,
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
        self._compute: Optional[Compute] = None
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

        # Remote instantiation - returns a proxy
        from zakuro.proxy import RemoteProxy

        return RemoteProxy(self._klass, args, kwargs, self._compute)  # type: ignore[return-value]


@overload
def cls(klass: type[T]) -> Cls[T]: ...


@overload
def cls(klass: None = None) -> Callable[[type[T]], Cls[T]]: ...


def cls(
    klass: Optional[type[T]] = None,
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
