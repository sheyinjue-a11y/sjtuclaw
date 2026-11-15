"""Gateway-owned fan-out and bounded replay for one RuntimeHost run."""

from __future__ import annotations

import asyncio
import re
from collections import deque
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import cast

from sjtuclaw.models.runtime import RuntimeEvent, RuntimeEventType
from sjtuclaw.runtime import RunHandle

_TERMINAL_EVENTS = frozenset(
    {
        RuntimeEventType.RUN_COMPLETED,
        RuntimeEventType.RUN_FAILED,
        RuntimeEventType.RUN_CANCELLED,
    }
)
_DOWNLOAD_HREF = re.compile(r"^/downloads/[A-Za-z0-9_-]{1,256}$")


@dataclass(frozen=True, slots=True)
class GatewayEvent:
    """One browser-safe, replayable representation of a Runtime event."""

    event_id: int
    event_type: str
    session_id: str
    request_id: str
    run_id: str
    sequence_number: int
    occurred_at: datetime
    payload: dict[str, object]

    def as_dict(self) -> dict[str, object]:
        return {
            "schemaVersion": 1,
            "eventId": self.event_id,
            "sessionId": self.session_id,
            "requestId": self.request_id,
            "runId": self.run_id,
            "sequenceNumber": self.sequence_number,
            "occurredAt": self.occurred_at.isoformat(),
            "payload": self.payload,
        }


class GatewaySubscription:
    """One SSE consumer; closing it never alters the underlying Run."""

    def __init__(
        self,
        stream: _RunStream,
        replay: tuple[GatewayEvent, ...],
        queue: asyncio.Queue[GatewayEvent | None],
        replay_window_expired: bool,
    ) -> None:
        self._stream = stream
        self._replay = deque(replay)
        self._queue = queue
        self.replay_window_expired = replay_window_expired
        self._closed = False

    async def events(self) -> AsyncGenerator[GatewayEvent, None]:
        try:
            while self._replay:
                yield self._replay.popleft()
            while True:
                event = await self._queue.get()
                if event is None:
                    return
                yield event
        finally:
            self.aclose()

    def aclose(self) -> None:
        if not self._closed:
            self._closed = True
            self._stream.remove(self._queue)


class GatewayEventHub:
    """Own runtime event consumption and preserve a small per-run replay window."""

    def __init__(self, *, replay_limit: int = 256) -> None:
        if replay_limit < 1:
            raise ValueError("replay_limit must be positive")
        self._replay_limit = replay_limit
        self._streams: dict[tuple[str, str], _RunStream] = {}

    def subscribe(
        self,
        *,
        session_id: str,
        request_id: str,
        run_id: str,
        handle: RunHandle,
        last_event_id: int | None,
    ) -> GatewaySubscription:
        key = (session_id, run_id)
        stream = self._streams.get(key)
        if stream is None:
            stream = _RunStream(
                session_id=session_id,
                request_id=request_id,
                run_id=run_id,
                handle=handle,
                replay_limit=self._replay_limit,
            )
            self._streams[key] = stream
        return stream.subscribe(last_event_id)

    async def aclose(self) -> None:
        streams = tuple(self._streams.values())
        for stream in streams:
            stream.cancel()
        tasks = tuple(stream.task for stream in streams if stream.task is not None)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class _RunStream:
    def __init__(
        self,
        *,
        session_id: str,
        request_id: str,
        run_id: str,
        handle: RunHandle,
        replay_limit: int,
    ) -> None:
        self._session_id = session_id
        self._request_id = request_id
        self._run_id = run_id
        self._handle = handle
        self._events: deque[GatewayEvent] = deque(maxlen=replay_limit)
        self._subscribers: set[asyncio.Queue[GatewayEvent | None]] = set()
        self._next_event_id = 1
        self._done = False
        self.task: asyncio.Task[None] | None = None

    def subscribe(self, last_event_id: int | None) -> GatewaySubscription:
        if self.task is None:
            self.task = asyncio.create_task(self._pump())
        replay_window_expired = (
            last_event_id is not None
            and bool(self._events)
            and last_event_id < self._events[0].event_id - 1
        )
        replay = (
            ()
            if replay_window_expired
            else tuple(
                event
                for event in self._events
                if last_event_id is None or event.event_id > last_event_id
            )
        )
        queue: asyncio.Queue[GatewayEvent | None] = asyncio.Queue()
        if not self._done:
            self._subscribers.add(queue)
        else:
            queue.put_nowait(None)
        return GatewaySubscription(self, replay, queue, replay_window_expired)

    def remove(self, queue: asyncio.Queue[GatewayEvent | None]) -> None:
        self._subscribers.discard(queue)

    def cancel(self) -> None:
        if self.task is not None:
            self.task.cancel()

    async def _pump(self) -> None:
        try:
            async for event in self._handle.events():
                gateway_event = GatewayEvent(
                    event_id=self._next_event_id,
                    event_type=event.event_type.value,
                    session_id=self._session_id,
                    request_id=self._request_id,
                    run_id=self._run_id,
                    sequence_number=event.sequence_number,
                    occurred_at=datetime.now(UTC),
                    payload=_public_payload(event),
                )
                self._next_event_id += 1
                self._events.append(gateway_event)
                for queue in tuple(self._subscribers):
                    queue.put_nowait(gateway_event)
        finally:
            self._done = True
            for queue in tuple(self._subscribers):
                queue.put_nowait(None)


