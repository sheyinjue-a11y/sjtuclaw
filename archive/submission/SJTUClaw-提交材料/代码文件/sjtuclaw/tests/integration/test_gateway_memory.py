"""Authenticated Memory and Memory Policy Gateway contracts."""

from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from sjtuclaw.entrypoints.gateway import GatewayOptions, create_gateway
from sjtuclaw.infrastructure.persistence import (
    InMemorySessionStore,
    JsonMemoryPolicyStore,
    JsonMemoryStore,
)
from sjtuclaw.runtime import (
    MemoryCoordinator,
    MemoryPolicyService,
    MemoryService,
    SessionCoordinator,
    SessionService,
)
from tests.fakes import FakeClock, FakeIdGenerator

NOW = datetime(2026, 7, 28, tzinfo=UTC)


def _client(tmp_path: Path) -> tuple[TestClient, GatewayOptions]:
    options = GatewayOptions(port=8765, bootstrap_token="m" * 43)
    sessions = SessionService(
        sessions=InMemorySessionStore(),
        coordinator=SessionCoordinator(),
        clock=FakeClock(NOW),
        ids=FakeIdGenerator(),
    )
    memories = MemoryService(JsonMemoryStore(tmp_path), MemoryCoordinator(), clock=lambda: NOW, new_id=lambda: "memory_001")
    policy = MemoryPolicyService(JsonMemoryPolicyStore(tmp_path), clock=lambda: NOW)
    client = TestClient(create_gateway(sessions=sessions, memories=memories, memory_policy=policy, options=options), base_url=options.origin)
    assert client.post("/api/v1/auth/bootstrap", headers={"Origin": options.origin}, json={"token": options.bootstrap_token}).status_code == 204
    return client, options


def test_memory_crud_and_policy_are_authenticated_and_camel_case(tmp_path: Path) -> None:
    client, options = _client(tmp_path)
    headers = {"Origin": options.origin}
    created = client.post("/api/v1/memories", headers=headers, json={"content": "用户偏好中文回答", "category": "preference", "originalTimeText": None, "eventAt": None, "eventTimezone": None})
    assert created.status_code == 201
    entry = created.json()
    assert entry["sourceKind"] == "manual"
    assert entry["importance"] == 3
    listed = client.get("/api/v1/memories?query=中文&limit=50")
    assert listed.json()["items"][0]["memoryId"] == entry["memoryId"]
    changed = client.patch(f"/api/v1/memories/{entry['memoryId']}", headers=headers, json={"revision": entry["revision"], "content": "用户偏好简洁中文回答", "category": "preference", "originalTimeText": None, "eventAt": None, "eventTimezone": None})
    assert changed.status_code == 200
    conflict = client.delete(f"/api/v1/memories/{entry['memoryId']}?revision=1", headers=headers)
    assert conflict.status_code == 409
    assert conflict.json()["code"] == "MEMORY_REVISION_CONFLICT"
    assert client.delete(f"/api/v1/memories/{entry['memoryId']}?revision=2", headers=headers).status_code == 204
    default = client.get("/api/v1/memory-policy")
    assert default.status_code == 200
    assert default.json()["isOverride"] is False
    assert isinstance(default.json()["defaultRules"], str)
    assert default.json()["revision"] == 1
    updated = client.put("/api/v1/memory-policy", headers=headers, json={"revision": 1, "rules": "只保存用户确认的长期偏好。"})
    assert updated.status_code == 200 and updated.json()["isOverride"] is True
    assert client.post("/api/v1/memory-policy/reset", headers=headers, json={"revision": 2}).json()["isOverride"] is False


def test_memory_api_rejects_secret_and_unknown_fields(tmp_path: Path) -> None:
    client, options = _client(tmp_path)
    response = client.post("/api/v1/memories", headers={"Origin": options.origin}, json={"content": "api_key=secret-value", "category": "other", "extra": True})
    assert response.status_code == 422
    assert "secret-value" not in response.text
