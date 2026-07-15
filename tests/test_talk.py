from types import SimpleNamespace

import pytest

from zendesk_skill.client import ZendeskAPIError
from zendesk_skill.talk import (
    CALLS_ENDPOINT,
    LEGS_ENDPOINT,
    _endpoint_from_next_page,
    classify_call,
    fetch_incremental,
    join_calls_and_legs,
    summarize_leg,
)


class FakeAuth:
    subdomain = "example"


class FakeClient:
    def __init__(self, pages):
        self._auth_provider = FakeAuth()
        self.pages = list(pages)
        self.requests = []

    async def get(self, endpoint, params=None):
        self.requests.append((endpoint, params))
        result = self.pages.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.mark.asyncio
async def test_fetch_incremental_paginates_and_filters(monkeypatch):
    async def no_wait():
        return None

    monkeypatch.setattr("zendesk_skill.talk._rate_limiter.wait", no_wait)
    client = FakeClient([
        {"calls": [{"id": 1, "created_at": "2026-01-01T10:00:00Z"}], "next_page": "https://example.zendesk.com/api/v2/channels/voice/stats/incremental/calls?page=2"},
        {"calls": [{"id": 2, "created_at": "2026-02-01T10:00:00Z"}], "next_page": None},
    ])

    calls = await fetch_incremental(client, CALLS_ENDPOINT, "calls", "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")

    assert [call["id"] for call in calls] == [1]
    assert client.requests[1][0] == CALLS_ENDPOINT
    assert client.requests[1][1] == {"page": "2"}


def test_rejects_pagination_url_for_other_subdomain():
    client = SimpleNamespace(_auth_provider=FakeAuth())
    with pytest.raises(ZendeskAPIError):
        _endpoint_from_next_page("https://attacker.example.com/api/v2/channels/voice/stats/incremental/calls", client)


def test_classification_requires_talk_time_and_completed_agent_leg():
    completed_call = {"id": "c1", "completion_status": "completed", "talk_time": 0}
    assert classify_call(completed_call, [agent_leg("c1", "completed")])["answered_by_agent"] is False

    answered_call = {"id": "c2", "completion_status": "completed", "talk_time": 120}
    result = classify_call(answered_call, [agent_leg("c2", "completed")])
    assert result["answered_by_agent"] is True
    assert result["outcome"] == "answered_by_agent"
    assert result["zendesk_completion_status"] == "completed"


def test_classification_keeps_voicemail_overflow_failed_separate():
    assert classify_call({"id": "v", "completion_status": "completed_voicemail"}, [])["outcome"] == "voicemail"
    assert classify_call({"id": "o", "completion_status": "external_overflow"}, [])["outcome"] == "overflowed"
    assert classify_call({"id": "f", "completion_status": "failed"}, [])["outcome"] == "failed"


def test_classification_abandoned_locations():
    assert classify_call({"id": "i", "completion_status": "abandoned_in_ivr"}, [])["outcome"] == "abandoned_in_ivr"
    assert classify_call({"id": "q", "completion_status": "abandoned_in_queue"}, [])["outcome"] == "abandoned_in_queue"
    assert classify_call({"id": "h", "completion_status": "abandoned_on_hold"}, [])["outcome"] == "abandoned_on_hold"
    assert classify_call({"id": "vm", "completion_status": "abandoned_in_voicemail"}, [])["outcome"] == "abandoned_in_voicemail"


def test_join_includes_metrics_ivr_ticket_and_agent_leg_statuses():
    calls = [{"id": "c1", "ticket_id": 123, "completion_status": "completed", "talk_time": 30, "queue_wait_time": 5, "time_to_answer": 7, "ivr_time": 3, "hold_time": 2, "wrap_up_time": 11, "ivr_action": "route"}]
    legs = [agent_leg("c1", "completed", agent_id=42), agent_leg("c1", "missed", agent_id=43)]
    row = join_calls_and_legs(calls, legs)[0]

    assert row["ticket_id"] == 123
    assert row["metrics"] == {"queue_wait_time": 5, "time_to_answer": 7, "ivr_time": 3, "talk_time": 30, "hold_time": 2, "wrap_up_time": 11}
    assert row["ivr"] == {"ivr_action": "route"}
    assert summarize_leg(legs[0])["agent_completed"] is True
    assert summarize_leg(legs[1])["agent_missed"] is True


