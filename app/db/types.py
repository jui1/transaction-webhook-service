"""Column types that keep money and UTC times consistent across databases."""

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import DateTime
from sqlalchemy.types import Numeric, TypeDecorator


class MoneyAmount(TypeDecorator):
    """Store money as a decimal and return Decimal in Python, including from SQLite."""

    impl = Numeric(18, 2)
    cache_ok = True

    def process_bind_param(self, value: object, dialect: object) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value)).quantize(Decimal("0.01"))

    def process_result_value(self, value: object, dialect: object) -> Decimal | None:
        if value is None:
            return None
        return Decimal(str(value)).quantize(Decimal("0.01"))


class UTCDateTime(TypeDecorator):
    """Store UTC times. SQLite gets naive UTC; Python always gets an aware time."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: object) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        else:
            value = value.astimezone(timezone.utc)
        if getattr(dialect, "name", None) == "sqlite":
            return value.replace(tzinfo=None)
        return value

    def process_result_value(
        self,
        value: datetime | str | None,
        dialect: object,
    ) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
