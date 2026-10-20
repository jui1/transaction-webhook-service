"""Real 30 second processing delay. Skipped unless RUN_INTEGRATION_TESTS=1."""

import os
import time

import pytest
from app.core.config import get_settings
from app.services.processor import process_transaction_job
from tests.helpers import payload


@pytest.mark.integration
def test_real_thirty_second_processing(client, monkeypatch):
    if os.environ.get("RUN_INTEGRATION_TESTS") != "1":
        pytest.skip("Set RUN_INTEGRATION_TESTS=1 to run the real 30-second processor test")

    monkeypatch.setenv("PROCESSING_DELAY_SECONDS", "30")
    get_settings.cache_clear()
    created = client.post("/v1/webhooks/transactions", json=payload(transaction_id="txn_slow_30"))
    assert created.status_code == 202
    assert client.get("/v1/transactions/txn_slow_30").json()["processed_at"] is None

    started = time.perf_counter()
    outcome = process_transaction_job("txn_slow_30")
    elapsed = time.perf_counter() - started

    assert outcome == "processed"
    assert elapsed >= 30
    body = client.get("/v1/transactions/txn_slow_30").json()
    assert body["status"] == "PROCESSED"
    assert body["processed_at"] is not None
