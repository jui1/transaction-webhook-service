"""Celery app. Importing this module also loads the task."""

from celery import Celery

from app.core.config import get_settings

settings = get_settings()

celery_app = Celery(
    "transaction_webhook_service",
    broker=settings.redis_url,
    backend=settings.redis_url,
)

celery_app.conf.update(
    task_default_queue=settings.celery_task_queue,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_track_started=True,
    worker_prefetch_multiplier=1,
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

# Import the task after celery_app exists, or the decorator has nothing to use.
from app.workers import tasks as tasks  # noqa: E402,F401
