"""Webhook responses, validation, duplicates, and response time."""

import time

from app.models.outbox import OutboxEvent
from app.models.transaction import Transaction
from tests.helpers import count_rows, payload


def test_valid_webhook_returns_202(client):
    response = client.post("/v1/webhooks/transactions", json=payload())

    assert response.status_code == 202
    assert response.json() == {
        "status": "accepted",
        "transaction_id": "txn_abc123def456",
        "message": "Transaction accepted for processing",
    }


def test_invalid_payload_returns_422(client):
    missing = client.post("/v1/webhooks/transactions", json={})
    negative = client.post("/v1/webhooks/transactions", json=payload(amount=-1))
    zero = client.post("/v1/webhooks/transactions", json=payload(amount=0))
    currency = client.post("/v1/webhooks/transactions", json=payload(currency="inr"))
    too_precise = client.post("/v1/webhooks/transactions", json=payload(amount="10.999"))

    assert missing.status_code == 422
    assert negative.status_code == 422
    assert zero.status_code == 422
    assert currency.status_code == 422
    assert too_precise.status_code == 422
    assert "detail" in missing.json()


def test_duplicate_webhook_creates_one_transaction_and_one_outbox(client, session_factory):
    first = client.post("/v1/webhooks/transactions", json=payload())
    second = client.post("/v1/webhooks/transactions", json=payload())

    assert first.status_code == 202
    assert first.json()["status"] == "accepted"
    assert second.status_code == 202
    assert second.json()["status"] == "duplicate"
    assert second.json()["transaction_id"] == "txn_abc123def456"
    assert count_rows(session_factory, Transaction) == 1
    assert count_rows(session_factory, OutboxEvent) == 1


def test_webhook_responds_within_500ms_without_processing(client, monkeypatch):
    monkeypatch.setenv("PROCESSING_DELAY_SECONDS", "30")
    from app.core.config import get_settings

    get_settings.cache_clear()

    def fail_if_called(delay_seconds: float) -> None:
        raise AssertionError("processor ran during the webhook request")

    monkeypatch.setattr("app.services.processor.simulate_external_payment_call", fail_if_called)
    started = time.perf_counter()
    response = client.post("/v1/webhooks/transactions", json=payload())
    elapsed = time.perf_counter() - started

    assert response.status_code == 202
    assert elapsed < 0.5
    body = client.get("/v1/transactions/txn_abc123def456").json()
    assert body["status"] == "PROCESSING"
    assert body["processed_at"] is None
