"""Deterministic normalization of bounded memory time expressions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

_RELATIVE_DAY = re.compile(r"^(今天|明天|后天)\s*(.*)$")
_NEXT_WEEKDAY = re.compile(r"^下周([一二三四五六日天])\s*(.*)$")
_ISO_WITH_IANA = re.compile(r"^(.*)\[([A-Za-z0-9._+-]+(?:/[A-Za-z0-9._+-]+)+)\]$")
_TIME = re.compile(
    r"^(?:(凌晨|早上|上午|中午|下午|晚上)\s*)?"
    r"([0-9]{1,2}|[零〇一二三四五六七八九十两]{1,3})"
    r"(?::([0-9]{1,2})|点"
    r"(?:(零|[0-9]{1,2}|[零〇一二三四五六七八九十两]{1,3})分?)?"
    r")?$"
)
_WEEKDAY_INDEX = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}
_CHINESE_DIGITS = {
    "零": 0,
    "〇": 0,
    "一": 1,
    "二": 2,
    "两": 2,
    "三": 3,
    "四": 4,
    "五": 5,
    "六": 6,
    "七": 7,
    "八": 8,
    "九": 9,
}


@dataclass(frozen=True, slots=True)
class NormalizedMemoryTime:
    original_time_text: str | None
    event_at: datetime | None
    event_timezone: str | None


def normalize_memory_time(
    original_time_text: str | None,
    *,
    now: datetime,
    timezone: ZoneInfo,
) -> NormalizedMemoryTime:
    """Normalize a small unambiguous subset and preserve everything else."""
    if original_time_text is None:
        return NormalizedMemoryTime(None, None, None)
    if type(original_time_text) is not str:
        raise ValueError("original_time_text must be text or None")
    if not original_time_text.strip():
        raise ValueError("original_time_text must not be empty")
    if not isinstance(timezone, ZoneInfo):
        raise ValueError("timezone must be a ZoneInfo")
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")

    text = original_time_text.strip()
    absolute = _absolute_iso(text, fallback_timezone=timezone)
    if absolute is not None:
        event_at, zone_name = absolute
        return NormalizedMemoryTime(original_time_text, event_at, zone_name)

    local_now = now.astimezone(timezone)
    relative_match = _RELATIVE_DAY.fullmatch(text)
    if relative_match is not None:
        day_offset = {"今天": 0, "明天": 1, "后天": 2}[relative_match.group(1)]
        target_date = local_now.date() + timedelta(days=day_offset)
        local_time = _parse_time(relative_match.group(2))
        if local_time is not None:
            return _normalized_relative(
                original_time_text,
                target_date,
                local_time,
                timezone,
            )

    weekday_match = _NEXT_WEEKDAY.fullmatch(text)
    if weekday_match is not None:
        monday = local_now.date() - timedelta(days=local_now.weekday())
        target_date = monday + timedelta(days=7 + _WEEKDAY_INDEX[weekday_match.group(1)])
        local_time = _parse_time(weekday_match.group(2))
        if local_time is not None:
            return _normalized_relative(
                original_time_text,
                target_date,
                local_time,
                timezone,
            )

    return NormalizedMemoryTime(original_time_text, None, None)


def _absolute_iso(
    text: str,
    *,
    fallback_timezone: ZoneInfo,
) -> tuple[datetime, str] | None:
    zone = fallback_timezone
    iso_text = text
    iana_match = _ISO_WITH_IANA.fullmatch(text)
    if iana_match is not None:
        iso_text, zone_name = iana_match.groups()
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            return None
    try:
        parsed = datetime.fromisoformat(iso_text)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        if iana_match is None:
            return None
        parsed = parsed.replace(tzinfo=zone)
    return parsed.astimezone(UTC), zone.key


def _normalized_relative(
    original: str,
    target_date: date,
    target_time: time,
    timezone: ZoneInfo,
) -> NormalizedMemoryTime:
    local = datetime.combine(target_date, target_time, tzinfo=timezone)
    return NormalizedMemoryTime(original, local.astimezone(UTC), timezone.key)


def _parse_time(value: str) -> time | None:
    if not value:
        return time()
    match = _TIME.fullmatch(value)
    if match is None:
        return None
    period, raw_hour, colon_minute, point_minute = match.groups()
    hour = _number(raw_hour)
    minute = _number(colon_minute or point_minute or "0")
    if hour is None or minute is None or not 0 <= minute <= 59:
        return None
    if period is None:
        if not 0 <= hour <= 23:
            return None
    elif not 1 <= hour <= 12:
        return None
    elif period in {"下午", "晚上"} and hour < 12:
        hour += 12
    elif period in {"凌晨", "早上", "上午"} and hour == 12:
        hour = 0
    elif period == "中午" and hour < 11:
        hour += 12
    return time(hour, minute)


def _number(value: str) -> int | None:
    if value.isascii() and value.isdecimal():
        return int(value)
    if value in _CHINESE_DIGITS:
        return _CHINESE_DIGITS[value]
    if "十" not in value:
        digits = [_CHINESE_DIGITS.get(character) for character in value]
        if any(digit is None for digit in digits):
            return None
        return int("".join(str(digit) for digit in digits))
    tens, ones = value.split("十", 1)
    tens_value = 1 if not tens else _CHINESE_DIGITS.get(tens)
    ones_value = 0 if not ones else _CHINESE_DIGITS.get(ones)
    if tens_value is None or ones_value is None:
        return None
    return tens_value * 10 + ones_value
