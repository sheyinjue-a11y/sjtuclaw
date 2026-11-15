"""Loopback-only FastAPI Gateway security boundary."""

from __future__ import annotations

import asyncio
import hmac
import json
import secrets
import webbrowser
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path
from typing import Literal, Protocol
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field

from sjtuclaw.bootstrap import build_cli_application
from sjtuclaw.config import ModelProfile, ProviderConfiguration, ProviderKind, Settings
from sjtuclaw.entrypoints.gateway_auth import BootstrapTokenRegistry
from sjtuclaw.entrypoints.gateway_streams import GatewayEventHub, GatewaySubscription
from sjtuclaw.entrypoints.pet_launcher import PetLauncher
from sjtuclaw.errors import AttachmentError, SkillError, StoreError
from sjtuclaw.infrastructure.persistence import ProviderSettingsStore
from sjtuclaw.infrastructure.tools.downloads import (
    DownloadServiceError,
    DownloadSnapshotService,
)
from sjtuclaw.models.attachment import Attachment
from sjtuclaw.models.memory import CreateMemory, MemoryCategory, UpdateMemory
from sjtuclaw.models.message import Message
from sjtuclaw.models.runtime import InvocationSource, RuntimeRequest, UserInput
from sjtuclaw.models.scheduler import (
    Occurrence,
    Schedule,
    ScheduleKind,
    Task,
    TaskStatus,
)
from sjtuclaw.models.session import (
    ApprovalDecisionStatus,
    PendingRequest,
    RunState,
    RunStatus,
    Session,
    WorkspaceBinding,
)
from sjtuclaw.models.skill import SkillManifest, SkillReference, SkillUsage
from sjtuclaw.models.tool import ToolResourceLink, mutable_json
from sjtuclaw.runtime import (
    ArchiveService,
    AttachmentService,
    CommandDescriptor,
    CommandOutcome,
    CommandService,
    CompactionService,
    RuntimeHost,
    SchedulerService,
    SessionService,
    SkillService,
    WorkspaceService,
)
from sjtuclaw.runtime.active_session import ActiveSessionCoordinator
from sjtuclaw.runtime.memory_policy import MemoryPolicyRevisionConflict, MemoryPolicyService
from sjtuclaw.runtime.memory_service import MemoryRevisionConflict, MemoryService
from sjtuclaw.runtime.prompt_configuration import PromptConfigurationService, PromptSnapshot
from sjtuclaw.runtime.provider_runtime import ProviderRuntime
from sjtuclaw.runtime.session_state import SessionArchivedError, require_session_mutable
from sjtuclaw.runtime.workspace_service import WorkspaceServiceError

COOKIE_NAME = "sjtuclaw_session"
CLIENT_COOKIE_NAME = "sjtuclaw_client"
API_PREFIX = "/api/v1"


@dataclass(frozen=True, slots=True)
class GatewayOptions:
    """Process-local Gateway settings; non-loopback hosts are forbidden."""

    host: str = "127.0.0.1"
    port: int = 8765
    bootstrap_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))

    def __post_init__(self) -> None:
        if self.host != "127.0.0.1":
            raise ValueError("Gateway must bind only to 127.0.0.1")
        if not 1 <= self.port <= 65_535:
            raise ValueError("Gateway port must be between 1 and 65535")
        if len(self.bootstrap_token) < 43:
            raise ValueError("bootstrap token must contain at least 256 bits of entropy")

    @property
    def origin(self) -> str:
        return f"http://{self.host}:{self.port}"

    @property
    def bootstrap_url(self) -> str:
        """Return the fragment-only URL that can exchange this process's token."""

        return f"{self.origin}/#bootstrap={self.bootstrap_token}"

    @property
    def allowed_hosts(self) -> frozenset[str]:
        return frozenset({self.host, f"{self.host}:{self.port}"})


class BootstrapBody(BaseModel):
    token: str = Field(min_length=1, max_length=256)


class ActiveSessionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=256, alias="sessionId")


class BootstrapTokenBody(BaseModel):
    purpose: Literal["browser"]


class CreateSessionBody(BaseModel):
    title: str = Field(default="新会话", min_length=1, max_length=200)


class RenameSessionBody(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class SubmitRequestBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    text: str = Field(min_length=1, max_length=100_000)
    attachment_refs: list[str] = Field(
        default_factory=list,
        min_length=0,
        max_length=10,
        alias="attachmentRefs",
    )
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        alias="idempotencyKey",
    )


class ExecuteCommandBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    command: str = Field(min_length=1, max_length=100_000)
    idempotency_key: str | None = Field(
        default=None,
        min_length=1,
        max_length=200,
        alias="idempotencyKey",
    )


class SteerBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    steer_id: str = Field(min_length=1, max_length=128, alias="steerId")
    idempotency_key: str = Field(min_length=1, max_length=200, alias="idempotencyKey")
    text: str = Field(min_length=1, max_length=100_000)


class SteerQueuedBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    steer_id: str = Field(min_length=1, max_length=128, alias="steerId")
    idempotency_key: str = Field(min_length=1, max_length=200, alias="idempotencyKey")


class WorkspaceBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root: str = Field(min_length=1, max_length=2_000)


class ProviderSettingsBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    provider: ProviderKind
    model: str = Field(min_length=1, max_length=200)
    base_url: str = Field(min_length=1, max_length=2_000, alias="baseUrl")
    api_key: str | None = Field(default=None, max_length=2_000, alias="apiKey")
    thinking_enabled: bool | None = Field(default=None, alias="thinkingEnabled")


class SoulSettingsBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content: str = Field(max_length=50_000)


class ApprovalDecisionBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    decision: Literal["approve", "deny"]
    idempotency_key: str = Field(min_length=1, max_length=200, alias="idempotencyKey")
    reason: str | None = Field(default=None, max_length=1_000)


class CreateMemoryBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    content: str = Field(min_length=1, max_length=4_000)
    category: MemoryCategory = MemoryCategory.OTHER
    original_time_text: str | None = Field(default=None, alias="originalTimeText")
    event_at: datetime | None = Field(default=None, alias="eventAt")
    event_timezone: str | None = Field(default=None, max_length=100, alias="eventTimezone")


class UpdateMemoryBody(CreateMemoryBody):
    revision: int = Field(ge=1)


class UpdateMemoryPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)
    rules: str = Field(min_length=1, max_length=12_000)


class ResetMemoryPolicyBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    revision: int = Field(ge=1)


class TaskScheduleBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    kind: ScheduleKind
    anchor_at: datetime = Field(alias="anchorAt")
    interval_seconds: int | None = Field(default=None, alias="intervalSeconds")
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    weekdays: list[int] = Field(default_factory=list, max_length=7)

    def to_domain(self) -> Schedule:
        return Schedule(
            kind=self.kind,
            anchor_at=self.anchor_at,
            interval_seconds=self.interval_seconds,
            timezone=self.timezone,
            hour=self.hour,
            minute=self.minute,
            weekdays=tuple(self.weekdays),
        )


class CreateTaskBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    session_id: str = Field(min_length=1, max_length=128, alias="sessionId")
    instruction: str = Field(min_length=1, max_length=100_000)
    schedule: TaskScheduleBody
    workspace_binding_revision: int | None = Field(
        default=None, ge=1, alias="workspaceBindingRevision"
    )
    managed_permissions: list[str] = Field(
        default_factory=list, max_length=3, alias="managedPermissions"
    )
    skill_refs: list[str] = Field(default_factory=list, max_length=3, alias="skillRefs")


