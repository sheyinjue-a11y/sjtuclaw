"""Silent post-answer orchestration for automatic long-term memory."""

from __future__ import annotations

import logging

from sjtuclaw.errors import LLMError, StoreError
from sjtuclaw.models.message import MessageRole, UserInputKind
from sjtuclaw.models.runtime import RuntimeRequest
from sjtuclaw.models.session import RunStatus, Session
from sjtuclaw.runtime.memory_extractor import (
    MemoryExtractionError,
    MemoryExtractor,
    MemoryTurn,
)
from sjtuclaw.runtime.memory_policy import MemoryPolicyService
from sjtuclaw.runtime.memory_retriever import LocalMemoryRetriever
from sjtuclaw.runtime.memory_service import MemoryService

logger = logging.getLogger(__name__)


class MemoryCapture:
    """Extract and apply memory from one completed user turn."""

    def __init__(
        self,
        *,
        policy: MemoryPolicyService,
        retriever: LocalMemoryRetriever,
        extractor: MemoryExtractor,
        service: MemoryService,
    ) -> None:
        self._policy = policy
        self._retriever = retriever
        self._extractor = extractor
        self._service = service

    async def capture(self, session: Session, request: RuntimeRequest) -> None:
        assembled = self._assemble_turn(session, request)
        if assembled is None:
            return
        turn, source_message_id = assembled
        try:
            rules = self._policy.get().rules
            existing = self._retriever.retrieve(turn.user_message)
            operations = await self._extractor.extract(
                turn,
                rules=rules,
                existing=existing,
            )
            self._service.apply_automatic_batch(
                operations,
                source_session_id=session.session_id,
                source_message_id=source_message_id,
            )
        except (LLMError, StoreError, MemoryExtractionError, ValueError) as error:
            logger.warning(
                "automatic memory capture skipped after %s",
                type(error).__name__,
            )

    @staticmethod
    def _assemble_turn(
        session: Session,
        request: RuntimeRequest,
    ) -> tuple[MemoryTurn, str] | None:
        run = next(
            (
                item
                for item in reversed(session.runs)
                if item.request_id == request.request_id
                and item.status is RunStatus.COMPLETED
                and item.assistant_message_id is not None
            ),
            None,
        )
        if run is None:
            return None
        assistant_index = next(
            (
                index
                for index, message in enumerate(session.messages)
                if message.message_id == run.assistant_message_id
                and message.role is MessageRole.ASSISTANT
            ),
            None,
        )
        if assistant_index is None:
            return None
        user_index = next(
            (
                index
                for index in range(assistant_index - 1, -1, -1)
                if session.messages[index].role is MessageRole.USER
                and session.messages[index].input_kind is UserInputKind.USER
            ),
            None,
        )
        if user_index is None:
            return None
        user = session.messages[user_index]
        assistant = session.messages[assistant_index]
        tool_results = tuple(
            message.content
            for message in session.messages[user_index + 1 : assistant_index]
            if message.role is MessageRole.TOOL
        )
        return (
            MemoryTurn(
                user_message=user.content,
                assistant_answer=assistant.content,
                tool_results=tool_results,
            ),
            user.message_id,
        )
