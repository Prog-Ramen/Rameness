"""Money as integer cents, with half-up rounding (as on printed invoices)."""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


class Money:
    __slots__ = ("cents",)

    def __init__(self, cents: int):
        if not isinstance(cents, int):
            raise TypeError("Money takes integer cents; use Money.of() for decimal amounts")
        self.cents = cents

    @classmethod
    def of(cls, amount) -> "Money":
        """From a decimal amount ("12.345", 12.345 or Decimal): rounded half-up to the cent."""
        d = Decimal(str(amount)).quantize(Decimal("0.01"))
        return cls(int(d * 100))

    def __add__(self, other: "Money") -> "Money":
        return Money(self.cents + other.cents)

    def __sub__(self, other: "Money") -> "Money":
        return Money(self.cents - other.cents)

    def times(self, factor) -> "Money":
        """Multiply by a quantity or rate, rounding half-up to the cent."""
        d = (Decimal(self.cents) * Decimal(str(factor))).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return Money(int(d))

    def __eq__(self, other) -> bool:
        return isinstance(other, Money) and self.cents == other.cents

    def __lt__(self, other: "Money") -> bool:
        return self.cents < other.cents

    def __hash__(self) -> int:
        return hash(self.cents)

    def __str__(self) -> str:
        sign = "-" if self.cents < 0 else ""
        c = abs(self.cents)
        return f"{sign}{c // 100}.{c % 100:02d}"

    __repr__ = lambda self: f"Money({self})"
