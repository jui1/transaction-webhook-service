"""Concurrent webhooks, outbox recovery, and database or queue failures."""

import threading
from datetime import timedelta
from decimal import Decimal

import pytest
from app.core.config import normalize_database_url
from app.core.exceptions import RetryableProcessingError
from app.core.time import utcnow
from app.models.outbox import OutboxEvent
from app.models.transaction import Transaction
from app.repositories import outbox_repository, transaction_repository
from app.schemas.webhook import WebhookRequest
from app.services.outbox_dispatcher import dispatch_once
from app.services.processor import process_transaction_job
from app.services.webhook_service import accept_transaction
from app.workers.tasks import process_transaction
from sqlalchemy.exc import OperationalError
from tests.helpers import count_rows, payload


class RecordingPublisher:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    def enqueue(self, transaction_id: str, attempt: int) -> None:
        self.calls.append((transaction_id, attempt))


class FailingPublisher:
    def enqueue(self, transaction_id: str, attempt: int) -> None:
        raise ConnectionError("redis unavailable")


def test_transaction_id_unique_constraint_is_declared():
    assert Transaction.__table__.c.transaction_id.unique is True
    assert OutboxEvent.__table__.c.transaction_id.unique is True
    assert OutboxEvent.__table__.c.transaction_pk.unique is True


def test_postgres_urls_use_the_psycopg_driver():
    assert (
        normalize_database_url("postgres://webhook:webhook@db:5432/webhook")
        == "postgresql+psycopg://webhook:webhook@db:5432/webhook"
    )
    assert (
        normalize_database_url("postgresql://webhook:webhook@db/webhook")
        == "postgresql+psycopg://webhook:webhook@db/webhook"
    )
    assert (
        normalize_database_url("postgresql+psycopg://webhook:webhook@db/webhook")
        == "postgresql+psycopg://webhook:webhook@db/webhook"
    )


def test_concurrent_duplicate_requests_create_one_record_and_one_job(session_factory):
    request = WebhookRequest(**payload(transaction_id="txn_concurrent"))
    barrier = threading.Barrier(8)
    duplicates: list[bool] = []
    errors: list[BaseException] = []

    def send() -> None:
        session = session_factory()
        try:
            barrier.wait(timeout=5)
            _transaction, is_duplicate = accept_transaction(session, request)
            duplicates.append(is_duplicate)
        except Exception as exc:
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=send) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    assert duplicates.count(False) == 1
    assert count_rows(session_factory, Transaction) == 1
    assert count_rows(session_factory, OutboxEvent) == 1

    publisher = RecordingPublisher()
    assert dispatch_once(session_factory, publisher) == 1
    assert dispatch_once(session_factory, publisher) == 0
    assert publisher.calls == [("txn_concurrent", 1)]


def test_queue_failure_keeps_the_transaction_and_retries_later(client, session_factory):
    created = client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_queue"))
    assert created.status_code == 202

    assert dispatch_once(session_factory, FailingPublisher()) == 0

    session = session_factory()
    try:
        event = outbox_repository.get_by_transaction_id(session, "txn_queue")
        transaction = transaction_repository.get_by_transaction_id(session, "txn_queue")
        assert event is not None
        assert transaction is not None
        assert event.status == "PENDING"
        assert "redis unavailable" in (event.last_error or "")
        assert transaction.status == "PROCESSING"
        assert transaction.processed_at is None
    finally:
        session.close()

    publisher = RecordingPublisher()
    published = dispatch_once(session_factory, publisher, now=utcnow() + timedelta(seconds=30))
    assert published == 1
    assert publisher.calls == [("txn_queue", 2)]
    assert process_transaction_job("txn_queue") == "processed"
    assert dispatch_once(session_factory, publisher, now=utcnow() + timedelta(hours=2)) == 0
    assert len(publisher.calls) == 1


def test_stale_dispatched_event_is_requeued(client, session_factory):
    client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_stale"))
    session = session_factory()
    try:
        event = outbox_repository.get_by_transaction_id(session, "txn_stale")
        assert event is not None
        event.status = "DISPATCHED"
        event.dispatched_at = utcnow() - timedelta(hours=1)
        event.attempts = 1
        session.commit()
    finally:
        session.close()

    publisher = RecordingPublisher()
    assert dispatch_once(session_factory, publisher) == 1
    assert publisher.calls == [("txn_stale", 2)]


def test_fresh_dispatched_event_is_not_duplicated(client, session_factory):
    client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_fresh"))
    session = session_factory()
    try:
        event = outbox_repository.get_by_transaction_id(session, "txn_fresh")
        assert event is not None
        event.status = "DISPATCHED"
        event.dispatched_at = utcnow()
        event.attempts = 1
        session.commit()
    finally:
        session.close()

    publisher = RecordingPublisher()
    assert dispatch_once(session_factory, publisher) == 0
    assert publisher.calls == []


def test_missing_outbox_row_is_repaired(client, session_factory):
    client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_repair"))
    session = session_factory()
    try:
        event = outbox_repository.get_by_transaction_id(session, "txn_repair")
        assert event is not None
        session.delete(event)
        session.commit()
    finally:
        session.close()

    publisher = RecordingPublisher()
    assert dispatch_once(session_factory, publisher) == 1
    assert publisher.calls[0][0] == "txn_repair"
    assert count_rows(session_factory, OutboxEvent) == 1


def test_failed_commit_does_not_persist_a_transaction(session_factory):
    session = session_factory()

    def fail_commit() -> None:
        raise OperationalError("COMMIT", {}, Exception("disk full"))

    session.commit = fail_commit  # type: ignore[method-assign]
    with pytest.raises(OperationalError):
        transaction_repository.insert_new_with_outbox(
            session,
            transaction_id="txn_commit_fails",
            source_account="acc_user_789",
            destination_account="acc_merchant_456",
            amount=Decimal("1500.00"),
            currency="INR",
        )
    session.close()

    assert count_rows(session_factory, Transaction) == 0
    assert count_rows(session_factory, OutboxEvent) == 0


def test_database_outage_returns_503_and_stores_nothing(client, session_factory, monkeypatch):
    def explode(session, request):
        raise OperationalError("INSERT", {}, Exception("connection lost"))

    monkeypatch.setattr("app.api.routes.webhooks.accept_transaction", explode)
    response = client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_db_down"))

    assert response.status_code == 503
    assert response.json()["detail"] == "Persistent storage is temporarily unavailable"
    assert count_rows(session_factory, Transaction) == 0


def test_celery_task_retries_transient_processor_failures(monkeypatch):
    calls = {"count": 0}

    def fail(transaction_id: str) -> str:
        calls["count"] += 1
        raise RetryableProcessingError("processor unavailable")

    monkeypatch.setattr("app.workers.tasks.process_transaction_job", fail)
    result = process_transaction.apply(args=["txn_celery_retry"], throw=False)

    assert result.result == "retry_exhausted"
    assert calls["count"] == process_transaction.max_retries + 1


def test_amount_column_uses_exact_numeric_storage():
    from app.db.types import MoneyAmount

    column_type = Transaction.__table__.c.amount.type
    assert isinstance(column_type, MoneyAmount)
