"""Invoices: lines, a percentage discount, tax, totals."""
from __future__ import annotations

from dataclasses import dataclass, field

from .money import Money


@dataclass
class Line:
    sku: str
    description: str
    unit_price: Money
    quantity: int = 1
    taxable: bool = True

    def total(self) -> Money:
        return self.unit_price.times(self.quantity)


@dataclass
class Invoice:
    number: str
    customer: str
    lines: list[Line] = field(default_factory=list)
    discount_percent: float = 0.0        # applied to the subtotal before tax
    tax_rate: float = 0.0                # e.g. 0.08 for 8%
    credit_note: bool = False            # a credit note may have negative quantities

    def __post_init__(self):
        if not 0 <= self.discount_percent <= 100:
            raise ValueError("discount_percent must be between 0 and 100")

    def add(self, line: Line) -> "Invoice":
        for existing in self.lines:
            if existing.sku == line.sku:          # same product again: merge into one line
                existing.quantity += line.quantity
                return self
        self.lines.append(line)
        return self

    def subtotal(self) -> Money:
        total = Money(0)
        for line in self.lines:
            total = total + line.total()
        return total

    def discount(self) -> Money:
        return self.subtotal().times(self.discount_percent / 100)

    def taxable_amount(self) -> Money:
        """Taxable lines after the discount (the discount is spread over lines by value)."""
        sub = self.subtotal()
        if sub.cents == 0:
            return Money(0)
        taxable = Money(0)
        for line in self.lines:
            if line.taxable:
                taxable = taxable + line.total()
        share = taxable.cents / sub.cents
        return taxable - self.discount().times(share)

    def tax(self) -> Money:
        return self.taxable_amount().times(self.tax_rate)

    def total(self) -> Money:
        return self.subtotal() - self.discount() + self.tax()
