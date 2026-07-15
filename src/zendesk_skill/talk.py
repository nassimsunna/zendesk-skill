"""Read-only Zendesk Talk analytics helpers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse

from zendesk_skill.client import ZendeskAPIError, ZendeskClient

CALLS_ENDPOINT = "channels/voice/stats/incremental/calls"
LEGS_ENDPOINT = "channels/voice/stats/incremental/legs"
REQUEST_INTERVAL_SECONDS = 6.0  # Zendesk Talk incremental stats limit: 10 requests/minute.


@dataclass
class TalkRateLimiter:
    """Small async limiter for Zendesk Talk's 10-requests-per-minute limit."""

    interval_seconds: float = REQUEST_INTERVAL_SECONDS
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    _last_request_at: float = 0.0

    async def wait(self) -> None:
        loop = asyncio.get_running_loop()
        async with self._lock:
            elapsed = loop.time() - self._last_request_at
            delay = self.interval_seconds - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = loop.time()


_rate_limiter = TalkRateLimiter()


def parse_datetime(value: str) -> datetime:
    """Parse an ISO date/datetime as a timezone-aware UTC datetime."""
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ValueError(f"Invalid date/datetime: {value!r}. Use ISO format like 2026-01-31 or 2026-01-31T15:00:00Z.") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _unix_seconds(value: str) -> int:
    return int(parse_datetime(value).timestamp())


def _record_datetime(record: dict[str, Any]) -> datetime | None:
    for field in ("created_at", "started_at", "updated_at", "call_started_at", "time"):
        value = record.get(field)
        if isinstance(value, str):
            try:
                return parse_datetime(value)
            except ValueError:
                continue
    timestamp = record.get("timestamp") or record.get("start_time")
    if isinstance(timestamp, (int, float)):
        return datetime.fromtimestamp(timestamp, tz=timezone.utc)
    return None


def _in_range(record: dict[str, Any], start: datetime, end: datetime) -> bool:
    when = _record_datetime(record)
    return when is None or start <= when <= end


def _items_from_response(data: dict[str, Any], item_key: str) -> list[dict[str, Any]]:
    value = data.get(item_key)
    if isinstance(value, list):
        return value
    # Some fake/test payloads may use singular or generic names.
    for key in ("records", "results", "data"):
        if isinstance(data.get(key), list):
            return data[key]
    return []


def _endpoint_from_next_page(next_page: str, client: ZendeskClient) -> tuple[str, dict[str, Any]]:
    parsed = urlparse(next_page)
    expected_host = f"{client._auth_provider.subdomain}.zendesk.com"
    if parsed.scheme != "https" or parsed.netloc.lower() != expected_host.lower():
        raise ZendeskAPIError("Refusing unsafe Zendesk pagination URL outside the configured subdomain.")
    prefix = "/api/v2/"
    if not parsed.path.startswith(prefix):
        raise ZendeskAPIError("Refusing unsafe Zendesk pagination URL outside /api/v2/.")
    return parsed.path.removeprefix(prefix), dict(parse_qsl(parsed.query, keep_blank_values=True))