def create_gateway(
    *,
    sessions: SessionService,
    bootstrap_tokens: BootstrapTokenRegistry | None = None,
    active_sessions: ActiveSessionCoordinator | None = None,
    runtime: RuntimeHost | None = None,
    attachments: AttachmentService | None = None,
    scheduler: SchedulerService | None = None,
    archive: ArchiveService | None = None,
    workspace: WorkspaceService | None = None,
    downloads: DownloadSnapshotService | None = None,
    skills: SkillService | None = None,
    compaction: CompactionService | None = None,
    commands: CommandService | None = None,
    model_profile: ModelProfile | None = None,
    providers: ProviderRuntime | None = None,
    provider_settings: ProviderSettingsStore | None = None,
    memories: MemoryService | None = None,
    memory_policy: MemoryPolicyService | None = None,
    prompts: PromptConfigurationService | None = None,
    directory_picker: Callable[[], str | None] | None = None,
    options: GatewayOptions | None = None,
) -> FastAPI:
    """Create one Gateway process with process-local bootstrap and cookie state."""

    gateway_options = options or GatewayOptions()
    session_cookie = secrets.token_urlsafe(32)
    token_registry = bootstrap_tokens or BootstrapTokenRegistry()
    if bootstrap_tokens is None:
        token_registry.issue(
            "browser", token=gateway_options.bootstrap_token, ttl_seconds=300
        )
    active_session_state = active_sessions or ActiveSessionCoordinator(sessions)
    event_hub = GatewayEventHub()
    pick_directory = directory_picker or _pick_local_directory

    def session_snapshot(session: Session) -> dict[str, object]:
        return _session_snapshot(
            session,
            compaction=compaction,
            model_profile=model_profile,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await event_hub.aclose()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)

    def request_id(request: Request) -> str:
        return getattr(request.state, "request_id", "unknown")

    def error_response(
        request: Request,
        *,
        status_code: int,
        code: str,
        message: str,
        details: dict[str, object] | None = None,
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status_code,
            content={
                "code": code,
                "message": message,
                "requestId": request_id(request),
                "details": details or {},
            },
        )

    @app.middleware("http")
    async def validate_host(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        request.state.request_id = uuid4().hex
        host = request.headers.get("host", "").lower()
        if host not in gateway_options.allowed_hosts:
            return error_response(
                request,
                status_code=status.HTTP_400_BAD_REQUEST,
                code="INVALID_HOST",
                message="请求主机无效",
            )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id(request)
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; base-uri 'none'; frame-ancestors 'none'; "
            "form-action 'self'; object-src 'none'; script-src 'self'; style-src 'self'; "
            "connect-src 'self'; img-src 'self' data:",
        )
        return response

    @app.exception_handler(HTTPException)
    async def handle_http_error(request: Request, exc: HTTPException) -> JSONResponse:
        code, message = (
            exc.detail
            if isinstance(exc.detail, tuple)
            else ("REQUEST_REJECTED", "请求被拒绝")
        )
        return error_response(
            request,
            status_code=exc.status_code,
            code=code,
            message=message,
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(
        request: Request,
        _exc: RequestValidationError,
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="INVALID_REQUEST",
            message="请求格式无效",
        )

    @app.exception_handler(AttachmentError)
    async def handle_attachment_error(
        request: Request,
        exc: AttachmentError,
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=status.HTTP_400_BAD_REQUEST,
            code=exc.code,
            message="附件请求无效",
        )

    @app.exception_handler(SessionArchivedError)
    async def handle_session_archived(
        request: Request,
        _exc: SessionArchivedError,
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=status.HTTP_409_CONFLICT,
            code="SESSION_ARCHIVED",
            message="archived sessions are read-only",
        )

    @app.exception_handler(DownloadServiceError)
    async def handle_download_error(
        request: Request,
        exc: DownloadServiceError,
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=status.HTTP_404_NOT_FOUND,
            code=exc.code,
            message="下载资源不可用",
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(
        request: Request,
        _exc: Exception,
    ) -> JSONResponse:
        return error_response(
            request,
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            code="INTERNAL_ERROR",
            message="服务内部错误",
        )

    def require_same_origin(request: Request) -> None:
        if request.headers.get("origin") != gateway_options.origin:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=("INVALID_ORIGIN", "请求来源无效"),
            )

    def require_same_origin_stream(request: Request) -> None:
        if request.headers.get("origin") is not None:
            require_same_origin(request)
            return
        if request.headers.get("sec-fetch-site") != "same-origin":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=("INVALID_ORIGIN", "请求来源无效"),
            )

    def require_cookie(request: Request) -> None:
        session_token = request.cookies.get(COOKIE_NAME)
        if session_token is None or not hmac.compare_digest(session_token, session_cookie):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=("UNAUTHENTICATED", "需要本地会话凭证"),
            )

    def require_client_cookie(request: Request, purpose: Literal["browser", "pet"]) -> None:
        require_cookie(request)
        client_purpose = request.cookies.get(CLIENT_COOKIE_NAME)
        if client_purpose is None or not hmac.compare_digest(client_purpose, purpose):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=("CLIENT_CAPABILITY_FORBIDDEN", "客户端权限不足"),
            )

    def require_browser_cookie(request: Request) -> None:
        require_client_cookie(request, "browser")

    def require_pet_cookie(request: Request) -> None:
        require_client_cookie(request, "pet")

    def require_runtime() -> RuntimeHost:
        if runtime is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("RUNTIME_UNAVAILABLE", "运行服务尚未就绪"),
            )
        return runtime

    def require_attachments() -> AttachmentService:
        if attachments is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("ATTACHMENTS_UNAVAILABLE", "附件服务尚未就绪"),
            )
        return attachments

    def require_scheduler() -> SchedulerService:
        if scheduler is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("SCHEDULER_UNAVAILABLE", "定时任务服务尚未就绪"),
            )
        return scheduler

    def require_archive() -> ArchiveService:
        if archive is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("ARCHIVE_UNAVAILABLE", "archive service is unavailable"),
            )
        return archive

    def require_workspace() -> WorkspaceService:
        if workspace is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("WORKSPACE_UNAVAILABLE", "工作区服务尚未就绪"),
            )
        return workspace

    def require_downloads() -> DownloadSnapshotService:
        if downloads is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("DOWNLOADS_UNAVAILABLE", "下载服务尚未就绪"),
            )
        return downloads

    def require_skills() -> SkillService:
        if skills is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("SKILLS_UNAVAILABLE", "local Skill service is unavailable"),
            )
        return skills

    def require_commands() -> CommandService:
        if commands is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("COMMANDS_UNAVAILABLE", "local command service is unavailable"),
            )
        return commands

    def require_providers() -> ProviderRuntime:
        if providers is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("PROVIDER_SETTINGS_UNAVAILABLE", "provider settings are unavailable"),
            )
        return providers

    def require_provider_settings() -> ProviderSettingsStore:
        if provider_settings is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("PROVIDER_SETTINGS_UNAVAILABLE", "provider settings are unavailable"),
            )
        return provider_settings

    def require_memories() -> MemoryService:
        if memories is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=("MEMORY_STORE_UNAVAILABLE", "memory service is unavailable"))
        return memories

    def require_memory_policy() -> MemoryPolicyService:
        if memory_policy is None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=("MEMORY_STORE_UNAVAILABLE", "memory policy is unavailable"))
        return memory_policy

    def require_prompts() -> PromptConfigurationService:
        if prompts is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("PROMPT_SETTINGS_UNAVAILABLE", "Prompt 配置服务尚未就绪"),
            )
        return prompts

    def load_session_or_404(session_id: str) -> Session:
        try:
            return sessions.load(session_id)
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("SESSION_NOT_FOUND", "会话不存在"),
            ) from exc

    def select_active_session_or_error(session_id: str) -> Session:
        try:
            return active_session_state.select(session_id)
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("SESSION_NOT_FOUND", "会话不存在"),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("SESSION_ARCHIVED", "archived sessions are read-only"),
            ) from exc

    @app.post(f"{API_PREFIX}/auth/bootstrap", status_code=status.HTTP_204_NO_CONTENT)
    async def bootstrap(body: BootstrapBody, request: Request, response: Response) -> None:
        require_same_origin(request)
        purpose = token_registry.consume(body.token)
        if purpose is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail=("INVALID_BOOTSTRAP_TOKEN", "引导凭证无效或已使用"),
            )
        response.set_cookie(
            key=COOKIE_NAME,
            value=session_cookie,
            httponly=True,
            samesite="strict",
            path="/",
            secure=False,
        )
        response.set_cookie(
            key=CLIENT_COOKIE_NAME,
            value=purpose,
            httponly=True,
            samesite="strict",
            path="/",
            secure=False,
        )

    @app.post(f"{API_PREFIX}/auth/bootstrap-token", status_code=status.HTTP_201_CREATED)
    async def issue_bootstrap_token(
        body: BootstrapTokenBody,
        request: Request,
        _pet: None = Depends(require_pet_cookie),
    ) -> dict[str, object]:
        require_same_origin(request)
        token = token_registry.issue(body.purpose, ttl_seconds=30)
        return {"token": token, "purpose": body.purpose, "expiresInSeconds": 30}

    @app.get(f"{API_PREFIX}/ui/active-session")
    async def get_active_session(
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, object]:
        return {"session": _session_summary(active_session_state.resolve())}

    @app.put(f"{API_PREFIX}/ui/active-session")
    async def put_active_session(
        body: ActiveSessionBody,
        request: Request,
        _browser: None = Depends(require_browser_cookie),
    ) -> dict[str, object]:
        require_same_origin(request)
        return {"session": _session_summary(select_active_session_or_error(body.session_id))}

    @app.get(f"{API_PREFIX}/sessions")
    async def list_sessions(
        archived: bool = False,
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, list[dict[str, object]]]:
        return {"sessions": [_session_summary(session) for session in sessions.list(archived=archived)]}

    @app.get(f"{API_PREFIX}/memories")
    async def list_memories(query: str = "", cursor: str | None = None, limit: int = Query(50, ge=1, le=100), service: MemoryService = Depends(require_memories), _authenticated: None = Depends(require_cookie)) -> dict[str, object]:  # noqa: B008
        try:
            page = service.list_page(query=query, cursor=cursor, limit=limit)
        except StoreError as exc:
            raise HTTPException(status_code=503, detail=("MEMORY_STORE_UNAVAILABLE", "memory store is unavailable")) from exc
        return {"items": [_memory_payload(item) for item in page.entries], "nextCursor": page.next_cursor}

    @app.post(f"{API_PREFIX}/memories", status_code=status.HTTP_201_CREATED)
    async def create_memory(body: CreateMemoryBody, request: Request, service: MemoryService = Depends(require_memories), _authenticated: None = Depends(require_cookie)) -> dict[str, object]:  # noqa: B008
        require_same_origin(request)
        try:
            entry = service.create_manual(CreateMemory(body.content, body.category, 3, body.original_time_text, body.event_at, body.event_timezone))
        except StoreError as exc:
            raise HTTPException(status_code=503, detail=("MEMORY_STORE_UNAVAILABLE", "memory store is unavailable")) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=("INVALID_MEMORY", "memory is invalid")) from exc
        return _memory_payload(entry)

    @app.patch(f"{API_PREFIX}/memories/{{memory_id}}")
    async def update_memory(memory_id: str, body: UpdateMemoryBody, request: Request, service: MemoryService = Depends(require_memories), _authenticated: None = Depends(require_cookie)) -> dict[str, object]:  # noqa: B008
        require_same_origin(request)
        try:
            entry = service.update_manual(memory_id, UpdateMemory(body.content, body.category, 3, body.original_time_text, body.event_at, body.event_timezone, body.revision))
        except MemoryRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=("MEMORY_REVISION_CONFLICT", "memory has changed")) from exc
        except StoreError as exc:
            raise HTTPException(status_code=503, detail=("MEMORY_STORE_UNAVAILABLE", "memory store is unavailable")) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=("INVALID_MEMORY", "memory is invalid")) from exc
        return _memory_payload(entry)

    @app.delete(f"{API_PREFIX}/memories/{{memory_id}}", status_code=status.HTTP_204_NO_CONTENT)
    async def delete_memory(memory_id: str, request: Request, revision: int = Query(ge=1), service: MemoryService = Depends(require_memories), _authenticated: None = Depends(require_cookie)) -> None:  # noqa: B008
        require_same_origin(request)
        try:
            service.delete_manual(memory_id, revision=revision)
        except MemoryRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=("MEMORY_REVISION_CONFLICT", "memory has changed")) from exc
        except StoreError as exc:
            raise HTTPException(status_code=503, detail=("MEMORY_STORE_UNAVAILABLE", "memory store is unavailable")) from exc

    @app.get(f"{API_PREFIX}/memory-policy")
    async def get_memory_policy(service: MemoryPolicyService = Depends(require_memory_policy), _authenticated: None = Depends(require_cookie)) -> dict[str, object]:  # noqa: B008
        try:
            snapshot = service.get()
        except StoreError as exc:
            raise HTTPException(status_code=503, detail=("MEMORY_STORE_UNAVAILABLE", "memory store is unavailable")) from exc
        return _memory_policy_payload(snapshot, default_rules=service.default_rules())

    @app.put(f"{API_PREFIX}/memory-policy")
    async def update_memory_policy(body: UpdateMemoryPolicyBody, request: Request, service: MemoryPolicyService = Depends(require_memory_policy), _authenticated: None = Depends(require_cookie)) -> dict[str, object]:  # noqa: B008
        require_same_origin(request)
        try:
            snapshot = service.update(body.rules, revision=body.revision)
        except MemoryPolicyRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=("MEMORY_REVISION_CONFLICT", "memory policy has changed")) from exc
        except StoreError as exc:
            raise HTTPException(status_code=503, detail=("MEMORY_STORE_UNAVAILABLE", "memory store is unavailable")) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=("INVALID_MEMORY_POLICY", "memory policy is invalid")) from exc
        return _memory_policy_payload(snapshot, default_rules=service.default_rules())

    @app.post(f"{API_PREFIX}/memory-policy/reset")
    async def reset_memory_policy(body: ResetMemoryPolicyBody, request: Request, service: MemoryPolicyService = Depends(require_memory_policy), _authenticated: None = Depends(require_cookie)) -> dict[str, object]:  # noqa: B008
        require_same_origin(request)
        try:
            snapshot = service.reset(revision=body.revision)
        except MemoryPolicyRevisionConflict as exc:
            raise HTTPException(status_code=409, detail=("MEMORY_REVISION_CONFLICT", "memory policy has changed")) from exc
        except StoreError as exc:
            raise HTTPException(status_code=503, detail=("MEMORY_STORE_UNAVAILABLE", "memory store is unavailable")) from exc
        return _memory_policy_payload(snapshot, default_rules=snapshot.rules)

    @app.get(f"{API_PREFIX}/settings/provider")
    async def get_provider_settings(
        service: ProviderRuntime = Depends(require_providers),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        return {"provider": _provider_settings_payload(service.configuration)}

    @app.put(f"{API_PREFIX}/settings/provider")
    async def update_provider_settings(
        body: ProviderSettingsBody,
        request: Request,
        service: ProviderRuntime = Depends(require_providers),  # noqa: B008
        store: ProviderSettingsStore = Depends(require_provider_settings),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        existing = service.configuration
        api_key = body.api_key.strip() if body.api_key is not None else ""
        try:
            configuration = ProviderConfiguration(
                provider=body.provider,
                model=body.model.strip(),
                base_url=body.base_url.strip().rstrip("/"),
                api_key=api_key or existing.api_key,
                thinking_enabled=(
                    body.thinking_enabled
                    if body.thinking_enabled is not None
                    else existing.thinking_enabled
                ),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_PROVIDER_SETTINGS", "provider settings are invalid"),
            ) from exc
        prepared = await service.prepare(configuration)
        try:
            store.save(configuration)
        except Exception:
            await service.discard(prepared)
            raise
        service.activate(prepared)
        return {"provider": _provider_settings_payload(configuration)}

    @app.get(f"{API_PREFIX}/settings/prompts")
    async def get_prompt_settings(
        service: PromptConfigurationService = Depends(require_prompts),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, object]:
        return _prompt_settings_payload(service.snapshot())

    @app.put(f"{API_PREFIX}/settings/prompts/soul")
    async def update_soul_settings(
        body: SoulSettingsBody,
        request: Request,
        service: PromptConfigurationService = Depends(require_prompts),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, object]:
        require_same_origin(request)
        try:
            snapshot = service.update_soul(body.content)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_SOUL_SETTINGS", str(exc)),
            ) from exc
        return _prompt_settings_payload(snapshot)

    @app.post(f"{API_PREFIX}/sessions", status_code=status.HTTP_201_CREATED)
    async def create_session(
        body: CreateSessionBody,
        request: Request,
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        return {"session": _session_summary(sessions.create(body.title.strip()))}

    @app.patch(f"{API_PREFIX}/sessions/{{session_id}}")
    async def rename_session(
        session_id: str,
        body: RenameSessionBody,
        request: Request,
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        load_session_or_404(session_id)
        title = body.title.strip()
        if not title:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_SESSION_TITLE", "会话名称不能为空"),
            )
        try:
            renamed = await sessions.rename(session_id, title)
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("SESSION_NOT_FOUND", "会话不存在"),
            ) from exc
        return {"session": _session_summary(renamed)}

    @app.delete(
        f"{API_PREFIX}/sessions/{{session_id}}",
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_session(
        session_id: str,
        request: Request,
        _authenticated: None = Depends(require_cookie),
    ) -> Response:
        require_same_origin(request)
        try:
            session = sessions.load(session_id)
            if session.archived_at is None:
                raise ValueError("session is not archived")
            await require_archive().purge_session(session_id)
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("SESSION_NOT_ARCHIVED", "只能永久删除已归档会话"),
            ) from exc
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("SESSION_NOT_FOUND", "会话不存在"),
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(f"{API_PREFIX}/sessions/{{session_id}}/archive")
    async def archive_session(
        session_id: str,
        request: Request,
        service: ArchiveService = Depends(require_archive),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        load_session_or_404(session_id)
        return {"session": _session_summary(await service.archive_session(session_id))}

    @app.post(f"{API_PREFIX}/sessions/{{session_id}}/restore")
    async def restore_session(
        session_id: str,
        request: Request,
        service: ArchiveService = Depends(require_archive),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        load_session_or_404(session_id)
        return {"session": _session_summary(await service.restore_session(session_id))}

    @app.get(f"{API_PREFIX}/sessions/{{session_id}}")
    async def get_session(
        session_id: str,
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        return {"session": session_snapshot(load_session_or_404(session_id))}

    @app.get(f"{API_PREFIX}/commands")
    async def list_commands(
        service: CommandService = Depends(require_commands),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, list[dict[str, object]]]:
        return {"commands": [_command_descriptor_payload(item) for item in service.descriptors()]}

    @app.post(f"{API_PREFIX}/sessions/{{session_id}}/commands", response_model=None)
    async def execute_command(
        session_id: str,
        body: ExecuteCommandBody,
        request: Request,
        service: CommandService = Depends(require_commands),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, object] | JSONResponse:
        require_same_origin(request)
        require_session_mutable(load_session_or_404(session_id))
        outcome = await service.execute(body.command, current_session_id=session_id)
        if outcome is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_COMMAND", "command text must begin with a slash"),
            )
        payload: dict[str, object] = {"outcome": _command_outcome_payload(outcome)}
        if outcome.submission is None:
            return payload

        active_runtime = require_runtime()
        runtime_request = RuntimeRequest(
            request_id=uuid4().hex,
            session_id=session_id,
            source=InvocationSource.GATEWAY,
            user_input=UserInput(outcome.submission.text),
            idempotency_key=body.idempotency_key,
            skill_refs=outcome.submission.skill_refs,
        )
        await active_runtime.submit(runtime_request)
        run = active_runtime.find_run(
            session_id,
            request_id=runtime_request.request_id,
            idempotency_key=body.idempotency_key,
        )
        if run is None:
            raise RuntimeError("runtime accepted a command submission without a durable run")
        payload["request"] = _run_payload(run)
        return JSONResponse(status_code=status.HTTP_202_ACCEPTED, content=payload)

    @app.get(f"{API_PREFIX}/skills")
    async def list_skills(
        service: SkillService = Depends(require_skills),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, list[dict[str, object]]]:
        return {"skills": [_skill_manifest_payload(item) for item in service.list()]}

    @app.get(f"{API_PREFIX}/skills/{{skill_id}}")
    async def get_skill(
        skill_id: str,
        service: SkillService = Depends(require_skills),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        manifest = next((item for item in service.list() if item.skill_id == skill_id), None)
        if manifest is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("SKILL_NOT_FOUND", "local Skill was not found"),
            )
        return {"skill": _skill_manifest_payload(manifest)}

    @app.post(f"{API_PREFIX}/skills/refresh")
    async def refresh_skills(
        request: Request,
        service: SkillService = Depends(require_skills),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, list[dict[str, object]]]:
        require_same_origin(request)
        try:
            manifests = service.refresh()
        except SkillError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(exc.code, "local Skill refresh failed"),
            ) from exc
        return {"skills": [_skill_manifest_payload(item) for item in manifests]}

    @app.get("/downloads/{download_id}")
    async def download_snapshot(
        download_id: str,
        service: DownloadSnapshotService = Depends(require_downloads),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> FileResponse:
        lease = service.lease(download_id=download_id)
        response = FileResponse(
            path=lease.path,
            filename=lease.filename,
            media_type="application/octet-stream",
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Cache-Control"] = "private, no-store"
        return response

    @app.put(f"{API_PREFIX}/sessions/{{session_id}}/workspace")
    async def bind_workspace(
        session_id: str,
        body: WorkspaceBody,
        request: Request,
        service: WorkspaceService = Depends(require_workspace),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        session = load_session_or_404(session_id)
        try:
            binding = (
                await service.replace(session_id, body.root)
                if session.workspace_binding is not None
                else await service.bind(session_id, body.root)
            )
        except WorkspaceServiceError as exc:
            raise HTTPException(
                status_code=(
                    status.HTTP_409_CONFLICT
                    if exc.code == "WORKSPACE_BUSY"
                    else status.HTTP_422_UNPROCESSABLE_CONTENT
                ),
                detail=(exc.code, "工作区无法绑定"),
            ) from exc
        return {"workspace": _workspace_payload(binding)}

    @app.post(f"{API_PREFIX}/sessions/{{session_id}}/workspace/pick")
    async def pick_workspace_directory(
        session_id: str,
        request: Request,
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, str | None]:
        require_same_origin(request)
        session = load_session_or_404(session_id)
        if session.archived_at is not None:
            raise SessionArchivedError
        try:
            selected = await asyncio.to_thread(pick_directory)
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("DIRECTORY_PICKER_UNAVAILABLE", "无法打开本机目录选择器"),
            ) from exc
        return {"path": selected}

    @app.delete(f"{API_PREFIX}/sessions/{{session_id}}/workspace")
    async def unbind_workspace(
        session_id: str,
        request: Request,
        service: WorkspaceService = Depends(require_workspace),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> Response:
        require_same_origin(request)
        load_session_or_404(session_id)
        try:
            await service.unbind(session_id)
        except WorkspaceServiceError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(exc.code, "工作区正在使用"),
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(f"{API_PREFIX}/sessions/{{session_id}}/approvals/{{approval_id}}/decision")
    async def decide_approval(
        session_id: str,
        approval_id: str,
        body: ApprovalDecisionBody,
        request: Request,
        active_runtime: RuntimeHost = Depends(require_runtime),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        session = load_session_or_404(session_id)
        approval = session.pending_approval
        if approval is None or approval.approval_id != approval_id:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("APPROVAL_NOT_FOUND", "审批请求不存在"),
            )
        reason = None if body.reason is None else body.reason.strip() or None
        if reason is not None and any(ord(character) < 32 or ord(character) == 127 for character in reason):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_APPROVAL_DECISION", "拒绝理由不能含控制字符"),
            )
        if body.decision == "approve" and reason is not None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_APPROVAL_DECISION", "批准请求不能附带理由"),
            )
        try:
            decision = await active_runtime.decide_approval(
                session_id,
                approval_id,
                status=(
                    ApprovalDecisionStatus.APPROVED
                    if body.decision == "approve"
                    else ApprovalDecisionStatus.DENIED
                ),
                idempotency_key=body.idempotency_key,
                actor_id="gateway",
                reason=reason,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("APPROVAL_CONFLICT", "审批决定冲突或已过期"),
            ) from exc
        return {
            "approval": {
                "approvalId": approval_id,
                "runId": approval.run_id,
                "decisionId": decision.decision_id,
                "status": decision.status.value,
            }
        }

    @app.post(
        f"{API_PREFIX}/sessions/{{session_id}}/requests",
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def submit_request(
        session_id: str,
        body: SubmitRequestBody,
        request: Request,
        active_runtime: RuntimeHost = Depends(require_runtime),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        load_session_or_404(session_id)
        raw_text = body.text.strip()
        text = SkillService.unescape_mentions(raw_text)
        if not text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_REQUEST", "消息不能为空"),
            )
        attachment_refs = tuple(body.attachment_refs)
        if len(attachment_refs) != len(set(attachment_refs)) or any(
            not reference for reference in attachment_refs
        ):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_ATTACHMENT_REFS", "附件引用无效"),
            )
        try:
            skill_refs = (
                ()
                if skills is None
                else skills.resolve_current_names(SkillService.mention_names(raw_text))
            )
        except SkillError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=(exc.code, "requested local Skill is unavailable"),
            ) from exc
        if skills is None and SkillService.mention_names(raw_text):
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=("SKILLS_UNAVAILABLE", "local Skill service is unavailable"),
            )
        runtime_request = RuntimeRequest(
            request_id=uuid4().hex,
            session_id=session_id,
            source=InvocationSource.GATEWAY,
            user_input=UserInput(text),
            attachment_refs=attachment_refs,
            idempotency_key=body.idempotency_key,
            skill_refs=skill_refs,
        )
        await active_runtime.submit(runtime_request)
        run = active_runtime.find_run(
            session_id,
            request_id=runtime_request.request_id,
            idempotency_key=body.idempotency_key,
        )
        if run is None:
            raise RuntimeError("runtime accepted request without a durable run")
        return {"request": _run_payload(run)}

    @app.delete(f"{API_PREFIX}/sessions/{{session_id}}/requests/{{request_id}}")
    async def cancel_request(
        session_id: str,
        request_id: str,
        request: Request,
        active_runtime: RuntimeHost = Depends(require_runtime),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        load_session_or_404(session_id)
        if await active_runtime.cancel_queued(session_id, request_id):
            run = active_runtime.find_run(session_id, request_id=request_id)
            if run is None:
                raise RuntimeError("cancelled request is missing its durable run")
            return {"request": _run_payload(run)}
        run = active_runtime.find_run(session_id, request_id=request_id)
        if run is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("REQUEST_NOT_FOUND", "队列请求不存在"),
            )
        if run.status is RunStatus.CANCELLED_BEFORE_START:
            return {"request": _run_payload(run)}
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("RUN_ALREADY_STARTED", "请求已开始运行"),
        )

    @app.post(f"{API_PREFIX}/sessions/{{session_id}}/runs/{{run_id}}/stop")
    async def stop_run(
        session_id: str,
        run_id: str,
        request: Request,
        active_runtime: RuntimeHost = Depends(require_runtime),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        session = load_session_or_404(session_id)
        if await active_runtime.stop_run(session_id, run_id):
            return {"run": _run_payload(_run_or_404(load_session_or_404(session_id), run_id))}
        _run_or_404(session, run_id)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=("RUN_NOT_ACTIVE", "运行未处于活动状态"),
        )

    @app.post(f"{API_PREFIX}/sessions/{{session_id}}/runs/{{run_id}}/steer")
    async def steer_run(
        session_id: str,
        run_id: str,
        body: SteerBody,
        request: Request,
        active_runtime: RuntimeHost = Depends(require_runtime),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, str]]:
        require_same_origin(request)
        _run_or_404(load_session_or_404(session_id), run_id)
        text = body.text.strip()
        if not text:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_REQUEST", "引导内容不能为空"),
            )
        result = await active_runtime.steer(
            session_id,
            run_id,
            steer_id=body.steer_id,
            idempotency_key=body.idempotency_key,
            text=text,
        )
        if result == "RUN_NOT_ACTIVE":
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("RUN_NOT_ACTIVE", "运行未处于活动状态"),
            )
        return {"steer": {"steerId": body.steer_id, "status": result}}

    @app.post(
        f"{API_PREFIX}/sessions/{{session_id}}/runs/{{run_id}}"
        "/queued-requests/{request_id}/steer"
    )
    async def steer_queued_request(
        session_id: str,
        run_id: str,
        request_id: str,
        body: SteerQueuedBody,
        request: Request,
        active_runtime: RuntimeHost = Depends(require_runtime),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, str]]:
        require_same_origin(request)
        load_session_or_404(session_id)
        result = await active_runtime.steer_queued(
            session_id,
            run_id,
            request_id=request_id,
            steer_id=body.steer_id,
            idempotency_key=body.idempotency_key,
        )
        if result in {
            "RUN_NOT_ACTIVE",
            "QUEUED_REQUEST_NOT_FOUND",
            "QUEUED_REQUEST_NOT_GUIDABLE",
        }:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(result, "queued request cannot be converted to guidance"),
            )
        return {"steer": {"steerId": body.steer_id, "status": result}}

    @app.get(f"{API_PREFIX}/sessions/{{session_id}}/attachments")
    async def list_attachments(
        session_id: str,
        attachment_service: AttachmentService = Depends(require_attachments),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, list[dict[str, object]]]:
        load_session_or_404(session_id)
        return {
            "attachments": [
                _attachment_payload(attachment)
                for attachment in attachment_service.list(session_id)
            ]
        }

    @app.post(
        f"{API_PREFIX}/sessions/{{session_id}}/attachments",
        status_code=status.HTTP_201_CREATED,
    )
    async def upload_attachment(
        session_id: str,
        request: Request,
        attachment_service: AttachmentService = Depends(require_attachments),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        load_session_or_404(session_id)
        display_name = request.headers.get("x-attachment-name")
        if display_name is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("ATTACHMENT_INVALID_NAME", "缺少附件名称"),
            )
        content_type = request.headers.get("content-type", "").split(";", maxsplit=1)[0]

        async def chunks() -> AsyncIterator[bytes]:
            async for chunk in request.stream():
                yield chunk

        attachment_id = uuid4().hex
        if content_type == "text/plain":
            attachment = await attachment_service.ingest_text(
                session_id=session_id,
                attachment_id=attachment_id,
                display_name=display_name,
                chunks=chunks(),
                created_at=datetime.now(UTC),
            )
        elif content_type == "application/pdf":
            attachment = await attachment_service.ingest_pdf(
                session_id=session_id,
                attachment_id=attachment_id,
                display_name=display_name,
                chunks=chunks(),
                created_at=datetime.now(UTC),
            )
        else:
            raise HTTPException(
                status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
                detail=("ATTACHMENT_UNSUPPORTED_TYPE", "仅支持文本和 PDF 附件"),
            )
        return {"attachment": _attachment_payload(attachment)}

    @app.delete(f"{API_PREFIX}/sessions/{{session_id}}/attachments/{{attachment_id}}")
    async def delete_attachment(
        session_id: str,
        attachment_id: str,
        request: Request,
        attachment_service: AttachmentService = Depends(require_attachments),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> Response:
        require_same_origin(request)
        load_session_or_404(session_id)
        try:
            await attachment_service.delete(session_id, attachment_id)
        except AttachmentError as exc:
            if exc.code == "ATTACHMENT_IN_USE":
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail=(exc.code, "附件正在被消息引用"),
                ) from exc
            raise
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.get(f"{API_PREFIX}/tasks")
    async def list_tasks(
        session_id: str | None = None,
        archived: bool = False,
        task_status: TaskStatus | None = Query(  # noqa: B008
            default=None, alias="status"
        ),
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, list[dict[str, object]]]:
        tasks = active_scheduler.list(archived=archived)
        if session_id is not None:
            tasks = tuple(task for task in tasks if task.session_id == session_id)
        if task_status is not None:
            tasks = tuple(task for task in tasks if task.status is task_status)
        return {"tasks": [_task_payload(task) for task in tasks]}

    @app.post(f"{API_PREFIX}/tasks", status_code=status.HTTP_201_CREATED)
    async def create_task(
        body: CreateTaskBody,
        request: Request,
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        session = load_session_or_404(body.session_id)
        if body.managed_permissions:
            binding = session.workspace_binding
            if (
                binding is None
                or body.workspace_binding_revision is None
                or body.workspace_binding_revision != binding.revision
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=("TASK_WORKSPACE_GRANT_INVALID", "工作区授权版本无效"),
                )
        try:
            task = await active_scheduler.create(
                session_id=body.session_id,
                instruction=body.instruction.strip(),
                schedule=body.schedule.to_domain(),
                workspace_binding_revision=body.workspace_binding_revision,
                managed_permissions=tuple(body.managed_permissions),
                skill_refs=(
                    ()
                    if not body.skill_refs
                    else _resolve_task_skills(skills, body.skill_refs)
                ),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail=("INVALID_SCHEDULE", "定时任务规则无效"),
            ) from exc
        return {"task": _task_payload(task)}

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}")
    async def get_task(
        task_id: str,
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        try:
            return {"task": _task_payload(active_scheduler.detail(task_id))}
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("TASK_NOT_FOUND", "定时任务不存在"),
            ) from exc

    @app.put(f"{API_PREFIX}/tasks/{{task_id}}")
    async def update_task(
        task_id: str,
        body: CreateTaskBody,
        request: Request,
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        session = load_session_or_404(body.session_id)
        if body.managed_permissions:
            binding = session.workspace_binding
            if (
                binding is None
                or body.workspace_binding_revision is None
                or body.workspace_binding_revision != binding.revision
            ):
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                    detail=("TASK_WORKSPACE_GRANT_INVALID", "工作区授权版本无效"),
                )
        try:
            task = await active_scheduler.update(
                task_id,
                session_id=body.session_id,
                instruction=body.instruction.strip(),
                schedule=body.schedule.to_domain(),
                workspace_binding_revision=body.workspace_binding_revision,
                managed_permissions=tuple(body.managed_permissions),
                skill_refs=(
                    () if not body.skill_refs else _resolve_task_skills(skills, body.skill_refs)
                ),
            )
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("TASK_NOT_FOUND", "定时任务不存在"),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("TASK_UPDATE_INVALID", "定时任务不能更新"),
            ) from exc
        return {"task": _task_payload(task)}

    @app.get(f"{API_PREFIX}/tasks/{{task_id}}/occurrences")
    async def task_occurrences(
        task_id: str,
        cursor: str | None = None,
        limit: int = 50,
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, object]:
        try:
            active_scheduler.detail(task_id)
            values, next_cursor = active_scheduler.history(
                task_id, cursor=cursor, limit=limit
            )
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("TASK_HISTORY_NOT_FOUND", "定时任务历史不存在"),
            ) from exc
        return {
            "occurrences": [_occurrence_payload(item) for item in values],
            "nextCursor": next_cursor,
        }

    @app.post(f"{API_PREFIX}/tasks/{{task_id}}/cancel")
    async def cancel_task(
        task_id: str,
        request: Request,
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        try:
            return {"task": _task_payload(await active_scheduler.cancel(task_id))}
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("TASK_NOT_FOUND", "定时任务不存在"),
            ) from exc

    @app.post(f"{API_PREFIX}/tasks/{{task_id}}/restore")
    async def restore_task(
        task_id: str,
        request: Request,
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        return {"task": _task_payload(await active_scheduler.restore(task_id))}

    @app.delete(f"{API_PREFIX}/tasks/{{task_id}}", status_code=status.HTTP_204_NO_CONTENT)
    async def purge_task(
        task_id: str,
        request: Request,
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> Response:
        require_same_origin(request)
        try:
            active_scheduler.purge(task_id)
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("TASK_NOT_ARCHIVED", "只能永久删除已归档任务"),
            ) from exc
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @app.post(f"{API_PREFIX}/tasks/{{task_id}}/occurrences/{{occurrence_id}}/retry")
    async def retry_task_occurrence(
        task_id: str,
        occurrence_id: str,
        request: Request,
        active_scheduler: SchedulerService = Depends(require_scheduler),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> dict[str, dict[str, object]]:
        require_same_origin(request)
        try:
            occurrence = await active_scheduler.retry(task_id, occurrence_id)
        except StoreError as exc:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=("OCCURRENCE_NOT_FOUND", "定时任务历史不存在"),
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=("OCCURRENCE_NOT_RETRYABLE", "该任务历史不能重试"),
            ) from exc
        return {"occurrence": _occurrence_payload(occurrence)}

    @app.get(f"{API_PREFIX}/sessions/{{session_id}}/runs/{{run_id}}/events")
    async def run_events(
        session_id: str,
        run_id: str,
        request: Request,
        active_runtime: RuntimeHost = Depends(require_runtime),  # noqa: B008
        _authenticated: None = Depends(require_cookie),
    ) -> StreamingResponse:
        require_same_origin_stream(request)
        session = load_session_or_404(session_id)
        run = _run_or_404(session, run_id)
        last_event_id = _last_event_id(request)
        handle = active_runtime.handle_for_run(session_id, run_id)
        if handle is None:
            return StreamingResponse(
                _snapshot_only_events(session, run, session_snapshot(session)),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
        subscription = event_hub.subscribe(
            session_id=session_id,
            request_id=run.request_id,
            run_id=run.run_id,
            handle=handle,
            last_event_id=last_event_id,
        )
        return StreamingResponse(
            _stream_events(session, run, subscription, session_snapshot(session)),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    web_dist = _web_dist()
    if web_dist.is_dir():
        @app.get("/", include_in_schema=False)
        async def web_index() -> FileResponse:
            return FileResponse(web_dist / "index.html")

        app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")

    return app


def _pick_local_directory() -> str | None:
    """Open the local OS directory dialog without exposing arbitrary paths."""

    import tkinter as tk
    from tkinter import filedialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        root.update_idletasks()
        selected = filedialog.askdirectory(
            parent=root,
            title="选择 SJTUClaw 工作区",
            mustexist=True,
        )
        return selected or None
    finally:
        root.destroy()


def _session_summary(session: Session) -> dict[str, object]:
    return {
        "sessionId": session.session_id,
        "title": session.title,
        "archivedAt": None if session.archived_at is None else session.archived_at.isoformat(),
        "archivedReason": (
            None if session.archived_reason is None else session.archived_reason.value
        ),
        "createdAt": session.created_at.isoformat(),
        "updatedAt": session.updated_at.isoformat(),
    }


def _session_snapshot(
    session: Session,
    *,
    compaction: CompactionService | None = None,
    model_profile: ModelProfile | None = None,
) -> dict[str, object]:
    return {
        **_session_summary(session),
        "summary": session.summary,
        "messages": [_message_payload(message) for message in session.messages],
        "pendingRequests": [_pending_payload(item) for item in session.pending_requests],
        "runs": [_run_payload(run) for run in session.runs],
        "workspace": (
            None
            if session.workspace_binding is None
            else _workspace_payload(session.workspace_binding)
        ),
        "pendingApproval": _pending_approval_payload(session),
        "explicitSkillRefs": [_skill_reference_payload(item) for item in session.explicit_skill_refs],
        "skillUsages": [_skill_usage_payload(item) for item in session.skill_usages],
        "tokenUsage": {
            "promptTokens": session.prompt_tokens,
            "promptCacheHitTokens": session.prompt_cache_hit_tokens,
            "promptCacheMissTokens": session.prompt_cache_miss_tokens,
            "completionTokens": session.completion_tokens,
            "totalTokens": (
                (session.prompt_cache_hit_tokens or 0)
                + (session.prompt_cache_miss_tokens or 0)
                + session.completion_tokens
                if session.cache_breakdown_complete
                else session.prompt_tokens + session.completion_tokens
            ),
            "cacheBreakdownAvailable": session.cache_breakdown_complete,
            "trackingStartedAt": (
                None
                if session.token_tracking_started_at is None
                else session.token_tracking_started_at.isoformat()
            ),
        },
        "contextWindow": (
            None
            if compaction is None or model_profile is None
            else {
                "currentTokens": compaction.context_tokens(session),
                "outputReserveTokens": compaction.output_reserve_tokens,
                "limitTokens": model_profile.context_window_tokens,
                "compactionTriggerTokens": compaction.compaction_trigger_tokens,
                "compactionTargetTokens": compaction.compaction_target_tokens,
                "model": {
                    "modelId": model_profile.model_id,
                    "displayName": model_profile.display_name,
                    "contextWindowTokens": model_profile.context_window_tokens,
                },
            }
        ),
    }


def _command_descriptor_payload(descriptor: CommandDescriptor) -> dict[str, object]:
    return {
        "template": descriptor.template,
        "description": descriptor.description,
        "skillArgument": descriptor.skill_argument,
    }


def _command_outcome_payload(outcome: CommandOutcome) -> dict[str, object]:
    return {
        "command": outcome.raw,
        "lines": list(outcome.lines),
        "isError": outcome.is_error,
        "effects": {
            "refreshSessions": outcome.effects.refresh_sessions,
            "refreshMemories": outcome.effects.refresh_memories,
            "refreshSkills": outcome.effects.refresh_skills,
            "selectSessionId": outcome.effects.select_session_id,
        },
    }


def _skill_manifest_payload(manifest: SkillManifest) -> dict[str, object]:
    return {
        "skillId": manifest.skill_id,
        "description": manifest.description,
        "version": manifest.version,
        "snapshotId": manifest.snapshot_id,
        "snapshotHash": manifest.snapshot_hash,
        "available": manifest.available,
        "unavailableReason": manifest.unavailable_reason,
    }


def _provider_settings_payload(
    configuration: ProviderConfiguration,
) -> dict[str, object]:
    snapshot = ProviderSettingsStore.public_snapshot(configuration)
    return {
        "provider": snapshot.provider.value,
        "model": snapshot.model,
        "baseUrl": snapshot.base_url,
        "keySaved": snapshot.key_saved,
        "tokenCounterMode": snapshot.token_counter_mode,
        "thinkingEnabled": snapshot.thinking_enabled,
    }


def _prompt_settings_payload(snapshot: PromptSnapshot) -> dict[str, object]:
    return {
        "systemPrompt": {
            "path": str(snapshot.system_prompt_path),
            "editable": False,
            "restartRequired": True,
        },
        "soul": {
            "path": str(snapshot.soul_path),
            "content": snapshot.soul,
        },
    }


def _memory_payload(entry: object) -> dict[str, object]:
    from sjtuclaw.models.memory import MemoryEntry

    assert isinstance(entry, MemoryEntry)
    return {
        "memoryId": entry.memory_id,
        "content": entry.content,
        "category": entry.category.value,
        "importance": entry.importance,
        "sourceKind": entry.source_kind.value,
        "sourceSessionId": entry.source_session_id,
        "sourceMessageId": entry.source_message_id,
        "originalTimeText": entry.original_time_text,
        "eventAt": None if entry.event_at is None else entry.event_at.isoformat(),
        "eventTimezone": entry.event_timezone,
        "createdAt": entry.created_at.isoformat(),
        "updatedAt": None if entry.updated_at is None else entry.updated_at.isoformat(),
        "revision": entry.revision,
    }


def _memory_policy_payload(snapshot: object, *, default_rules: str) -> dict[str, object]:
    from sjtuclaw.runtime.memory_policy import MemoryPolicySnapshot

    assert isinstance(snapshot, MemoryPolicySnapshot)
    return {
        "rules": snapshot.rules,
        "defaultRules": default_rules,
        "isOverride": snapshot.is_override,
        "revision": snapshot.revision,
        "updatedAt": None if snapshot.updated_at is None else snapshot.updated_at.isoformat(),
    }


def _skill_reference_payload(reference: SkillReference) -> dict[str, str]:
    return {
        "skillId": reference.skill_id,
        "snapshotId": reference.snapshot_id,
        "snapshotHash": reference.snapshot_hash,
        "version": reference.version,
    }


def _skill_usage_payload(usage: SkillUsage) -> dict[str, object]:
    return {
        "reference": _skill_reference_payload(usage.reference),
        "source": usage.source.value,
        "selectionOrder": usage.selection_order,
        "reason": usage.reason,
        "selectedAt": usage.selected_at.isoformat(),
        "requestId": usage.request_id,
        "userTask": usage.user_task,
        "outputMessageId": usage.output_message_id,
        "resourcePaths": list(usage.resource_paths),
    }


def _resolve_task_skills(
    service: SkillService | None, raw_skill_refs: list[str]
) -> tuple[SkillReference, ...]:
    if service is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=("SKILLS_UNAVAILABLE", "local Skill service is unavailable"),
        )
    try:
        return service.resolve_current_names(raw_skill_refs)
    except SkillError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=(exc.code, "scheduled Skill reference is unavailable"),
        ) from exc


def _message_payload(message: Message) -> dict[str, object]:
    return {
        "messageId": message.message_id,
        "role": message.role.value,
        "content": message.content,
        "createdAt": message.created_at.isoformat(),
        "sequenceNumber": message.sequence_number,
        "attachmentRefs": list(message.attachment_refs),
        "inputKind": message.input_kind.value,
        "reasoningContent": message.reasoning_content,
        "toolCalls": [
            {
                "callId": call.call_id,
                "name": call.name,
                "arguments": call.arguments_json,
            }
            for call in message.tool_calls
        ],
        "toolCallId": message.tool_call_id,
        "toolName": message.tool_name,
        "toolResultStatus": (
            None if message.tool_result_status is None else message.tool_result_status.value
        ),
        "toolErrorCode": message.tool_error_code,
        "structuredContent": (
            mutable_json(message.tool_structured_content)
            if message.tool_structured_content is not None
            else None
        ),
        "resources": [_resource_payload(resource) for resource in message.tool_resources],
    }


def _resource_payload(resource: ToolResourceLink) -> dict[str, object]:
    return {
        "kind": resource.kind,
        "name": resource.name,
        "href": resource.href,
        "mimeType": resource.mime_type,
        "expiresAt": resource.expires_at,
    }


def _pending_payload(pending: PendingRequest) -> dict[str, object]:
    return {
        "requestId": pending.request_id,
        "acceptanceSequence": pending.acceptance_sequence,
        "source": pending.source.value,
        "text": pending.text,
        "submittedAt": pending.submitted_at.isoformat(),
        "attachmentRefs": list(pending.attachment_refs),
        "skillRefs": [_skill_reference_payload(item) for item in pending.skill_refs],
    }


def _run_payload(run: RunState) -> dict[str, object]:
    return {
        "runId": run.run_id,
        "requestId": run.request_id,
        "status": run.status.value,
        "startedAt": None if run.started_at is None else run.started_at.isoformat(),
        "completedAt": None if run.completed_at is None else run.completed_at.isoformat(),
        "durationMs": run.duration_ms,
        "assistantMessageId": run.assistant_message_id,
        "selectedSkillRefs": [_skill_reference_payload(item) for item in run.selected_skill_refs],
    }


def _attachment_payload(attachment: Attachment) -> dict[str, object]:
    return {
        "attachmentId": attachment.attachment_id,
        "sessionId": attachment.session_id,
        "displayName": attachment.display_name,
        "kind": attachment.kind.value,
        "status": attachment.status.value,
        "createdAt": attachment.created_at.isoformat(),
        "updatedAt": attachment.updated_at.isoformat(),
        "sizeBytes": attachment.size_bytes,
        "sha256": attachment.sha256,
        "normalizedCharCount": attachment.normalized_char_count,
        "errorCode": attachment.error_code,
    }


def _workspace_payload(binding: WorkspaceBinding) -> dict[str, object]:
    return {
        "bindingId": binding.binding_id,
        "displayPath": binding.display_path,
        "revision": binding.revision,
        "boundAt": binding.bound_at.isoformat(),
    }


def _pending_approval_payload(session: Session) -> dict[str, object] | None:
    approval = session.pending_approval
    if approval is None:
        return None
    return {
        "approvalId": approval.approval_id,
        "runId": approval.run_id,
        "invocationId": approval.invocation_id,
        "toolName": approval.intent.name,
        "safetyLevel": approval.intent.safety_level.value,
        "targets": list(approval.intent.target_resources),
        "preview": _approval_preview(approval.intent.name, approval.intent.arguments),
        "reason": approval.reason,
        "createdAt": approval.created_at.isoformat(),
        "expiresAt": approval.expires_at.isoformat(),
    }


def _approval_preview(name: str, arguments: Mapping[str, object]) -> str:
    if not isinstance(arguments, Mapping):
        return "参数预览不可用"
    if name == "run_command":
        command = arguments.get("command")
        return _bounded_preview(command, "命令")
    if name == "write_file":
        path = arguments.get("path")
        mode = arguments.get("mode", "write")
        payload = arguments.get("patch") if mode == "patch" else arguments.get("content")
        return f"{mode}: {_bounded_preview(path, '路径')}; {_bounded_preview(payload, '内容', 800)}"
    if name == "copy_attachment_to_workspace":
        return f"复制到: {_bounded_preview(arguments.get('destination'), '目标')}"
    if name == "create_download":
        return f"创建下载快照: {_bounded_preview(arguments.get('path'), '文件')}"
    if name == "new_shell":
        return f"启动 Shell: {_bounded_preview(arguments.get('cwd', '.'), '目录')}"
    return "此操作会产生受限副作用"


def _bounded_preview(value: object, label: str, limit: int = 300) -> str:
    if not isinstance(value, str):
        return f"{label}不可用"
    compact = " ".join(value.split())
    return compact[:limit] + ("…" if len(compact) > limit else "")


def _task_payload(task: Task) -> dict[str, object]:
    return {
        "taskId": task.task_id,
        "sessionId": task.session_id,
        "instruction": task.instruction,
        "schedule": {
            "kind": task.schedule.kind.value,
            "anchorAt": task.schedule.anchor_at.isoformat(),
            "intervalSeconds": task.schedule.interval_seconds,
            "timezone": task.schedule.timezone,
            "hour": task.schedule.hour,
            "minute": task.schedule.minute,
            "weekdays": list(task.schedule.weekdays),
        },
        "status": task.status.value,
        "nextRunAt": None if task.next_run_at is None else task.next_run_at.isoformat(),
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
        "workspaceBindingRevision": task.workspace_binding_revision,
        "managedPermissions": list(task.managed_permissions),
        "skillRefs": [_skill_reference_payload(item) for item in task.skill_refs],
        "archivedAt": None if task.archived_at is None else task.archived_at.isoformat(),
        "archivedReason": (
            None if task.archived_reason is None else task.archived_reason.value
        ),
    }


def _occurrence_payload(occurrence: Occurrence) -> dict[str, object]:
    return {
        "occurrenceId": occurrence.occurrence_id,
        "taskId": occurrence.task_id,
        "sequenceNumber": occurrence.sequence_number,
        "status": occurrence.status.value,
        "scheduledFor": occurrence.scheduled_for.isoformat(),
        "createdAt": occurrence.created_at.isoformat(),
        "updatedAt": occurrence.updated_at.isoformat(),
        "queuedAt": None if occurrence.queued_at is None else occurrence.queued_at.isoformat(),
        "runId": occurrence.run_id,
        "requestId": occurrence.request_id,
        "assistantMessageId": occurrence.assistant_message_id,
        "coalesced": occurrence.coalesced,
        "missedCount": occurrence.missed_count,
        "retryOfOccurrenceId": occurrence.retry_of_occurrence_id,
        "blockingOccurrenceId": occurrence.blocking_occurrence_id,
        "errorCode": occurrence.error_code,
    }


def _run_or_404(session: Session, run_id: str) -> RunState:
    run = next((item for item in session.runs if item.run_id == run_id), None)
    if run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=("RUN_NOT_FOUND", "运行不存在"),
        )
    return run


