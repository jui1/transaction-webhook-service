"""Read-side transaction lookup."""

from sqlalchemy.orm import Session

from app.models.transaction import Transaction
from app.repositories import transaction_repository


def get_transaction(session: Session, transaction_id: str) -> Transaction | None:
    return transaction_repository.get_by_transaction_id(session, transaction_id)
