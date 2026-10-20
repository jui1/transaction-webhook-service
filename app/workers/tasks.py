"""Celery task for processing one transaction."""

import logging

from app.core.exceptions import RetryableProcessingError
from app.services.processor import process_transaction_job
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


@celery_app.task(
    bind=True,
    name="app.workers.tasks.process_transaction",
    max_retries=5,
    acks_late=True,
    reject_on_worker_lost=True,
)
def process_transaction(self, transaction_id: str) -> str:
    try:
        return process_transaction_job(transaction_id)
    except RetryableProcessingError as exc:
        # Stop after the retry limit. Celery would otherwise raise the original
        # error. The dispatcher publishes the unfinished outbox row again later.
        if self.request.retries >= self.max_retries:
            logger.error(
                "celery retries exhausted for %s; the outbox dispatcher will redeliver",
                transaction_id,
            )
            return "retry_exhausted"
        countdown = min(2 ** self.request.retries, 60)
        logger.warning(
            "retrying transaction %s after transient failure: %s",
            transaction_id,
            exc,
        )
        raise self.retry(exc=exc, countdown=countdown) from exc