def _last_event_id(request: Request) -> int | None:
    raw = request.headers.get("last-event-id")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=("INVALID_LAST_EVENT_ID", "事件游标无效"),
        ) from exc
    if value < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=("INVALID_LAST_EVENT_ID", "事件游标无效"),
        )
    return value


async def _snapshot_only_events(
    session: Session,
    run: RunState,
    session_payload: dict[str, object],
) -> AsyncIterator[str]:
    yield _sse_frame("snapshot", _snapshot_payload(session, run, session_payload))


async def _stream_events(
    session: Session,
    run: RunState,
    subscription: GatewaySubscription,
    session_payload: dict[str, object],
) -> AsyncIterator[str]:
    yield _sse_frame("snapshot", _snapshot_payload(session, run, session_payload))
    if subscription.replay_window_expired:
        yield _sse_frame(
            "replay_window_expired",
            {
                "schemaVersion": 1,
                "eventId": 0,
                "sessionId": session.session_id,
                "requestId": run.request_id,
                "runId": run.run_id,
                "sequenceNumber": 0,
                "occurredAt": datetime.now(UTC).isoformat(),
                "payload": {"code": "REPLAY_WINDOW_EXPIRED"},
            },
        )
    events = subscription.events()
    next_event = asyncio.ensure_future(anext(events))
    try:
        while True:
            done, _pending = await asyncio.wait((next_event,), timeout=15)
            if not done:
                yield ": heartbeat\n\n"
                continue
            try:
                event = next_event.result()
            except StopAsyncIteration:
                return
            yield _sse_frame(event.event_type, event.as_dict(), event.event_id)
            next_event = asyncio.ensure_future(anext(events))
    finally:
        if not next_event.done():
            next_event.cancel()
            await asyncio.gather(next_event, return_exceptions=True)
        await events.aclose()


