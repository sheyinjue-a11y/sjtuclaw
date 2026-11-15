"""Gateway bootstrap and same-origin boundary tests."""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from sjtuclaw.config import ProviderConfiguration, ProviderKind
from sjtuclaw.entrypoints.gateway import COOKIE_NAME, GatewayOptions, create_gateway
from sjtuclaw.entrypoints.gateway_auth import BootstrapTokenRegistry
from sjtuclaw.entrypoints.gateway_streams import _public_payload
from sjtuclaw.infrastructure.persistence import InMemorySessionStore, ProviderSettingsStore
from sjtuclaw.infrastructure.tools.downloads import DownloadSnapshotService
from sjtuclaw.models.llm import LLMRequest, LLMResponse, LLMStreamEvent, LLMUsage
from sjtuclaw.models.runtime import InvocationSource, RuntimeEvent, RuntimeEventType
from sjtuclaw.runtime import SessionCoordinator, SessionService
from sjtuclaw.runtime.provider_runtime import ProviderRuntime
from tests.fakes import FakeClock, FakeIdGenerator


def test_gateway_bootstrap_cookie_and_same_origin_boundary() -> None:
    options = GatewayOptions(port=8765, bootstrap_token="a" * 43)
    client = TestClient(
        create_gateway(
            sessions=_sessions(),
            directory_picker=lambda: r"C:\course-workspace",
            options=options,
        ),
        base_url=options.origin,
    )

    unauthenticated = client.get("/api/v1/sessions")
    assert unauthenticated.status_code == 401
    assert unauthenticated.json()["code"] == "UNAUTHENTICATED"
    assert "requestId" in unauthenticated.json()

    wrong_origin = client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": "http://localhost:8765"},
        json={"token": options.bootstrap_token},
    )
    assert wrong_origin.status_code == 403
    assert wrong_origin.json()["code"] == "INVALID_ORIGIN"

    exchange = client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": options.origin},
        json={"token": options.bootstrap_token},
    )
    assert exchange.status_code == 204
    cookie = exchange.headers["set-cookie"]
    assert f"{COOKIE_NAME}=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Path=/" in cookie

    sessions = client.get("/api/v1/sessions")
    assert sessions.status_code == 200
    assert sessions.json()["sessions"] == []

    cross_origin_create = client.post(
        "/api/v1/sessions",
        headers={"Origin": "http://localhost:8765"},
        json={"title": "不应创建"},
    )
    assert cross_origin_create.status_code == 403
    assert cross_origin_create.json()["code"] == "INVALID_ORIGIN"

    create = client.post(
        "/api/v1/sessions",
        headers={"Origin": options.origin},
        json={"title": "课程"},
    )
    assert create.status_code == 201
    assert create.json()["session"]["title"] == "课程"
    session_id = create.json()["session"]["sessionId"]

    detail = client.get(f"/api/v1/sessions/{session_id}")
    assert detail.status_code == 200
    assert detail.json()["session"]["tokenUsage"] == {
        "promptTokens": 0,
        "promptCacheHitTokens": 0,
        "promptCacheMissTokens": 0,
        "completionTokens": 0,
        "totalTokens": 0,
        "cacheBreakdownAvailable": True,
        "trackingStartedAt": "2026-07-19T00:00:00+00:00",
    }
    assert detail.json()["session"]["contextWindow"] is None

    pick_workspace = client.post(
        f"/api/v1/sessions/{session_id}/workspace/pick",
        headers={"Origin": options.origin},
    )
    assert pick_workspace.status_code == 200
    assert pick_workspace.json() == {"path": r"C:\course-workspace"}

    rename = client.patch(
        f"/api/v1/sessions/{session_id}",
        headers={"Origin": options.origin},
        json={"title": "课程问答"},
    )
    assert rename.status_code == 200
    assert rename.json()["session"]["title"] == "课程问答"

    delete = client.delete(
        f"/api/v1/sessions/{session_id}",
        headers={"Origin": options.origin},
    )
    assert delete.status_code == 409
    assert delete.json()["code"] == "SESSION_NOT_ARCHIVED"
    assert len(client.get("/api/v1/sessions").json()["sessions"]) == 1

    replay = client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": options.origin},
        json={"token": options.bootstrap_token},
    )
    assert replay.status_code == 401
    assert replay.json()["code"] == "INVALID_BOOTSTRAP_TOKEN"


