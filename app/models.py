"""The compiled promise. Whatever the LLM says, only a validated Policy ever reaches the money path."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Policy(BaseModel):
    kind: Literal["price_drop"] = "price_drop"
    window_days: int = Field(ge=1, le=90, description="Days after purchase the promise applies")
    max_refund_per_order: float = Field(gt=0, le=1000, description="Hard cap per order, in dollars")
    daily_budget: float = Field(gt=0, le=100000, description="Merchant-wide refund budget per UTC day, in dollars")
    min_drop_pct: float = Field(default=0, ge=0, lt=100, description="Ignore drops smaller than this % of price paid")
