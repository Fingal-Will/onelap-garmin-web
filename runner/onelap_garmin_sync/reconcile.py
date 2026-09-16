from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Iterable


START_TOLERANCE_SECONDS = 120
MIN_DURATION_TOLERANCE_SECONDS = 30
MIN_DISTANCE_TOLERANCE_METERS = 200


@dataclass(frozen=True)
class OneLapCandidate:
    record_id: str
    start_time_local: datetime | None
    duration_seconds: float | None
    distance_meters: float | None
    activity_type: str | None
    remote_activity_id: str | None = None

    @property
    def fingerprint_complete(self) -> bool:
        return (
            self.start_time_local is not None
            and self.duration_seconds is not None
            and self.distance_meters is not None
        )


@dataclass(frozen=True)
class GarminActivity:
    activity_id: str
    start_time_local: datetime | None
    duration_seconds: float | None
    distance_meters: float | None
    activity_type: str | None
    name: str = ""


@dataclass(frozen=True)
class ReconciliationMatch:
    record_id: str
    status: str
    remote_activity_id: str | None
    message: str


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if result >= 0 else None


def _local_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            return datetime.strptime(text[:19], fmt)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _activity_type(value: Any) -> str | None:
    if isinstance(value, dict):
        value = value.get("typeKey") or value.get("type_key") or value.get("key")
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if any(marker in text for marker in ("cycl", "bike", "biking", "ride", "骑")):
        return "cycling"
    if any(marker in text for marker in ("run", "跑")):
        return "running"
    if any(marker in text for marker in ("walk", "步行", "健走")):
        return "walking"
    if any(marker in text for marker in ("swim", "游泳")):
        return "swimming"
    return text


def candidate_from_source(
    record_id: str,
    source: dict[str, Any],
    remote_activity_id: str | None = None,
) -> OneLapCandidate:
    return OneLapCandidate(
        record_id=record_id,
        start_time_local=_local_datetime(
            source.get("start_riding_time")
            or source.get("startTimeLocal")
            or source.get("activity_time")
            or source.get("date")
        ),
        duration_seconds=_number(
            source.get("time_seconds")
            or source.get("duration")
            or source.get("elapsed_time")
        ),
        distance_meters=(
            _number(source.get("distance_meters"))
            if source.get("distance_meters") is not None
            else (
                _number(source.get("distance")) if source.get("distance") is not None
                else (
                    _number(source.get("distance_km")) * 1000
                    if _number(source.get("distance_km")) is not None
                    else None
                )
            )
        ),
        activity_type=_activity_type(
            source.get("activity_type")
            or source.get("sport_type")
            or source.get("sport_name")
            or source.get("type")
        ),
        remote_activity_id=(
            str(remote_activity_id) if remote_activity_id is not None else None
        ),
    )


def garmin_activity_from_dict(activity: dict[str, Any]) -> GarminActivity:
    activity_id = activity.get("activityId") or activity.get("activity_id")
    return GarminActivity(
        activity_id=str(activity_id or "").strip(),
        start_time_local=_local_datetime(
            activity.get("startTimeLocal") or activity.get("start_time_local")
        ),
        duration_seconds=_number(
            activity.get("durationSeconds")
            if activity.get("durationSeconds") is not None
            else activity.get("duration")
        ),
        distance_meters=_number(
            activity.get("distanceMeters")
            if activity.get("distanceMeters") is not None
            else activity.get("distance")
        ),
        activity_type=_activity_type(
            activity.get("activityType") or activity.get("activity_type")
        ),
        name=str(activity.get("activityName") or activity.get("name") or ""),
    )


def comparison_date_range(
    candidates: Iterable[OneLapCandidate],
    padding_days: int = 1,
) -> tuple[date, date] | None:
    dates = [
        candidate.start_time_local.date()
        for candidate in candidates
        if candidate.start_time_local is not None
    ]
    if not dates:
        return None
    padding = timedelta(days=max(0, padding_days))
    return min(dates) - padding, max(dates) + padding


