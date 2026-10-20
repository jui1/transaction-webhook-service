"""Read saved outbox rows and put them on the Celery queue."""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.time import utcnow
from app.repositories import outbox_repository

logger = logging.getLogger(__name__)


class TaskPublisher(Protocol):
    def enqueue(self, transaction_id: str, attempt: int) -> None:
        """Enqueue one job. Raise if Redis does not accept it."""


@dataclass(frozen=True)
class ClaimedEvent:
    id: UUID
    transaction_id: str
    attempts: int


def dispatch_once(
    session_factory: sessionmaker[Session] | Callable[[], Session],
    publisher: TaskPublisher,
    *,
    now: datetime | None = None,
) -> int:
    """Claim due outbox rows and enqueue them. A Redis failure leaves the row pending."""
    settings = get_settings()
    current_time = now or utcnow()
    visibility_timeout = settings.outbox_redelivery_window_seconds
    session = session_factory()
    try:
        outbox_repository.ensure_outbox_for_processing_transactions(
            session,
            current_time,
            settings.outbox_batch_size,
        )
        session.flush()
        events = outbox_repository.claim_due_events(
            session,
            current_time,
            limit=settings.outbox_batch_size,
            visibility_timeout_seconds=visibility_timeout,
        )
        claimed = [
            ClaimedEvent(id=event.id, transaction_id=event.transaction_id, attempts=event.attempts)
            for event in events
        ]
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()

    published = 0
    for event in claimed:
        try:
            publisher.enqueue(event.transaction_id, event.attempts)
        except Exception as exc:
            logger.exception("failed to enqueue transaction %s", event.transaction_id)
            retry_delay = min(2 ** min(event.attempts, 6), 60)
            failure_session = session_factory()
            try:
                outbox_repository.mark_dispatch_failed(
                    failure_session,
                    event.id,
                    str(exc),
                    current_time + timedelta(seconds=retry_delay),
                )
                failure_session.commit()
            except Exception:
                failure_session.rollback()
                logger.exception("failed to revert outbox event %s", event.id)
            finally:
                failure_session.close()
            continue
        published += 1
        logger.info("enqueued transaction %s attempt %s", event.transaction_id, event.attempts)
    return published
