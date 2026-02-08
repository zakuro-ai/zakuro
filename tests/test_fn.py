"""Tests for fn decorator."""

import pytest

from zakuro import Compute, fn
from zakuro.fn import Cls, Fn, cls


class TestFn:
    """Test cases for fn decorator."""

    def test_local_execution(self) -> None:
        """Test local execution without compute target."""

        @fn
        def add(a: int, b: int) -> int:
            return a + b

        result = add(2, 3)
        assert result == 5

    def test_fn_preserves_name(self) -> None:
        """Test that fn preserves function name."""

        @fn
        def my_func() -> None:
            pass

        assert my_func.__name__ == "my_func"

    def test_fn_preserves_docstring(self) -> None:
        """Test that fn preserves docstring."""

        @fn
        def my_func() -> None:
            """My docstring."""
            pass

        assert my_func.__doc__ == "My docstring."

    def test_to_returns_same_fn(self) -> None:
        """Test that .to() returns the same Fn object."""

        @fn
        def hello() -> str:
            return "hello"

        compute = Compute(host="localhost")
        chained = hello.to(compute)
        assert chained is hello

    def test_local_resets_compute(self) -> None:
        """Test that .local() removes compute target."""

        @fn
        def hello() -> str:
            return "hello"

        compute = Compute(host="localhost")
        hello.to(compute)
        assert hello.is_remote

        hello.local()
        assert not hello.is_remote

    def test_is_remote_property(self) -> None:
        """Test is_remote property."""

        @fn
        def hello() -> str:
            return "hello"

        assert not hello.is_remote

        hello.to(Compute(host="localhost"))
        assert hello.is_remote

    def test_fn_without_parentheses(self) -> None:
        """Test fn decorator without parentheses."""

        @fn
        def add(a: int, b: int) -> int:
            return a + b

        assert isinstance(add, Fn)
        assert add(1, 2) == 3

    def test_fn_with_parentheses(self) -> None:
        """Test fn decorator with empty parentheses."""

        @fn()
        def add(a: int, b: int) -> int:
            return a + b

        assert isinstance(add, Fn)
        assert add(1, 2) == 3

    def test_closure_execution(self) -> None:
        """Test that closures work correctly."""
        multiplier = 10

        @fn
        def multiply(x: int) -> int:
            return x * multiplier

        assert multiply(5) == 50

    def test_lambda_wrapping(self) -> None:
        """Test wrapping lambdas."""
        square = fn(lambda x: x**2)
        assert square(4) == 16


class TestCls:
    """Test cases for cls decorator."""

    def test_local_instantiation(self) -> None:
        """Test local class instantiation."""

        @cls
        class Counter:
            def __init__(self, start: int = 0):
                self.value = start

            def increment(self) -> int:
                self.value += 1
                return self.value

        counter = Counter(10)
        assert counter.value == 10
        assert counter.increment() == 11

    def test_cls_preserves_name(self) -> None:
        """Test that cls preserves class name."""

        @cls
        class MyClass:
            pass

        # Note: Cls wrapper has __wrapped__
        assert "MyClass" in str(MyClass._klass)

    def test_to_returns_same_cls(self) -> None:
        """Test that .to() returns the same Cls object."""

        @cls
        class MyClass:
            pass

        compute = Compute(host="localhost")
        chained = MyClass.to(compute)
        assert chained is MyClass

    def test_cls_without_parentheses(self) -> None:
        """Test cls decorator without parentheses."""

        @cls
        class Simple:
            def get(self) -> str:
                return "value"

        assert isinstance(Simple, Cls)
        instance = Simple()
        assert instance.get() == "value"
