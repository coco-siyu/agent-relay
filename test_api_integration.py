"""Black-box task-flow test for a running Agent Relay API.

Set ``RELAY_INTEGRATION_BASE_URL`` to opt in. The test deliberately imports no
application or database modules, so it exercises the real HTTP and persistence
boundaries used by Docker Compose and Kubernetes deployments.
"""

from __future__ import annotations

import os
import uuid

import httpx
import pytest


BASE_URL = os.getenv("RELAY_INTEGRATION_BASE_URL")
pytestmark = pytest.mark.skipif(not BASE_URL, reason="RELAY_INTEGRATION_BASE_URL is not set")


def test_two_agents_exchange_task_and_result() -> None:
    run_id = uuid.uuid4().hex
    with httpx.Client(base_url=BASE_URL or "", timeout=10) as client:
        ready = client.get("/ready")
        assert ready.status_code == 200
        assert ready.json() == {"status": "ready"}

        sender_response = client.post("/api/v1/agents", json={"name": f"integration-sender-{run_id}"})
        recipient_response = client.post("/api/v1/agents", json={"name": f"integration-recipient-{run_id}"})
        assert sender_response.status_code == 201
        assert recipient_response.status_code == 201
        sender = sender_response.json()
        recipient = recipient_response.json()
        sender_headers = {"Authorization": f"Bearer {sender['token']}"}
        recipient_headers = {"Authorization": f"Bearer {recipient['token']}"}

        created = client.post(
            "/api/v1/tasks",
            headers={**sender_headers, "Idempotency-Key": f"integration-{run_id}"},
            json={"to": recipient["agent_id"], "input": "integration task"},
        )
        assert created.status_code == 201
        assert created.json()["status"] == "queued"
        task_id = created.json()["task_id"]

        claimed = client.post(
            "/api/v1/tasks/claim",
            headers=recipient_headers,
            json={"worker_id": "integration-worker", "wait_seconds": 0},
        )
        assert claimed.status_code == 200
        claim = claimed.json()
        assert claim["task_id"] == task_id
        assert claim["input"] == "integration task"
        assert claim["attempt"] == 1

        completed = client.post(
            f"/api/v1/tasks/{task_id}/complete",
            headers=recipient_headers,
            json={"claim_token": claim["claim_token"], "output": "INTEGRATION TASK"},
        )
        assert completed.status_code == 200
        assert completed.json() == {"task_id": task_id, "status": "completed"}

        result = client.get(f"/api/v1/tasks/{task_id}", headers=sender_headers)
        assert result.status_code == 200
        task = result.json()
        assert task["status"] == "completed"
        assert task["output"] == "INTEGRATION TASK"
        assert task["attempt_count"] == 1

        attempts = client.get(f"/api/v1/tasks/{task_id}/attempts", headers=sender_headers)
        assert attempts.status_code == 200
        assert attempts.json()["items"][0]["outcome"] == "completed"
