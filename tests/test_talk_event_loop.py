"""Regression coverage for Talk analytics post-processing isolation."""

import asyncio
import threading
import time

import pytest

from zendesk_skill import operations


class _Client:
    pass


def _install_fakes(monkeypatch, tmp_path, *, row_count=1, join=None):
    async def fetch_calls(*args, **kwargs):
        return {"calls": [{"id": str(index), "name": f"caller {index}"} for index in range(row_count)],
                "metadata": {"pages_fetched": 1}}

    async def fetch_legs(*args, **kwargs):
        return {"legs": [], "metadata": {"pages_fetched": 1}}

    monkeypatch.setattr(operations, "_get_client", lambda: _Client())
    monkeypatch.setattr("zendesk_skill.talk.fetch_incremental_with_metadata", fetch_calls)
    monkeypatch.setattr("zendesk_skill.talk.fetch_relevant_legs_for_calls", fetch_legs)
    monkeypatch.setattr("zendesk_skill.talk.join_calls_and_legs", join or (lambda calls, legs: calls))
    monkeypatch.setattr("zendesk_skill.talk.breakdown", lambda rows, name: [{"key": name, "count": len(rows)}])
    monkeypatch.setattr(operations, "save_response", lambda *args, **kwargs: (tmp_path / "report.json", {}))
    monkeypatch.setattr(operations, "_sanitize_talk_for_llm", lambda value: value)


@pytest.mark.asyncio
async def test_slow_talk_processing_does_not_block_event_loop(monkeypatch, tmp_path):
    started = threading.Event()

    def slow_join(calls, legs):
        started.set()
        time.sleep(0.2)
        return calls

    _install_fakes(monkeypatch, tmp_path, join=slow_join)
    task = asyncio.create_task(operations.get_talk_analytics("2026-01-01", "2026-01-02"))
    await asyncio.wait_for(asyncio.to_thread(started.wait), 1)

    tick_started = time.perf_counter()
    await asyncio.sleep(0)
    assert time.perf_counter() - tick_started < 0.05
    await task


@pytest.mark.asyncio
async def test_talk_admission_bounds_concurrent_processing(monkeypatch, tmp_path):
    active = 0
    maximum = 0
    guard = threading.Lock()

    def measured_join(calls, legs):
        nonlocal active, maximum
        with guard:
            active += 1
            maximum = max(maximum, active)
        time.sleep(0.05)
        with guard:
            active -= 1
        return calls

    _install_fakes(monkeypatch, tmp_path, join=measured_join)
    await asyncio.gather(*(
        operations.get_talk_analytics("2026-01-01", "2026-01-02") for _ in range(3)
    ))
    assert maximum == 1


@pytest.mark.asyncio
async def test_talk_preview_is_capped_and_raw_arrays_are_not_returned(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, tmp_path, row_count=30)
    result = await operations.get_talk_analytics("2026-01-01", "2026-01-02")

    assert len(result["joined_calls_preview"]) == 25
    assert result["preview_truncated"] is True
    assert result["preview_remaining_count"] == 5
    assert "calls" not in result and "legs" not in result and "joined_calls" not in result
    assert "file_path" not in result


def test_talk_admission_survives_successive_asyncio_run_calls(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, tmp_path)

    first = asyncio.run(operations.get_talk_analytics("2026-01-01", "2026-01-02"))
    second = asyncio.run(operations.get_talk_analytics("2026-01-01", "2026-01-02"))

    assert first["joined_count"] == second["joined_count"] == 1


@pytest.mark.asyncio
async def test_exception_releases_talk_admission(monkeypatch, tmp_path):
    _install_fakes(monkeypatch, tmp_path, join=lambda calls, legs: (_ for _ in ()).throw(RuntimeError("boom")))
    with pytest.raises(RuntimeError, match="boom"):
        await operations.get_talk_analytics("2026-01-01", "2026-01-02")

    _install_fakes(monkeypatch, tmp_path)
    result = await asyncio.wait_for(
        operations.get_talk_analytics("2026-01-01", "2026-01-02"), 1
    )
    assert result["joined_count"] == 1


@pytest.mark.asyncio
async def test_cancellation_eventually_releases_talk_admission(monkeypatch, tmp_path):
    finish = threading.Event()

    def cancellable_join(calls, legs):
        finish.wait(1)
        return calls

    _install_fakes(monkeypatch, tmp_path, join=cancellable_join)
    task = asyncio.create_task(operations.get_talk_analytics("2026-01-01", "2026-01-02"))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    finish.set()
    await asyncio.sleep(0.02)

    _install_fakes(monkeypatch, tmp_path)
    result = await asyncio.wait_for(
        operations.get_talk_analytics("2026-01-01", "2026-01-02"), 1
    )
    assert result["joined_count"] == 1


@pytest.mark.asyncio
async def test_exposed_talk_payload_is_screened_once(monkeypatch, tmp_path):
    calls = 0

    def screen(value):
        nonlocal calls
        calls += 1
        return value

    _install_fakes(monkeypatch, tmp_path)
    monkeypatch.setattr(operations, "_sanitize_talk_for_llm", screen)
    await operations.get_talk_analytics("2026-01-01", "2026-01-02")
    assert calls == 1
