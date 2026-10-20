"""Accept a webhook by persisting the transaction and its outbox event together."""

import logging

from sqlalchemy.orm import Session

from app.models.transaction import Transaction
from app.repositories import transaction_repository
from app.schemas.webhook import WebhookRequest

logger = logging.getLogger(__name__)


def accept_transaction(session: Session, payload: WebhookRequest) -> tuple[Transaction, bool]:
    """Return the transaction and whether this request was a duplicate."""
    transaction, is_new = transaction_repository.insert_new_with_outbox(
        session,
        transaction_id=payload.transaction_id,
        source_account=payload.source_account,
        destination_account=payload.destination_account,
        amount=payload.amount,
        currency=payload.currency,
    )
    if is_new:
        logger.info("accepted transaction %s", transaction.transaction_id)
    else:
        logger.info("duplicate transaction %s ignored", transaction.transaction_id)
    return transaction, not is_new
