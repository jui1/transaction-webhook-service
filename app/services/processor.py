"""Process one saved transaction without finishing it twice."""

import logging
import uuid

from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.core.exceptions import RetryableProcessingError
from app.core.time import utcnow
from app.db.session import session_scope
from app.models.transaction import TransactionStatus
from app.repositories import outbox_repository, transaction_repository
from app.services.payment_processor import simulate_external_payment_call

logger = logging.getLogger(__name__)


def _finish_if_already_processed(session: Session, transaction_id: str) -> bool:
    transaction = transaction_repository.get_by_transaction_id(session, transaction_id)
    if transaction is None or transaction.status != TransactionStatus.PROCESSED.value:
        return False
    outbox_repository.mark_completed(session, transaction_id, utcnow())
    session.commit()
    return True


def process_transaction_job(transaction_id: str) -> str:
    """Run one transaction. Calling it again after a crash or a repeat delivery is safe.

    Returns processed, already_processed, in_progress, missing, or lost_lease.
    """
    settings = get_settings()
    token = uuid.uuid4()
    delay_seconds = settings.processing_delay_seconds
    lease_seconds = settings.processing_lease_window_seconds

    with session_scope() as session:
        transaction = transaction_repository.get_by_transaction_id(session, transaction_id)
        if transaction is None:
            logger.warning("processing skipped; transaction %s does not exist", transaction_id)
            return "missing"
        if transaction.status == TransactionStatus.PROCESSED.value:
            outbox_repository.mark_completed(session, transaction_id, utcnow())
            session.commit()
            logger.info("processing skipped; transaction %s is already PROCESSED", transaction_id)
            return "already_processed"

        acquired = transaction_repository.try_acquire_lease(
            session,
            transaction_id,
            token,
            lease_seconds,
            utcnow(),
        )
        if not acquired:
            if _finish_if_already_processed(session, transaction_id):
                return "already_processed"
            logger.info("processing skipped; transaction %s lease is held", transaction_id)
            return "in_progress"
        session.commit()

    logger.info("processing transaction %s", transaction_id)
    try:
        simulate_external_payment_call(delay_seconds)
    except Exception as exc:
        logger.exception("external processing failed for %s", transaction_id)
        with session_scope() as session:
            transaction_repository.release_lease(session, transaction_id, token)
            session.commit()
        if isinstance(exc, RetryableProcessingError):
            raise
        raise RetryableProcessingError(str(exc)) from exc

    with session_scope() as session:
        completed = transaction_repository.mark_processed(session, transaction_id, token, utcnow())
        if not completed:
            logger.warning("transaction %s lease was lost before completion", transaction_id)
            return "lost_lease"
        outbox_repository.mark_completed(session, transaction_id, utcnow())
        session.commit()

    logger.info("processed transaction %s", transaction_id)
    return "processed"