def _snapshot_payload(
    session: Session,
    run: RunState,
    session_payload: dict[str, object],
) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "eventId": 0,
        "sessionId": session.session_id,
        "requestId": run.request_id,
        "runId": run.run_id,
        "sequenceNumber": 0,
        "occurredAt": datetime.now(UTC).isoformat(),
        "payload": {"session": session_payload, "run": _run_payload(run)},
    }


def _sse_frame(event_type: str, data: dict[str, object], event_id: int | None = None) -> str:
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines.extend((f"event: {event_type}", f"data: {json.dumps(data, ensure_ascii=False)}"))
    return "\n".join(lines) + "\n\n"


def _web_dist() -> Path:
    return Path(__file__).resolve().parents[3] / "web" / "dist"


class _ServerReadiness(Protocol):
    started: bool
    should_exit: bool


async def _open_bootstrap_page_when_ready(
    server: _ServerReadiness,
    options: GatewayOptions,
) -> None:
    """Open the one-time local workbench URL after Uvicorn accepts requests."""

    while not server.started and not server.should_exit:
        await asyncio.sleep(0.05)
    if not server.started or server.should_exit:
        return
    try:
        await asyncio.to_thread(webbrowser.open, options.bootstrap_url, new=2)
    except (OSError, webbrowser.Error):
        return