def test_pet_and_browser_cookies_have_separate_capabilities() -> None:
    options = GatewayOptions(bootstrap_token="b" * 43)
    tokens = BootstrapTokenRegistry()
    tokens.issue("browser", token=options.bootstrap_token, ttl_seconds=300)
    pet_token = tokens.issue("pet", token="p" * 43, ttl_seconds=300)
    service = _sessions()
    first = service.create("第一会话")
    second = service.create("第二会话")
    app = create_gateway(sessions=service, options=options, bootstrap_tokens=tokens)

    with (
        TestClient(app, base_url=options.origin) as browser,
        TestClient(app, base_url=options.origin) as pet,
    ):
        browser_exchange = browser.post(
            "/api/v1/auth/bootstrap",
            headers={"Origin": options.origin},
            json={"token": options.bootstrap_token},
        )
        pet_exchange = pet.post(
            "/api/v1/auth/bootstrap",
            headers={"Origin": options.origin},
            json={"token": pet_token},
        )
        assert browser_exchange.status_code == 204
        assert pet_exchange.status_code == 204
        assert pet.post(
            "/api/v1/auth/bootstrap",
            headers={"Origin": options.origin},
            json={"token": pet_token},
        ).status_code == 401
        assert browser.post(
            "/api/v1/auth/bootstrap-token",
            headers={"Origin": options.origin},
            json={"purpose": "browser"},
        ).status_code == 403
        issued = pet.post(
            "/api/v1/auth/bootstrap-token",
            headers={"Origin": options.origin},
            json={"purpose": "browser"},
        )
        assert issued.status_code == 201
        assert issued.json()["purpose"] == "browser"
        assert browser.put(
            "/api/v1/ui/active-session",
            headers={"Origin": options.origin},
            json={"sessionId": second.session_id},
        ).json()["session"]["sessionId"] == second.session_id
        assert pet.get("/api/v1/ui/active-session").json()["session"][
            "sessionId"
        ] == second.session_id
        assert pet.put(
            "/api/v1/ui/active-session",
            headers={"Origin": options.origin},
            json={"sessionId": first.session_id},
        ).status_code == 403
        assert browser.put(
            "/api/v1/ui/active-session",
            json={"sessionId": first.session_id},
        ).status_code == 403
        assert browser.put(
            "/api/v1/ui/active-session",
            headers={"Origin": options.origin},
            json={"sessionId": "missing"},
        ).status_code == 404


def test_gateway_rejects_unexpected_host_with_safe_error() -> None:
    options = GatewayOptions(port=8765, bootstrap_token="b" * 43)
    client = TestClient(
        create_gateway(sessions=_sessions(), options=options),
        base_url=options.origin,
    )

    response = client.get("/api/v1/sessions", headers={"Host": "localhost:8765"})

    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_HOST"
    assert set(response.json()) == {"code", "message", "requestId", "details"}


def test_gateway_serves_only_known_unexpired_same_origin_downloads(tmp_path: Path) -> None:
    options = GatewayOptions(port=8765, bootstrap_token="f" * 43)
    clock = FakeClock(datetime(2026, 7, 19, tzinfo=UTC))
    downloads = DownloadSnapshotService(
        root=tmp_path / "data",
        clock=clock,
        ids=FakeIdGenerator(),
        ttl_seconds=60,
    )
    source = tmp_path / "report.md"
    source.write_text("trusted snapshot", encoding="utf-8")
    snapshot = downloads.publish(
        session_id="session_001",
        source=source,
        filename="report.md",
    )
    client = TestClient(
        create_gateway(
            sessions=_sessions(),
            downloads=downloads,
            options=options,
        ),
        base_url=options.origin,
    )
    _bootstrap(client, options)

    download = client.get(f"/downloads/{snapshot.download_id}")

    assert download.status_code == 200
    assert download.content == b"trusted snapshot"
    assert download.headers["content-disposition"] == 'attachment; filename="report.md"'
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["cache-control"] == "private, no-store"

    unknown = client.get("/downloads/download_unknown")
    assert unknown.status_code == 404
    assert unknown.json()["code"] == "DOWNLOAD_NOT_FOUND"

    clock.current += timedelta(seconds=60)
    expired = client.get(f"/downloads/{snapshot.download_id}")
    assert expired.status_code == 404
    assert expired.json()["code"] == "DOWNLOAD_NOT_FOUND"


