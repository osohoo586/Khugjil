"""A wrapper for values that must never be printed, logged or serialized by accident."""

from __future__ import annotations

from typing import Final


class Sensitive:
    __slots__ = ("_value",)

    def __init__(self, value: str) -> None:
        self._value: Final = value

    def reveal(self) -> str:
        return self._value

    def __repr__(self) -> str:
        return "<redacted>"

    __str__ = __repr__

    def __format__(self, spec: str) -> str:
        return "<redacted>"

    def __eq__(self, other: object) -> bool:
        return isinstance(other, Sensitive) and other._value == self._value

    def __hash__(self) -> int:
        return hash(("Sensitive", self._value))

    def __reduce__(self) -> tuple[object, ...]:
        raise TypeError("Sensitive values cannot be pickled")
