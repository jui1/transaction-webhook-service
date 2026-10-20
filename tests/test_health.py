"""Health check."""

from datetime import datetime

from tests.helpers import payload


def test_health_endpoint_returns_healthy(client):
    response = client.get("/")

    assert response.status_code == 200
    body = response.json()
    assert body == {
        "status": "HEALTHY",
        "current_time": body["current_time"],
    }
    assert body["status"] == "HEALTHY"
    parsed = datetime.fromisoformat(body["current_time"].replace("Z", "+00:00"))
    assert parsed.tzinfo is not None
    assert body["current_time"].endswith("Z")


def test_health_does_not_require_a_transaction(client):
    assert client.get("/").status_code == 200
    assert client.post("/v1/webhooks/transactions", json=payload()).status_code == 202
