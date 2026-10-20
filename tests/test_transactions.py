"""Transaction lookup."""

from tests.helpers import payload

RESPONSE_FIELDS = {
    "transaction_id",
    "source_account",
    "destination_account",
    "amount",
    "currency",
    "status",
    "created_at",
    "processed_at",
}


def test_unknown_transaction_returns_404(client):
    response = client.get("/v1/transactions/txn_does_not_exist")

    assert response.status_code == 404
    assert response.json()["detail"] == "Transaction not found"


def test_new_transaction_is_processing_and_processed_at_is_null(client):
    created = client.post(
        "/v1/webhooks/transactions",
        json=payload(transaction_id="txn_query", amount=150.5, currency="USD"),
    )
    assert created.status_code == 202

    response = client.get("/v1/transactions/txn_query")
    body = response.json()

    assert response.status_code == 200
    assert set(body) == RESPONSE_FIELDS
    assert body["transaction_id"] == "txn_query"
    assert body["source_account"] == "acc_user_789"
    assert body["destination_account"] == "acc_merchant_456"
    assert body["amount"] == 150.5
    assert body["currency"] == "USD"
    assert body["status"] == "PROCESSING"
    assert body["processed_at"] is None
    assert body["created_at"].endswith("Z")