def _json_safe(value: object) -> object:
    """Convert frozen runtime payload containers back to JSON-safe ones.

    RuntimeHost freezes event payloads into MappingProxyType / tuple before
    publishing; the browser stream must project plain dict / list values or
    json.dumps fails and tears down the whole SSE connection.
    """

    if isinstance(value, Mapping):
        return {
            key: _json_safe(item)
            for key, item in value.items()
            if isinstance(key, str)
        }
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _public_payload(event: RuntimeEvent) -> dict[str, object]:
    """Project one Runtime event to a JSON-serializable browser payload."""

    return cast(dict[str, object], _json_safe(_project_payload(event)))


def _project_payload(event: RuntimeEvent) -> dict[str, object]:
    """Expose approved local observability fields without leaking unrelated payload data."""

    payload = event.payload
    if event.event_type is RuntimeEventType.ASSISTANT_DELTA:
        return {"delta": _text(payload.get("delta"))}
    if event.event_type is RuntimeEventType.ASSISTANT_MESSAGE:
        return {
            "content": _text(payload.get("content")),
            "usage": _usage(payload.get("usage")),
        }
    if event.event_type is RuntimeEventType.RUN_PROGRESS:
        result: dict[str, object] = {"code": _text(payload.get("code"))}
        if "tool" in payload:
            result["tool"] = _text(payload.get("tool"))
        if result["code"] == "skill.selected":
            result["skillId"] = _text(payload.get("skill_id"))
            result["source"] = _text(payload.get("source"))
            result["reason"] = _text(payload.get("reason"))
        return result
    if event.event_type is RuntimeEventType.TOOL_CALL:
        return {
            "callId": _text(payload.get("call_id")),
            "name": _text(payload.get("name")),
            "input": _text(payload.get("arguments")),
        }
    if event.event_type is RuntimeEventType.TOOL_RESULT:
        tool_status = _text(payload.get("status"))
        return {
            "callId": _text(payload.get("call_id")),
            "tool": _text(payload.get("tool")),
            "status": tool_status,
            "output": _text(payload.get("content")),
            "errorCode": _optional_text(payload.get("error_code")),
            "structuredContent": _structured_content(payload.get("structured_content")),
            "resources": (
                _resources(payload.get("resources"))
                if tool_status == "success"
                else []
            ),
        }
    if event.event_type in _TERMINAL_EVENTS:
        result = {"usage": _usage(payload.get("usage"))}
        if "assistant_message_id" in payload:
            result["assistantMessageId"] = _text(payload.get("assistant_message_id"))
        if "code" in payload:
            result["code"] = _text(payload.get("code"))
        return result
    if event.event_type in {
        RuntimeEventType.STEER_ACCEPTED,
        RuntimeEventType.STEER_WAITING_SAFE_POINT,
        RuntimeEventType.STEER_APPLIED,
        RuntimeEventType.STEER_REJECTED,
    }:
        result = {"steerId": _text(payload.get("steer_id"))}
        if "code" in payload:
            result["code"] = _text(payload.get("code"))
        return result
    return {"code": _text(payload.get("code"))}


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _structured_content(value: object) -> dict[str, object] | None:
    if not isinstance(value, Mapping):
        return None
    return {key: item for key, item in value.items() if isinstance(key, str)}


def _resources(value: object) -> list[dict[str, object]]:
    if not isinstance(value, (list, tuple)):
        return []
    resources: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, Mapping):
            continue
        kind = item.get("kind")
        name = item.get("name")
        href = item.get("href")
        mime_type = item.get("mime_type")
        expires_at = item.get("expires_at")
        if (
            kind != "download"
            or not isinstance(name, str)
            or not name.strip()
            or not isinstance(href, str)
            or _DOWNLOAD_HREF.fullmatch(href) is None
            or (mime_type is not None and not isinstance(mime_type, str))
            or (expires_at is not None and not isinstance(expires_at, str))
        ):
            continue
        resources.append(
            {
                "kind": kind,
                "name": name,
                "href": href,
                "mimeType": mime_type,
                "expiresAt": expires_at,
            }
        )
    return resources


def _usage(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    return {
        key: item
        for key in ("prompt_tokens", "completion_tokens", "total_tokens")
        if isinstance((item := value.get(key)), int) and item >= 0
    }