def test_gateway_stream_projection_rejects_unsafe_tool_resources() -> None:
    event = RuntimeEvent(
        RuntimeEventType.TOOL_RESULT,
        "session_001",
        "request_001",
        1,
        InvocationSource.GATEWAY,
        {
            "call_id": "call_001",
            "tool": "create_download",
            "status": "success",
            "content": "snapshot created",
            "error_code": None,
            "structured_content": {"filename": "report.md"},
            "resources": (
                {
                    "kind": "download",
                    "name": "absolute.md",
                    "href": "https://evil.example/download_001",
                    "mime_type": "text/markdown",
                    "expires_at": None,
                },
                {
                    "kind": "download",
                    "name": "authority.md",
                    "href": "//evil.example/downloads/download_002",
                    "mime_type": "text/markdown",
                    "expires_at": None,
                },
                {
                    "kind": "download",
                    "name": "wrong-path.md",
                    "href": "/files/download_003",
                    "mime_type": "text/markdown",
                    "expires_at": None,
                },
            ),
        },
    )

    projected = _public_payload(event)

    assert projected["resources"] == []
    serialized = json.dumps(projected)
    assert "evil.example" not in serialized
    assert "/files/download_003" not in serialized


def test_gateway_hides_unexpected_service_error() -> None:
    options = GatewayOptions(port=8765, bootstrap_token="c" * 43)
    client = TestClient(
        create_gateway(
            sessions=_FailingSessionService(),  # type: ignore[arg-type]
            options=options,
        ),
        base_url=options.origin,
        raise_server_exceptions=False,
    )
    _bootstrap(client, options)

    response = client.get("/api/v1/sessions")

    assert response.status_code == 500
    assert response.json()["code"] == "INTERNAL_ERROR"
    assert "RuntimeError" not in response.text


def test_gateway_switches_provider_without_returning_or_requiring_saved_key(
    tmp_path: Path,
) -> None:
    options = GatewayOptions(port=8765, bootstrap_token="d" * 43)
    providers = _providers()
    store = ProviderSettingsStore(tmp_path, protector=_Protector())
    client = TestClient(
        create_gateway(
            sessions=_sessions(),
            providers=providers,
            provider_settings=store,
            options=options,
        ),
        base_url=options.origin,
    )
    _bootstrap(client, options)

    initial = client.get("/api/v1/settings/provider")

    assert initial.status_code == 200
    assert initial.json()["provider"] == {
        "provider": "openai_compatible",
        "model": "first-model",
        "baseUrl": "https://api.example/v1",
        "keySaved": True,
        "tokenCounterMode": "approximate",
        "thinkingEnabled": True,
    }
    assert "initial-secret" not in initial.text

    updated = client.put(
        "/api/v1/settings/provider",
        headers={"Origin": options.origin},
        json={
            "provider": "anthropic",
            "model": "claude-test",
            "baseUrl": "https://anthropic.example",
            "thinkingEnabled": False,
        },
    )

    assert updated.status_code == 200
    assert updated.json()["provider"]["provider"] == "anthropic"
    assert updated.json()["provider"]["tokenCounterMode"] == "approximate"
    assert updated.json()["provider"]["thinkingEnabled"] is False
    assert "initial-secret" not in updated.text
    assert providers.configuration.api_key == "initial-secret"
    assert providers.configuration.thinking_enabled is False
    assert store.load() == providers.configuration

    legacy_updated = client.put(
        "/api/v1/settings/provider",
        headers={"Origin": options.origin},
        json={
            "provider": "anthropic",
            "model": "claude-test",
            "baseUrl": "https://anthropic.example",
        },
    )

    assert legacy_updated.status_code == 200
    assert legacy_updated.json()["provider"]["thinkingEnabled"] is False
    assert providers.configuration.thinking_enabled is False


