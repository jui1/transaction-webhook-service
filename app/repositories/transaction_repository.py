"""Transaction persistence."""

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import or_, select, update
from sqlalchemy.dialects.postgresql import insert as postgres_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from app.core.time import utcnow
from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.transaction import Transaction, TransactionStatus


def _insert_for(session: Session, model: type[Transaction] | type[OutboxEvent]):
    dialect_name = session.get_bind().dialect.name
    if dialect_name == "postgresql":
        return postgres_insert(model)
    if dialect_name == "sqlite":
        return sqlite_insert(model)
    raise RuntimeError(f"Unsupported database dialect: {dialect_name}")


def get_by_transaction_id(session: Session, transaction_id: str) -> Transaction | None:
    return session.scalar(select(Transaction).where(Transaction.transaction_id == transaction_id))


def insert_new_with_outbox(
    session: Session,
    *,
    transaction_id: str,
    source_account: str,
    destination_account: str,
    amount: Decimal,
    currency: str,
) -> tuple[Transaction, bool]:
    """Insert the transaction and its outbox row, then commit both.

    A duplicate transaction_id hits ON CONFLICT DO NOTHING. That call returns
    the row that was already saved.
    """
    now = utcnow()
    primary_key = uuid.uuid4()
    outbox_id = uuid.uuid4()
    try:
        inserted = session.execute(
            _insert_for(session, Transaction)
            .values(
                id=primary_key,
                transaction_id=transaction_id,
                source_account=source_account,
                destination_account=destination_account,
                amount=amount,
                currency=currency,
                status=TransactionStatus.PROCESSING.value,
                created_at=now,
                processed_at=None,
                processing_token=None,
                lease_expires_at=None,
            )
            .on_conflict_do_nothing(index_elements=["transaction_id"])
            .returning(Transaction.id)
        ).first()
        if inserted is None:
            session.rollback()
            existing = get_by_transaction_id(session, transaction_id)
            if existing is None:
                raise RuntimeError(
                    "transaction conflict detected but the existing row was not found"
                )
            return existing, False

        session.execute(
            _insert_for(session, OutboxEvent).values(
                id=outbox_id,
                transaction_pk=primary_key,
                transaction_id=transaction_id,
                event_type="transaction.process",
                status=OutboxStatus.PENDING.value,
                attempts=0,
                last_error=None,
                available_at=now,
                created_at=now,
                dispatched_at=None,
                completed_at=None,
            )
        )
        session.commit()
    except Exception:
        session.rollback()
        raise

    created = get_by_transaction_id(session, transaction_id)
    if created is None:
        raise RuntimeError("transaction insert committed but the row could not be reloaded")
    return created, True


def try_acquire_lease(
    session: Session,
    transaction_id: str,
    token: uuid.UUID,
    lease_seconds: float,
    now: datetime,
) -> bool:
    """Take a PROCESSING row whose lease is empty or already expired."""
    expires_at = now + timedelta(seconds=lease_seconds)
    result = session.execute(
        update(Transaction)
        .where(
            Transaction.transaction_id == transaction_id,
            Transaction.status == TransactionStatus.PROCESSING.value,
            or_(
                Transaction.lease_expires_at.is_(None),
                Transaction.lease_expires_at < now,
            ),
        )
        .values(processing_token=token, lease_expires_at=expires_at)
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    return result.first() is not None


def release_lease(session: Session, transaction_id: str, token: uuid.UUID) -> None:
    session.execute(
        update(Transaction)
        .where(
            Transaction.transaction_id == transaction_id,
            Transaction.status == TransactionStatus.PROCESSING.value,
            Transaction.processing_token == token,
        )
        .values(processing_token=None, lease_expires_at=None)
        .execution_options(synchronize_session=False)
    )


def mark_processed(session: Session, transaction_id: str, token: uuid.UUID, now: datetime) -> bool:
    """Mark the row PROCESSED only if this worker still holds the lease."""
    result = session.execute(
        update(Transaction)
        .where(
            Transaction.transaction_id == transaction_id,
            Transaction.status == TransactionStatus.PROCESSING.value,
            Transaction.processing_token == token,
        )
        .values(
            status=TransactionStatus.PROCESSED.value,
            processed_at=now,
            processing_token=None,
            lease_expires_at=None,
        )
        .returning(Transaction.id)
        .execution_options(synchronize_session=False)
    )
    return result.first() is not None
