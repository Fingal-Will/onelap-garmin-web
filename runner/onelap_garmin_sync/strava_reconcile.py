"""Strava 指纹核对：统一为 UTC，沿用一对一匹配和保守缺失判断。"""

from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from .reconcile import (
    GarminActivity,
    OneLapCandidate,
    candidate_from_source,
    garmin_activity_from_dict,
)


def utc_time(value: Any, *, onelap: bool = False) -> datetime | None:
    """OneLap 无偏移的骑行时间按北京时间解释，不依赖 runner 时区。"""
    try:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            value = value / 1000 if value > 10**11 else value
            parsed = datetime.fromtimestamp(value, timezone.utc)
        elif isinstance(value, str) and len(value.strip()) >= 16:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(
                tzinfo=ZoneInfo("Asia/Shanghai") if onelap else timezone.utc
            )
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    except (ValueError, OverflowError, OSError):
        return None


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def strava_candidate(record_id: str, source: dict[str, Any]) -> OneLapCandidate:
    candidate = candidate_from_source(record_id, source)
    started = next((source.get(key) for key in (
        "start_riding_time", "startTimeLocal", "activity_time", "date"
    ) if source.get(key) is not None), None)
    return replace(
        candidate,
        start_time_local=utc_time(started, onelap=True),
        duration_seconds=finite_number(candidate.duration_seconds),
        distance_meters=finite_number(candidate.distance_meters),
    )


def strava_activity(activity: dict[str, Any]) -> GarminActivity:
    # elapsed_time 对应文件总时长；moving_time 会扣除停顿，不能用来去重。
    normalized = garmin_activity_from_dict({
        "activityId": activity.get("id"),
        "durationSeconds": finite_number(activity.get("elapsed_time")),
        "distanceMeters": finite_number(activity.get("distance")),
        "activityType": activity.get("sport_type") or activity.get("type"),
        "name": activity.get("name"),
    })
    activity_id = str(activity.get("id") or "")
    return replace(
        normalized,
        activity_id=(activity_id if activity_id.isascii()
                     and activity_id.isdigit() and int(activity_id) > 0 else ""),
        start_time_local=utc_time(activity.get("start_date")),
    )
