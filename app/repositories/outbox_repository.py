"""Read and update outbox rows."""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import and_, exists, or_, select, update
from sqlalchemy.orm import Session

from app.models.outbox import OutboxEvent, OutboxStatus
from app.models.transaction import Transaction, TransactionStatus


def get_by_transaction_id(session: Session, transaction_id: str) -> OutboxEvent | None:
    return session.scalar(select(OutboxEvent).where(OutboxEvent.transaction_id == transaction_id))


def ensure_outbox_for_processing_transactions(session: Session, now: datetime, limit: int) -> int:
    """Add a pending outbox row when a PROCESSING transaction does not have one."""
    missing = session.scalars(
        select(Transaction)
        .where(
            Transaction.status == TransactionStatus.PROCESSING.value,
            ~exists(select(OutboxEvent.id).where(OutboxEvent.transaction_pk == Transaction.id)),
        )
        .limit(limit)
    ).all()
    for transaction in missing:
        session.add(
            OutboxEvent(
                id=uuid.uuid4(),
                transaction_pk=transaction.id,
                transaction_id=transaction.transaction_id,
                event_type="transaction.process",
                status=OutboxStatus.PENDING.value,
                attempts=0,
                available_at=now,
                created_at=now,
            )
        )
    return len(missing)


def claim_due_events(
    session: Session,
    now: datetime,
    *,
    limit: int,
    visibility_timeout_seconds: float,
) -> list[OutboxEvent]:
    """Claim pending rows, or dispatched rows that have been waiting too long.

    On PostgreSQL this uses SKIP LOCKED so two dispatchers do not take the same
    row. The caller commits before it talks to Redis.
    """
    stale_before = now - timedelta(seconds=visibility_timeout_seconds)
    statement = (
        select(OutboxEvent)
        .where(
            or_(
                and_(
                    OutboxEvent.status == OutboxStatus.PENDING.value,
                    OutboxEvent.available_at <= now,
                ),
                and_(
                    OutboxEvent.status == OutboxStatus.DISPATCHED.value,
                    OutboxEvent.dispatched_at.is_not(None),
                    OutboxEvent.dispatched_at <= stale_before,
                ),
            )
        )
        .order_by(OutboxEvent.created_at)
        .limit(limit)
    )
    if session.get_bind().dialect.name == "postgresql":
        statement = statement.with_for_update(skip_locked=True)

    events = list(session.scalars(statement).all())
    available_again_at = now + timedelta(seconds=visibility_timeout_seconds)
    for event in events:
        event.status = OutboxStatus.DISPATCHED.value
        event.dispatched_at = now
        event.attempts += 1
        event.available_at = available_again_at
        event.last_error = None
    return events


def mark_dispatch_failed(
    session: Session,
    event_id: uuid.UUID,
    error: str,
    available_at: datetime,
) -> None:
    """Put a claimed row back to PENDING when Redis rejects the job."""
    session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.id == event_id,
            OutboxEvent.status == OutboxStatus.DISPATCHED.value,
        )
        .values(
            status=OutboxStatus.PENDING.value,
            last_error=error[:2000],
            available_at=available_at,
        )
        .execution_options(synchronize_session=False)
    )


def mark_completed(session: Session, transaction_id: str, now: datetime) -> None:
    session.execute(
        update(OutboxEvent)
        .where(
            OutboxEvent.transaction_id == transaction_id,
            OutboxEvent.status != OutboxStatus.COMPLETED.value,
        )
        .values(status=OutboxStatus.COMPLETED.value, completed_at=now)
        .execution_options(synchronize_session=False)
    )