async def _get_with_retry(client: ZendeskClient, endpoint: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
    await _rate_limiter.wait()
    try:
        return await client.get(endpoint, params=params)
    except ZendeskAPIError as exc:
        retry_after = None
        response = getattr(getattr(exc, "__cause__", None), "response", None)
        if response is not None:
            retry_after = response.headers.get("Retry-After")
        if exc.status_code == 429 and retry_after:
            try:
                await asyncio.sleep(float(retry_after))
            except ValueError:
                await asyncio.sleep(REQUEST_INTERVAL_SECONDS)
            await _rate_limiter.wait()
            return await client.get(endpoint, params=params)
        raise


async def fetch_incremental(client: ZendeskClient, endpoint: str, item_key: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Fetch a read-only Talk incremental endpoint across safe next_page pagination."""
    start_dt = parse_datetime(start_date)
    end_dt = parse_datetime(end_date)
    if end_dt < start_dt:
        raise ValueError("end_date must be after start_date")

    params: dict[str, Any] = {"start_time": _unix_seconds(start_date), "end_time": _unix_seconds(end_date)}
    items: list[dict[str, Any]] = []
    next_endpoint = endpoint
    while next_endpoint:
        page = await _get_with_retry(client, next_endpoint, params=params)
        items.extend(item for item in _items_from_response(page, item_key) if _in_range(item, start_dt, end_dt))
        next_page = page.get("next_page")
        if next_page:
            next_endpoint, params = _endpoint_from_next_page(str(next_page), client)
        else:
            next_endpoint = ""
    return items


def _duration(record: dict[str, Any], *names: str) -> int | float | None:
    for name in names:
        value = record.get(name)
        if isinstance(value, (int, float)):
            return value
    return None


def _call_id(record: dict[str, Any]) -> str | None:
    value = record.get("call_id") or record.get("id")
    return str(value) if value is not None else None


def _leg_call_id(record: dict[str, Any]) -> str | None:
    value = record.get("call_id") or record.get("callId")
    return str(value) if value is not None else None


def _agent_id(leg: dict[str, Any]) -> str | None:
    value = leg.get("agent_id") or leg.get("user_id") or leg.get("assignee_id")
    return str(value) if value is not None else None


def classify_call(call: dict[str, Any], legs: list[dict[str, Any]]) -> dict[str, Any]:
    """Classify a call while preserving Zendesk's original completion status."""
    status = str(call.get("completion_status") or call.get("status") or "").lower()
    talk_time = _duration(call, "talk_time", "talk_time_in_seconds", "agent_talk_time") or 0
    completed_agent_legs = [leg for leg in legs if str(leg.get("type") or leg.get("leg_type") or "").lower() == "agent" and str(leg.get("completion_status") or leg.get("status") or "").lower() == "completed"]
    answered_by_agent = bool(talk_time and completed_agent_legs)

    outcome = "other"
    if answered_by_agent:
        outcome = "answered_by_agent"
    elif "abandoned" in status:
        if "ivr" in status:
            outcome = "abandoned_in_ivr"
        elif "queue" in status:
            outcome = "abandoned_in_queue"
        elif "hold" in status:
            outcome = "abandoned_on_hold"
        elif "voicemail" in status:
            outcome = "abandoned_in_voicemail"
        else:
            outcome = "abandoned"
    elif "voicemail" in status:
        outcome = "voicemail"
    elif "overflow" in status:
        outcome = "overflowed"
    elif "failed" in status:
        outcome = "failed"

    return {"outcome": outcome, "answered_by_agent": answered_by_agent, "zendesk_completion_status": call.get("completion_status") or call.get("status")}


def summarize_leg(leg: dict[str, Any]) -> dict[str, Any]:
    status = str(leg.get("completion_status") or leg.get("status") or "").lower()
    leg_type = str(leg.get("type") or leg.get("leg_type") or "").lower()
    return {"agent_id": _agent_id(leg), "call_id": _leg_call_id(leg), "type": leg_type, "status": status, "agent_missed": status == "missed", "agent_declined": status == "declined", "agent_accepted": status == "accepted", "agent_completed": status == "completed"}


def join_calls_and_legs(calls: list[dict[str, Any]], legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    by_call: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for leg in legs:
        cid = _leg_call_id(leg)
        if cid:
            by_call[cid].append(leg)
    rows = []
    for call in calls:
        cid = _call_id(call)
        call_legs = by_call.get(cid or "", [])
        classification = classify_call(call, call_legs)
        rows.append({
            "call": call,
            "legs": call_legs,
            "classification": classification,
            "ticket_id": call.get("ticket_id"),
            "ticket_url": f"/tickets/{call.get('ticket_id')}" if call.get("ticket_id") else None,
            "metrics": {
                "queue_wait_time": _duration(call, "queue_wait_time", "queue_wait_time_in_seconds", "wait_time"),
                "time_to_answer": _duration(call, "time_to_answer", "time_to_answer_in_seconds"),
                "ivr_time": _duration(call, "ivr_time", "ivr_time_in_seconds"),
                "talk_time": _duration(call, "talk_time", "talk_time_in_seconds"),
                "hold_time": _duration(call, "hold_time", "hold_time_in_seconds"),
                "wrap_up_time": _duration(call, "wrap_up_time", "wrap_up_time_in_seconds"),
            },
            "ivr": {key: call.get(key) for key in ("ivr_action", "ivr_destination_group_name", "ivr_hops", "ivr_routed_to") if key in call},
        })
    return rows


def breakdown(rows: list[dict[str, Any]], by: str) -> list[dict[str, Any]]:
    buckets: dict[str, dict[str, Any]] = {}
    for row in rows:
        call = row["call"]
        when = _record_datetime(call)
        key = "unknown"
        if by == "date" and when:
            key = when.date().isoformat()
        elif by == "hour" and when:
            key = when.strftime("%Y-%m-%dT%H:00:00Z")
        elif by == "outcome":
            key = row["classification"]["outcome"]
        elif by == "phone_line":
            key = str(call.get("phone_line_id") or call.get("line_id") or call.get("from") or "unknown")
        elif by == "group":
            key = str(call.get("group_id") or call.get("group_name") or "unknown")
        elif by == "agent":
            ids = sorted({summarize_leg(leg).get("agent_id") or "unknown" for leg in row["legs"] if str(leg.get("type") or leg.get("leg_type") or "").lower() == "agent"})
            key = ",".join(ids) if ids else "unknown"
        bucket = buckets.setdefault(key, {"key": key, "count": 0, "outcomes": defaultdict(int)})
        bucket["count"] += 1
        bucket["outcomes"][row["classification"]["outcome"]] += 1
    return [{**bucket, "outcomes": dict(bucket["outcomes"])} for bucket in buckets.values()]
