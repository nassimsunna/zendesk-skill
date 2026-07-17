"""Read-only Zendesk Talk analytics helpers."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import parse_qsl, urlparse

from zendesk_skill.client import ZendeskAPIError, ZendeskClient

CALLS_ENDPOINT = "channels/voice/stats/incremental/calls"
LEGS_ENDPOINT = "channels/voice/stats/incremental/legs"
REQUEST_INTERVAL_SECONDS = 6.0  # Zendesk Talk incremental stats limit: 10 requests/minute.
MAX_INCREMENTAL_PAGES = 1000


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


def _is_date_only(value: str) -> bool:
    stripped = value.strip()
    return len(stripped) == 10 and stripped[4] == "-" and stripped[7] == "-"


def parse_end_datetime(value: str) -> datetime:
    """Parse an end date as an exclusive upper bound.

    Date-only values include the whole requested day by advancing to the next
    midnight. Full datetime values keep their exact instant as the exclusive
    boundary.
    """
    parsed = parse_datetime(value)
    if _is_date_only(value):
        return parsed + timedelta(days=1)
    return parsed


def _unix_seconds(value: str) -> int:
    return int(parse_datetime(value).timestamp())


def _first_datetime_field(record: dict[str, Any], fields: tuple[str, ...], numeric_fields: tuple[str, ...] = ()) -> datetime | None:
    for field in fields:
        value = record.get(field)
        if isinstance(value, str):
            try:
                return parse_datetime(value)
            except ValueError:
                continue
    for field in numeric_fields:
        value = record.get(field)
        if isinstance(value, (int, float)):
            return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _export_datetime(record: dict[str, Any]) -> datetime | None:
    """Timestamp used for incremental export retrieval and pagination progress.

    Zendesk Talk incremental exports are driven by records created or updated
    since start_time. This is intentionally separate from reporting time so an
    old call updated in the export window is retrieved without pretending that
    the customer called at the update time.
    """
    return _first_datetime_field(
        record,
        ("updated_at", "created_at", "modified_at", "last_updated_at"),
        ("updated_timestamp", "timestamp"),
    )


def _reporting_datetime(record: dict[str, Any], item_key: str = "calls") -> datetime | None:
    """Timestamp used for call occurrence reporting and date/hour breakdowns."""
    if item_key == "legs":
        return _first_datetime_field(
            record,
            ("started_at", "leg_started_at", "call_started_at", "created_at", "time"),
            ("start_time", "timestamp"),
        )
    return _first_datetime_field(
        record,
        ("started_at", "call_started_at", "created_at", "time"),
        ("start_time", "timestamp"),
    )


def _in_export_range(record: dict[str, Any], start: datetime, end_exclusive: datetime) -> bool:
    when = _export_datetime(record)
    return when is None or start <= when < end_exclusive


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


def _page_reaches_end(page: dict[str, Any], page_items: list[dict[str, Any]], end_exclusive: datetime) -> bool:
    response_end_time = page.get("end_time")
    if isinstance(response_end_time, (int, float)):
        if datetime.fromtimestamp(response_end_time, tz=timezone.utc) >= end_exclusive:
            return True

    record_times = [when for item in page_items if (when := _export_datetime(item)) is not None]
    return bool(record_times and max(record_times) >= end_exclusive)


def _stable_record_key(record: dict[str, Any], item_key: str) -> str | None:
    if item_key == "calls":
        candidates = ("id", "call_id")
    elif item_key == "legs":
        candidates = ("id", "leg_id", "call_leg_id")
    else:
        candidates = ("id",)
    for candidate in candidates:
        value = record.get(candidate)
        if value is not None:
            return f"{item_key}:{value}"
    return None


def _page_key(endpoint: str, params: dict[str, Any] | None) -> str:
    return f"{endpoint}?{sorted(params.items()) if params else []}"


async def fetch_incremental_with_metadata(client: ZendeskClient, endpoint: str, item_key: str, start_date: str, end_date: str) -> dict[str, Any]:
    """Fetch a Talk incremental endpoint and include safe pagination metadata."""
    start_dt = parse_datetime(start_date)
    end_dt = parse_end_datetime(end_date)
    if end_dt <= start_dt:
        raise ValueError("end_date must be after start_date")

    params: dict[str, Any] = {"start_time": _unix_seconds(start_date)}
    items: list[dict[str, Any]] = []
    seen_record_keys: set[str] = set()
    next_endpoint = endpoint
    seen_pages: set[str] = set()
    pages_fetched = 0
    reached_live_tail = False
    requested_end_reached = False
    empty_page_completion = False
    termination_reason = "no_next_page"

    while next_endpoint:
        pages_fetched += 1
        if pages_fetched > MAX_INCREMENTAL_PAGES:
            raise ZendeskAPIError(f"Stopped Zendesk Talk pagination after {MAX_INCREMENTAL_PAGES} pages to avoid an infinite loop.")

        page_key = _page_key(next_endpoint, params)
        if page_key in seen_pages:
            reached_live_tail = True
            termination_reason = "live_tail_repeated_cursor"
            break
        seen_pages.add(page_key)

        page = await _get_with_retry(client, next_endpoint, params=params)
        page_items = _items_from_response(page, item_key)
        if page.get("count") == 0 or not page_items:
            empty_page_completion = True
            termination_reason = "empty_page"
            break

        for item in page_items:
            if not _in_export_range(item, start_dt, end_dt):
                continue
            record_key = _stable_record_key(item, item_key)
            if record_key is not None:
                if record_key in seen_record_keys:
                    continue
                seen_record_keys.add(record_key)
            items.append(item)

        if _page_reaches_end(page, page_items, end_dt):
            requested_end_reached = True
            termination_reason = "requested_end_reached"
            break

        next_page = page.get("next_page")
        if next_page:
            candidate_endpoint, candidate_params = _endpoint_from_next_page(str(next_page), client)
            candidate_key = _page_key(candidate_endpoint, candidate_params)
            if candidate_key in seen_pages:
                reached_live_tail = True
                termination_reason = "live_tail_repeated_cursor"
                break
            next_endpoint, params = candidate_endpoint, candidate_params
        else:
            termination_reason = "no_next_page"
            next_endpoint = ""

    return {
        item_key: items,
        "metadata": {
            "reached_live_tail": reached_live_tail,
            "pages_fetched": pages_fetched,
            "records_returned": len(items),
            "requested_end_reached": requested_end_reached,
            "empty_page_completion": empty_page_completion,
            "termination_reason": termination_reason,
        },
    }


async def fetch_incremental(client: ZendeskClient, endpoint: str, item_key: str, start_date: str, end_date: str) -> list[dict[str, Any]]:
    """Fetch a read-only Talk incremental endpoint across safe next_page pagination."""
    result = await fetch_incremental_with_metadata(client, endpoint, item_key, start_date, end_date)
    return result[item_key]


def _leg_id(record: dict[str, Any]) -> str | None:
    value = record.get("id") or record.get("leg_id") or record.get("call_leg_id")
    return str(value) if value is not None else None


def _dedupe_legs(legs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for leg in legs:
        leg_id = _leg_id(leg)
        if leg_id is not None:
            if leg_id in seen:
                continue
            seen.add(leg_id)
        deduped.append(leg)
    return deduped


def filter_legs_for_calls(legs: list[dict[str, Any]], calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    call_ids = {_call_id(call) for call in calls if _call_id(call)}
    return _dedupe_legs([leg for leg in legs if _leg_call_id(leg) in call_ids])


async def fetch_relevant_legs_for_calls(client: ZendeskClient, calls: list[dict[str, Any]], start_date: str, end_date: str) -> dict[str, Any]:
    """Fetch legs broadly enough to join older legs for returned calls.

    Calls can be returned because their call record was updated during the
    export window, while their agent legs were created/updated earlier. Use one
    paginated Talk legs export starting at the earliest returned call occurrence
    timestamp, then keep only legs whose call_id belongs to returned calls.
    """
    call_ids = {_call_id(call) for call in calls if _call_id(call)}
    if not call_ids:
        return {"legs": [], "metadata": {"records_returned": 0, "joined_call_ids": 0}}

    export_start = parse_datetime(start_date)
    candidate_times = [
        when
        for call in calls
        if (when := (_reporting_datetime(call, "calls") or _export_datetime(call))) is not None
    ]
    leg_start = min(candidate_times, default=export_start)
    if leg_start > export_start:
        leg_start = export_start
    leg_start_text = leg_start.isoformat().replace("+00:00", "Z")
    result = await fetch_incremental_with_metadata(client, LEGS_ENDPOINT, "legs", leg_start_text, end_date)
    legs = filter_legs_for_calls(result["legs"], calls)
    metadata = {**result["metadata"], "records_returned": len(legs), "joined_call_ids": len(call_ids), "leg_export_start": leg_start_text}
    return {"legs": legs, "metadata": metadata}


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
    elif call.get("overflowed") is True:
        outcome = "overflowed"
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
    missed_statuses = {"missed", "agent_missed"}
    declined_statuses = {"declined", "agent_declined", "agent_transfer_declined"}
    return {"agent_id": _agent_id(leg), "call_id": _leg_call_id(leg), "type": leg_type, "status": status, "agent_missed": status in missed_statuses, "agent_declined": status in declined_statuses, "agent_accepted": status == "accepted", "agent_completed": status == "completed"}


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
                "ivr_time": _duration(call, "ivr_time_spent", "ivr_time", "ivr_time_in_seconds"),
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
        when = _reporting_datetime(call, "calls")
        key = "unknown"
        if by == "date" and when:
            key = when.date().isoformat()
        elif by == "hour" and when:
            key = when.strftime("%Y-%m-%dT%H:00:00Z")
        elif by == "outcome":
            key = row["classification"]["outcome"]
        elif by == "phone_line":
            key = str(call.get("phone_number_id") or call.get("phone_number") or call.get("phone_line_id") or call.get("line_id") or call.get("line_name") or "unknown")
        elif by == "group":
            key = str(call.get("call_group_id") or call.get("group_id") or call.get("group_name") or call.get("group") or "unknown")
        elif by == "agent":
            ids = sorted({summarize_leg(leg).get("agent_id") or "unknown" for leg in row["legs"] if str(leg.get("type") or leg.get("leg_type") or "").lower() == "agent"})
            key = ",".join(ids) if ids else "unknown"
        bucket = buckets.setdefault(key, {"key": key, "count": 0, "outcomes": defaultdict(int)})
        bucket["count"] += 1
        bucket["outcomes"][row["classification"]["outcome"]] += 1
    return [{**bucket, "outcomes": dict(bucket["outcomes"])} for bucket in buckets.values()]
