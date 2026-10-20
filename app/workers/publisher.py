"""Celery publisher used by the outbox dispatcher."""

from app.core.config import get_settings
from app.workers.celery_app import celery_app


class CeleryTaskPublisher:
    def enqueue(self, transaction_id: str, attempt: int) -> None:
        settings = get_settings()
        celery_app.send_task(
            "app.workers.tasks.process_transaction",
            args=[transaction_id],
            queue=settings.celery_task_queue,
        )
