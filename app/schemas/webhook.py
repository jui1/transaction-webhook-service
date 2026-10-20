"""Webhook request and acknowledgement models."""

from decimal import Decimal, InvalidOperation
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


class WebhookRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    transaction_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_\-\.]+$")
    source_account: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_\-\.]+$")
    destination_account: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_\-\.]+$")
    amount: Decimal
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")

    @field_validator("amount", mode="before")
    @classmethod
    def parse_amount(cls, value: object) -> Decimal:
        if isinstance(value, bool) or value is None:
            raise ValueError("amount must be a positive number with at most 2 decimal places")
        try:
            amount = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(
                "amount must be a positive number with at most 2 decimal places"
            ) from exc
        if not amount.is_finite():
            raise ValueError("amount must be a positive number with at most 2 decimal places")
        exponent = amount.as_tuple().exponent
        if isinstance(exponent, int) and exponent < -2:
            raise ValueError("amount supports at most 2 decimal places")
        if amount <= 0:
            raise ValueError("amount must be greater than zero")
        if amount > Decimal("9999999999999999.99"):
            raise ValueError("amount exceeds the supported maximum")
        return amount.quantize(Decimal("0.01"))


class WebhookAcknowledgement(BaseModel):
    status: Literal["accepted", "duplicate"]
    transaction_id: str
    message: str
