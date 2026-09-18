from __future__ import annotations

from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo


KST = ZoneInfo("Asia/Seoul")


def parse_clock(value: str) -> time:
    hour, minute = value.strip().split(":")
    parsed = time(int(hour), int(minute), tzinfo=KST)
    return parsed


def _combine(day: date, clock: time) -> datetime:
    return datetime(
        day.year,
        day.month,
        day.day,
        clock.hour,
        clock.minute,
        tzinfo=KST,
    )


def window_bounds(day: date, start: str, end: str) -> tuple[datetime, datetime]:
    start_clock = parse_clock(start)
    end_clock = parse_clock(end)
    start_at = _combine(day, start_clock)
    end_at = _combine(day, end_clock)
    if end_at <= start_at:
        end_at += timedelta(days=1)
    return start_at, end_at


def next_slot(
    current: datetime,
    start: str,
    end: str,
    interval_minutes: int,
) -> datetime:
    """Return the next allowed slot.

    The daily window is start-inclusive and end-exclusive. Crossing the end
    carries the slot to the following window start, including midnight wrap.
    """

    if interval_minutes < 0:
        raise ValueError("간격은 0분 이상이어야 합니다")
    if current.tzinfo is None:
        current = current.replace(tzinfo=KST)
    current = current.astimezone(KST)
    start_at, end_at = window_bounds(current.date(), start, end)
    if current < start_at:
        previous_start, previous_end = window_bounds(
            current.date() - timedelta(days=1),
            start,
            end,
        )
        if previous_start <= current < previous_end:
            start_at, end_at = previous_start, previous_end
        else:
            return start_at
    if current >= end_at:
        next_start, next_end = window_bounds(current.date(), start, end)
        if current >= next_end:
            next_start, _next_end = window_bounds(
                current.date() + timedelta(days=1),
                start,
                end,
            )
        return next_start
    return current


def plan_slots(
    *,
    count: int,
    start_date: str,
    window_start: str,
    window_end: str,
    interval_minutes: int,
    first_at: datetime | None = None,
) -> list[datetime]:
    if count < 0:
        raise ValueError("글 수는 0 이상이어야 합니다")
    if not start_date:
        day = datetime.now(KST).date()
    else:
        day = date.fromisoformat(start_date)
    cursor = first_at or window_bounds(day, window_start, window_end)[0]
    slots: list[datetime] = []
    for _ in range(count):
        cursor = next_slot(cursor, window_start, window_end, interval_minutes)
        start_at, end_at = window_bounds(cursor.date(), window_start, window_end)
        if start_at <= cursor < end_at:
            pass
        else:
            cursor = start_at
        if cursor >= end_at:
            cursor = window_bounds(end_at.date(), window_start, window_end)[0]
        slots.append(cursor)
        cursor = cursor + timedelta(minutes=interval_minutes)
    return slots