def test_gateway_provider_save_failure_discards_prepared_runtime(
    tmp_path: Path,
) -> None:
    options = GatewayOptions(port=8765, bootstrap_token="e" * 43)
    clients: list[_ProviderClient] = []

    def create_client(_: ProviderConfiguration) -> _ProviderClient:
        client = _ProviderClient()
        clients.append(client)
        return client

    initial = ProviderConfiguration(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        model="first-model",
        base_url="https://api.example/v1",
        api_key="initial-secret",
    )
    providers = ProviderRuntime(
        initial,
        create_client=create_client,
        create_token_counter=lambda _: _Counter(),
    )
    persisted = ProviderSettingsStore(tmp_path, protector=_Protector())
    persisted.save(initial)
    client = TestClient(
        create_gateway(
            sessions=_sessions(),
            providers=providers,
            provider_settings=_FailingProviderStore(persisted),  # type: ignore[arg-type]
            options=options,
        ),
        base_url=options.origin,
        raise_server_exceptions=False,
    )
    _bootstrap(client, options)

    response = client.put(
        "/api/v1/settings/provider",
        headers={"Origin": options.origin},
        json={
            "provider": "anthropic",
            "model": "broken-save",
            "baseUrl": "https://anthropic.example",
        },
    )

    assert response.status_code == 500
    assert providers.configuration == initial
    assert persisted.load() == initial
    assert len(clients) == 2
    assert clients[0].closed is False
    assert clients[1].closed is True


class _ProviderClient:
    def __init__(self) -> None:
        self.closed = False

    async def complete(self, _: LLMRequest) -> LLMResponse:
        return LLMResponse("", LLMUsage(0, 0, 0), "stop")

    async def stream(self, _: LLMRequest) -> AsyncIterator[LLMStreamEvent]:
        if False:
            yield LLMStreamEvent(response=LLMResponse("", LLMUsage(0, 0, 0), "stop"))

    async def aclose(self) -> None:
        self.closed = True


class _Counter:
    def count(self, _: LLMRequest) -> int:
        return 1


class _Protector:
    def protect(self, plaintext: bytes) -> bytes:
        return b"protected:" + plaintext

    def unprotect(self, ciphertext: bytes) -> bytes:
        return ciphertext.removeprefix(b"protected:")


class _FailingProviderStore:
    def __init__(self, persisted: ProviderSettingsStore) -> None:
        self.persisted = persisted

    def save(self, _configuration: ProviderConfiguration) -> None:
        raise OSError("disk full")


def _providers() -> ProviderRuntime:
    configuration = ProviderConfiguration(
        provider=ProviderKind.OPENAI_COMPATIBLE,
        model="first-model",
        base_url="https://api.example/v1",
        api_key="initial-secret",
    )
    return ProviderRuntime(
        configuration,
        create_client=lambda _: _ProviderClient(),
        create_token_counter=lambda _: _Counter(),
    )


def _sessions() -> SessionService:
    clock = FakeClock(datetime(2026, 7, 19, tzinfo=UTC))
    return SessionService(
        sessions=InMemorySessionStore(),
        coordinator=SessionCoordinator(),
        clock=clock,
        ids=FakeIdGenerator(),
    )


class _FailingSessionService:
    def list(self) -> tuple[object, ...]:
        raise RuntimeError("secret internal detail")


def _bootstrap(client: TestClient, options: GatewayOptions) -> None:
    response = client.post(
        "/api/v1/auth/bootstrap",
        headers={"Origin": options.origin},
        json={"token": options.bootstrap_token},
    )
    assert response.status_code == 204
