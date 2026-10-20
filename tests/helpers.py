"""Request fixtures shared by the API tests."""

from sqlalchemy import func, select

SAMPLE_WEBHOOK = {
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500,
    "currency": "INR",
}


def payload(**overrides: object) -> dict[str, object]:
    data: dict[str, object] = dict(SAMPLE_WEBHOOK)
    data.update(overrides)
    return data


def count_rows(session_factory, model: type) -> int:
    session = session_factory()
    try:
        value = session.scalar(select(func.count()).select_from(model))
        return int(value or 0)
    finally:
        session.close()
