"""Money helpers. All internal amounts are integer cents to avoid float drift."""
from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal


def to_cents(value) -> int:
    """'12.34' / 12.34 / Decimal -> 1234."""
    return int((Decimal(str(value)) * 100).to_integral_value(rounding=ROUND_HALF_UP))


def fmt_amount(cents: int) -> str:
    """1234 -> '12.34' (the string format PayPal expects)."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
