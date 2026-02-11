"""Tests for function serialization."""

import cloudpickle
import pytest

from zakuro.fn import Fn


class TestSerialization:
    """Test cases for function serialization."""

    def test_serialize_simple_function(self) -> None:
        """Test serializing a simple function."""

        def add(a: int, b: int) -> int:
            return a + b

        wrapped = Fn(add)
        serialized = wrapped.serialize()

        deserialized = cloudpickle.loads(serialized)
        assert deserialized(2, 3) == 5

    def test_serialize_closure(self) -> None:
        """Test serializing a closure."""
        multiplier = 10

        def multiply(x: int) -> int:
            return x * multiplier

        wrapped = Fn(multiply)
        serialized = wrapped.serialize()

        deserialized = cloudpickle.loads(serialized)
        assert deserialized(5) == 50

    def test_serialize_lambda(self) -> None:
        """Test serializing a lambda."""
        wrapped = Fn(lambda x: x**2)
        serialized = wrapped.serialize()

        deserialized = cloudpickle.loads(serialized)
        assert deserialized(4) == 16

    def test_serialize_with_imports(self) -> None:
        """Test serializing a function with imports."""

        def use_math(x: float) -> float:
            import math

            return math.sqrt(x)

        wrapped = Fn(use_math)
        serialized = wrapped.serialize()

        deserialized = cloudpickle.loads(serialized)
        assert deserialized(16) == 4.0

    def test_serialize_caches_result(self) -> None:
        """Test that serialize() caches the result."""

        def simple() -> int:
            return 42

        wrapped = Fn(simple)

        # First call
        serialized1 = wrapped.serialize()
        # Second call should return cached
        serialized2 = wrapped.serialize()

        assert serialized1 is serialized2

    def test_serialize_full_payload(self) -> None:
        """Test serializing function with args and kwargs."""

        def greet(name: str, greeting: str = "Hello") -> str:
            return f"{greeting}, {name}!"

        payload = cloudpickle.dumps(
            {
                "func": greet,
                "args": ("World",),
                "kwargs": {"greeting": "Hi"},
            }
        )

        data = cloudpickle.loads(payload)
        result = data["func"](*data["args"], **data["kwargs"])
        assert result == "Hi, World!"

    def test_serialize_class(self) -> None:
        """Test serializing a class."""

        class Calculator:
            def __init__(self, value: int = 0):
                self.value = value

            def add(self, x: int) -> int:
                self.value += x
                return self.value

        serialized = cloudpickle.dumps(Calculator)
        CalcClass = cloudpickle.loads(serialized)

        calc = CalcClass(10)
        assert calc.add(5) == 15
