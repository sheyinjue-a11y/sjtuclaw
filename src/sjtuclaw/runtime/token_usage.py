"""Accumulate provider usage into one durable Session total."""

from dataclasses import replace
from datetime import datetime

from sjtuclaw.models.llm import LLMUsage
from sjtuclaw.models.session import Session


def accumulate_token_usage(session: Session, usage: LLMUsage, tracked_at: datetime) -> Session:
    cache_available = (
        usage.prompt_cache_hit_tokens is not None
        and usage.prompt_cache_miss_tokens is not None
    )
    return replace(
        session,
        prompt_tokens=session.prompt_tokens + usage.prompt_tokens,
        prompt_cache_hit_tokens=(
            session.prompt_cache_hit_tokens
            + (usage.prompt_cache_hit_tokens or 0)
        ),
        prompt_cache_miss_tokens=(
            session.prompt_cache_miss_tokens
            + (usage.prompt_cache_miss_tokens or 0)
        ),
        completion_tokens=session.completion_tokens + usage.completion_tokens,
        cache_breakdown_complete=session.cache_breakdown_complete and cache_available,
        token_tracking_started_at=session.token_tracking_started_at or tracked_at,
    )