def fingerprint_matches(
    candidate: OneLapCandidate,
    activity: GarminActivity,
) -> bool:
    if not candidate.fingerprint_complete:
        return False
    if (
        activity.start_time_local is None
        or activity.duration_seconds is None
        or activity.distance_meters is None
    ):
        return False
    start_difference = abs(
        (candidate.start_time_local - activity.start_time_local).total_seconds()
    )
    duration_tolerance = max(
        MIN_DURATION_TOLERANCE_SECONDS,
        candidate.duration_seconds * 0.01,
    )
    distance_tolerance = max(
        MIN_DISTANCE_TOLERANCE_METERS,
        candidate.distance_meters * 0.01,
    )
    types_compatible = (
        candidate.activity_type is None
        or activity.activity_type is None
        or candidate.activity_type == activity.activity_type
    )
    return (
        start_difference <= START_TOLERANCE_SECONDS
        and abs(candidate.duration_seconds - activity.duration_seconds)
        <= duration_tolerance
        and abs(candidate.distance_meters - activity.distance_meters)
        <= distance_tolerance
        and types_compatible
    )


def reconcile_activities(
    candidates: Iterable[OneLapCandidate],
    activities: Iterable[GarminActivity],
) -> list[ReconciliationMatch]:
    candidate_list = list(candidates)
    all_activities = list(activities)
    incomplete_activity_data = any(
        not activity.activity_id
        or activity.start_time_local is None
        or activity.duration_seconds is None
        or activity.distance_meters is None
        for activity in all_activities
    )
    activity_list = [activity for activity in all_activities if activity.activity_id]
    activities_by_id = {
        activity.activity_id: activity for activity in activity_list
    }
    exact_claims: dict[str, list[str]] = {}
    for candidate in candidate_list:
        if (
            candidate.remote_activity_id
            and candidate.remote_activity_id in activities_by_id
        ):
            exact_claims.setdefault(candidate.remote_activity_id, []).append(
                candidate.record_id
            )

    exact_present: dict[str, str] = {}
    exact_ambiguous: set[str] = set()
    claimed_activity_ids: set[str] = set()
    for activity_id, record_ids in exact_claims.items():
        if len(record_ids) == 1:
            exact_present[record_ids[0]] = activity_id
            claimed_activity_ids.add(activity_id)
        else:
            exact_ambiguous.update(record_ids)

    edges: dict[str, set[str]] = {}
    remote_degrees: dict[str, set[str]] = {}
    for candidate in candidate_list:
        if candidate.record_id in exact_present or candidate.record_id in exact_ambiguous:
            continue
        matches = {
            activity.activity_id
            for activity in activity_list
            if fingerprint_matches(candidate, activity)
        }
        edges[candidate.record_id] = matches
        for activity_id in matches:
            remote_degrees.setdefault(activity_id, set()).add(candidate.record_id)

    results: list[ReconciliationMatch] = []
    for candidate in candidate_list:
        if candidate.record_id in exact_present:
            activity_id = exact_present[candidate.record_id]
            results.append(
                ReconciliationMatch(
                    candidate.record_id,
                    "present",
                    activity_id,
                    "保存的 Garmin 活动 ID 仍存在",
                )
            )
            continue
        if candidate.record_id in exact_ambiguous:
            results.append(
                ReconciliationMatch(
                    candidate.record_id,
                    "ambiguous",
                    None,
                    "多个 OneLap 活动保存了同一个 Garmin 活动 ID",
                )
            )
            continue
        if not candidate.fingerprint_complete:
            results.append(
                ReconciliationMatch(
                    candidate.record_id,
                    "unverified",
                    None,
                    "OneLap 活动缺少开始时间、时长或距离，无法安全核对",
                )
            )
            continue
        if incomplete_activity_data:
            results.append(
                ReconciliationMatch(
                    candidate.record_id,
                    "unverified",
                    None,
                    "Garmin 活动列表存在缺少 ID、开始时间、时长或距离的记录，"
                    "无法安全判断缺失",
                )
            )
            continue

        matches = edges.get(candidate.record_id, set())
        if not matches:
            results.append(
                ReconciliationMatch(
                    candidate.record_id,
                    "missing",
                    None,
                    "在完整时间窗内没有找到匹配的 Garmin 活动",
                )
            )
            continue
        if len(matches) == 1:
            activity_id = next(iter(matches))
            if (
                activity_id not in claimed_activity_ids
                and len(remote_degrees.get(activity_id, set())) == 1
            ):
                results.append(
                    ReconciliationMatch(
                        candidate.record_id,
                        "present",
                        activity_id,
                        "通过开始时间、时长和距离唯一匹配",
                    )
                )
                continue
        results.append(
            ReconciliationMatch(
                candidate.record_id,
                "ambiguous",
                None,
                "存在多个可能匹配，或同一 Garmin 活动对应多个 OneLap 活动",
            )
        )
    return results