@pytest.mark.asyncio
async def test_429_honors_retry_after(monkeypatch):
    waits = []

    async def no_wait():
        return None

    async def fake_sleep(seconds):
        waits.append(seconds)

    monkeypatch.setattr("zendesk_skill.talk._rate_limiter.wait", no_wait)
    monkeypatch.setattr("zendesk_skill.talk.asyncio.sleep", fake_sleep)
    response = SimpleNamespace(headers={"Retry-After": "2"})

    class HTTPStatusCause(Exception):
        def __init__(self, response):
            super().__init__("HTTP 429")
            self.response = response

    err = ZendeskAPIError("rate limited", 429)
    err.__cause__ = HTTPStatusCause(response)
    client = FakeClient([err, {"legs": [], "next_page": None}])

    legs = await fetch_incremental(client, LEGS_ENDPOINT, "legs", "2026-01-01", "2026-01-02")

    assert legs == []
    assert waits == [2.0]
    assert len(client.requests) == 2


def test_permission_errors_are_not_swallowed():
    err = ZendeskAPIError("Permission denied", 403)
    assert err.status_code == 403


def agent_leg(call_id, status, agent_id=1):
    return {"call_id": call_id, "type": "agent", "completion_status": status, "agent_id": agent_id}

@pytest.mark.asyncio
async def test_fetch_incremental_stops_when_count_zero_with_next_page(monkeypatch):
    async def no_wait():
        return None

    monkeypatch.setattr("zendesk_skill.talk._rate_limiter.wait", no_wait)
    client = FakeClient([
        {"calls": [], "count": 0, "next_page": "https://example.zendesk.com/api/v2/channels/voice/stats/incremental/calls?page=2"},
        {"calls": [{"id": "should-not-fetch", "created_at": "2026-01-01T10:00:00Z"}], "count": 1, "next_page": None},
    ])

    calls = await fetch_incremental(client, CALLS_ENDPOINT, "calls", "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")

    assert calls == []
    assert len(client.requests) == 1


@pytest.mark.asyncio
async def test_fetch_incremental_rejects_repeated_next_page_cursor(monkeypatch):
    async def no_wait():
        return None

    monkeypatch.setattr("zendesk_skill.talk._rate_limiter.wait", no_wait)
    repeated = "https://example.zendesk.com/api/v2/channels/voice/stats/incremental/calls?page=2"
    client = FakeClient([
        {"calls": [{"id": 1, "created_at": "2026-01-01T10:00:00Z"}], "count": 1, "next_page": repeated},
        {"calls": [{"id": 2, "created_at": "2026-01-01T11:00:00Z"}], "count": 1, "next_page": repeated},
    ])

    with pytest.raises(ZendeskAPIError, match="repeated next_page cursor"):
        await fetch_incremental(client, CALLS_ENDPOINT, "calls", "2026-01-01T00:00:00Z", "2026-01-31T23:59:59Z")


def test_group_breakdown_uses_call_group_id():
    from zendesk_skill.talk import breakdown, join_calls_and_legs

    rows = join_calls_and_legs([
        {"id": "c1", "call_group_id": 987654, "completion_status": "completed", "created_at": "2026-01-01T10:00:00Z"}
    ], [])

    grouped = breakdown(rows, "group")

    assert grouped == [{"key": "987654", "count": 1, "outcomes": {"other": 1}}]


def test_agent_prefixed_leg_completion_statuses():
    assert summarize_leg(agent_leg("c1", "agent_missed"))["agent_missed"] is True
    assert summarize_leg(agent_leg("c1", "agent_declined"))["agent_declined"] is True
    assert summarize_leg(agent_leg("c1", "agent_transfer_declined"))["agent_declined"] is True


def test_overflowed_boolean_takes_priority_over_completed_status():
    assert classify_call({"id": "o1", "completion_status": "completed", "overflowed": True}, [])["outcome"] == "overflowed"
    assert classify_call({"id": "o2", "completion_status": "completed", "overflowed": False}, [])["outcome"] == "other"
    assert classify_call({"id": "v", "completion_status": "completed_voicemail", "overflowed": False}, [])["outcome"] == "voicemail"
    assert classify_call({"id": "a", "completion_status": "completed", "overflowed": True, "talk_time": 120}, [agent_leg("a", "completed")])["outcome"] == "answered_by_agent"