async def _run_pet_when_ready(server: _ServerReadiness, launcher: PetLauncher) -> None:
    """Launch the optional desktop pet after Uvicorn accepts requests."""

    while not server.started and not server.should_exit:
        await asyncio.sleep(0.05)
    if not server.started or server.should_exit:
        return
    await launcher.run()


async def run_gateway(options: GatewayOptions | None = None) -> None:
    """Run one loopback Gateway process and recover durable queued work."""

    import uvicorn

    gateway_options = options or GatewayOptions()
    settings = Settings.load()
    provider_settings = ProviderSettingsStore(settings.data_dir)
    runtime_application = build_cli_application(
        settings,
        _load_prompt("system_prompt.md"),
        _load_prompt("soul.md"),
        _load_prompt("compact_prompt.md"),
        provider_configuration=provider_settings.load(),
    )
    await runtime_application.runtime.recover_pending()
    await runtime_application.scheduler.recover()
    runtime_application.scheduler.start()
    tokens = BootstrapTokenRegistry()
    tokens.issue("browser", token=gateway_options.bootstrap_token, ttl_seconds=300)
    active_sessions = ActiveSessionCoordinator(runtime_application.session_service)
    server = uvicorn.Server(
        uvicorn.Config(
            create_gateway(
                sessions=runtime_application.session_service,
                runtime=runtime_application.runtime,
                attachments=runtime_application.attachment_service,
                scheduler=runtime_application.scheduler,
                archive=runtime_application.archive_service,
                workspace=runtime_application.workspace_service,
                downloads=runtime_application.downloads,
                skills=runtime_application.skills,
                compaction=runtime_application.compaction,
                commands=runtime_application.commands,
                model_profile=settings.model_profile,
                providers=runtime_application.llm,
                provider_settings=provider_settings,
                memories=runtime_application.memory_service,
                memory_policy=runtime_application.memory_policy,
                prompts=runtime_application.prompts,
                bootstrap_tokens=tokens,
                active_sessions=active_sessions,
                options=gateway_options,
            ),
            host=gateway_options.host,
            port=gateway_options.port,
        )
    )
    browser_task = asyncio.create_task(
        _open_bootstrap_page_when_ready(server, gateway_options)
    )
    launcher = PetLauncher(
        origin=gateway_options.origin,
        web_root=_web_dist().parent,
        issue_pet_token=lambda: tokens.issue("pet", ttl_seconds=300),
    )
    pet_task = asyncio.create_task(_run_pet_when_ready(server, launcher))
    try:
        await server.serve()
    finally:
        browser_task.cancel()
        pet_task.cancel()
        await asyncio.gather(browser_task, pet_task, return_exceptions=True)
        await runtime_application.aclose()


def _load_prompt(name: str) -> str:
    content = files("sjtuclaw.prompts").joinpath(name).read_text(encoding="utf-8").strip()
    if not content:
        raise ValueError(f"packaged prompt is empty: {name}")
    return content


def main() -> None:
    asyncio.run(run_gateway())


if __name__ == "__main__":
    main()
