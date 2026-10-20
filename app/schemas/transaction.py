"""Transaction query response."""

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, field_serializer

from app.core.time import isoformat_utc


class TransactionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    transaction_id: str
    source_account: str
    destination_account: str
    amount: Decimal
    currency: str
    status: str
    created_at: datetime
    processed_at: datetime | None

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> int | float:
        quantized = Decimal(value).quantize(Decimal("0.01"))
        if quantized == quantized.to_integral_value():
            return int(quantized)
        return float(quantized)

    @field_serializer("created_at", "processed_at")
    def serialize_timestamp(self, value: datetime | None) -> str | None:
        if value is None:
            return None
        return isoformat_utc(value)
