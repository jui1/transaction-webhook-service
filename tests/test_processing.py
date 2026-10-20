"""Background processing, retries, and repeat delivery."""

import threading
from datetime import timedelta

import pytest
from app.core.config import get_settings
from app.core.exceptions import RetryableProcessingError
from app.core.time import utcnow
from app.models.transaction import Transaction
from app.repositories import transaction_repository
from app.services.processor import process_transaction_job
from tests.helpers import count_rows, payload


def test_processing_sets_processed_status_and_timestamp(client):
    assert client.post("/v1/webhooks/transactions", json=payload()).status_code == 202
    before = client.get("/v1/transactions/txn_abc123def456").json()
    assert before["status"] == "PROCESSING"
    assert before["processed_at"] is None

    assert process_transaction_job("txn_abc123def456") == "processed"

    after = client.get("/v1/transactions/txn_abc123def456").json()
    assert after["status"] == "PROCESSED"
    assert after["processed_at"] is not None
    assert after["amount"] == 1500

    assert process_transaction_job("txn_abc123def456") == "already_processed"
    repeated = client.get("/v1/transactions/txn_abc123def456").json()
    assert repeated["processed_at"] == after["processed_at"]
    assert repeated["status"] == "PROCESSED"


def test_worker_is_given_the_configured_delay(client, monkeypatch):
    seen: dict[str, float] = {}

    def capture(delay_seconds: float) -> None:
        seen["delay"] = delay_seconds

    monkeypatch.setenv("PROCESSING_DELAY_SECONDS", "30")
    get_settings.cache_clear()
    monkeypatch.setattr("app.services.processor.simulate_external_payment_call", capture)
    client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_delay"))

    assert process_transaction_job("txn_delay") == "processed"
    assert seen["delay"] == 30


def test_retry_and_redelivery_do_not_duplicate_processing(client, session_factory, monkeypatch):
    calls = {"count": 0}

    def flaky(delay_seconds: float) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise RetryableProcessingError("temporary outage")

    monkeypatch.setattr("app.services.processor.simulate_external_payment_call", flaky)
    client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_retry"))

    with pytest.raises(RetryableProcessingError):
        process_transaction_job("txn_retry")

    pending = client.get("/v1/transactions/txn_retry").json()
    assert pending["status"] == "PROCESSING"
    assert pending["processed_at"] is None

    assert process_transaction_job("txn_retry") == "processed"
    assert process_transaction_job("txn_retry") == "already_processed"
    assert calls["count"] == 2
    assert count_rows(session_factory, Transaction) == 1

    done = client.get("/v1/transactions/txn_retry").json()
    assert done["status"] == "PROCESSED"
    assert done["processed_at"] is not None


def test_overlapping_workers_process_once(client, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def block(delay_seconds: float) -> None:
        started.set()
        assert release.wait(timeout=5)

    monkeypatch.setattr("app.services.processor.simulate_external_payment_call", block)
    client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_overlap"))
    outcome: dict[str, str] = {}

    def run() -> None:
        outcome["value"] = process_transaction_job("txn_overlap")

    worker = threading.Thread(target=run)
    worker.start()
    assert started.wait(timeout=5)
    assert process_transaction_job("txn_overlap") == "in_progress"
    release.set()
    worker.join(timeout=5)

    assert outcome["value"] == "processed"
    assert process_transaction_job("txn_overlap") == "already_processed"
    body = client.get("/v1/transactions/txn_overlap").json()
    assert body["status"] == "PROCESSED"


def test_active_lease_prevents_a_second_processor(client, session_factory, monkeypatch):
    def fail_if_called(delay_seconds: float) -> None:
        raise AssertionError("processor ran while another lease was active")

    monkeypatch.setattr("app.services.processor.simulate_external_payment_call", fail_if_called)
    client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_leased"))
    session = session_factory()
    try:
        transaction = transaction_repository.get_by_transaction_id(session, "txn_leased")
        assert transaction is not None
        transaction.processing_token = transaction.id
        transaction.lease_expires_at = utcnow() + timedelta(minutes=5)
        session.commit()
    finally:
        session.close()

    assert process_transaction_job("txn_leased") == "in_progress"
    body = client.get("/v1/transactions/txn_leased").json()
    assert body["status"] == "PROCESSING"
    assert body["processed_at"] is None


def test_expired_lease_can_be_retried_after_a_crash(client, session_factory):
    client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_expired"))
    session = session_factory()
    try:
        transaction = transaction_repository.get_by_transaction_id(session, "txn_expired")
        assert transaction is not None
        transaction.processing_token = transaction.id
        transaction.lease_expires_at = utcnow() - timedelta(seconds=5)
        session.commit()
    finally:
        session.close()

    assert process_transaction_job("txn_expired") == "processed"
    body = client.get("/v1/transactions/txn_expired").json()
    assert body["status"] == "PROCESSED"
    assert body["processed_at"] is not None
