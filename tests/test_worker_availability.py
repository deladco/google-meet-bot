import ast
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth_db
import bot_worker as bw
import worker_availability as wa

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def heartbeat(activity="idle", age=0):
    return {"worker_id": "opaque-id", "activity": activity, "last_seen_at": (NOW - timedelta(seconds=age)).isoformat()}


def test_availability_separates_ready_busy_stopped_and_stale_workers():
    rows = [heartbeat(), heartbeat("recording", 20), heartbeat("offline", 0), heartbeat("idle", 91)]
    status = wa.summarize(rows, NOW)
    assert len(status["online"]) == 2
    assert len(status["available"]) == 1
    assert status["last_seen"] == NOW


@pytest.mark.parametrize("row", [
    {}, {"last_seen_at": "broken"}, {"last_seen_at": None},
    {"last_seen_at": "2026-11-08T12:00:00", "activity": "idle"},
    heartbeat(age=-3600), heartbeat(activity="unknown"),
])
def test_bad_heartbeats_do_not_show_worker_online(row):
    assert wa.summarize([row], NOW)["online"] == []


def test_worker_identity_survives_restart_and_contains_no_host_name(tmp_path):
    identity = wa.worker_id(tmp_path)
    assert str(UUID(identity)) == identity
    assert wa.worker_id(tmp_path) == identity


def test_activity_tracks_only_current_job_and_returns_idle_after_error():
    activity = wa.Activity()
    with pytest.raises(RuntimeError), activity.working("joining", ("bot_jobs", "own")):
        activity.update_job("bot_jobs", "another", "recording")
        assert activity.snapshot() == "joining"
        activity.update_job("bot_jobs", "own", "recording")
        assert activity.snapshot() == "recording"
        with activity.working("transcribing", ("bot_jobs", "own")):
            assert activity.snapshot() == "transcribing"
        assert activity.snapshot() == "recording"
        raise RuntimeError("Job failed")
    assert activity.snapshot() == "idle"


class HeartbeatDB:
    def __init__(self):
        self.payloads = []

    def table(self, table):
        assert table == wa.TABLE
        return self

    def upsert(self, payload, **kwargs):
        assert set(payload) == {"worker_id", "last_seen_at", "activity"}
        assert kwargs == {"on_conflict": "worker_id"}
        self.payloads.append(dict(payload))
        return self

    def execute(self):
        return SimpleNamespace(data=[])


def test_heartbeat_continues_during_busy_worker_and_marks_shutdown(monkeypatch, tmp_path):
    db = HeartbeatDB()
    clients = iter([object(), object(), db])
    monkeypatch.setattr(bw, "service_client", lambda: next(clients))
    monkeypatch.setattr(bw, "RECOVERY_DIR", tmp_path)
    monkeypatch.setattr(wa, "HEARTBEAT_SECONDS", 0.01)
    monkeypatch.setattr(bw, "poll_calendar", lambda db, stop: stop.wait(2))
    ready = threading.Event()
    original = db.upsert

    def upsert(payload, **kwargs):
        result = original(payload, **kwargs)
        if payload["activity"] == "recording":
            ready.set()
        return result

    db.upsert = upsert

    def busy_worker(client):
        with wa.ACTIVITY.working("recording", ("bot_jobs", "private-job")):
            assert ready.wait(2), "Heartbeat blocked behind recording"
        raise KeyboardInterrupt

    monkeypatch.setattr(bw, "worker_loop", busy_worker)
    with pytest.raises(KeyboardInterrupt):
        bw.main()
    assert db.payloads[-1]["activity"] == "offline"
    assert any(row["activity"] == "recording" for row in db.payloads)


def test_temporary_heartbeat_failure_does_not_stop_future_beats(monkeypatch):
    stop = threading.Event()
    calls = []
    monkeypatch.setattr(wa, "HEARTBEAT_SECONDS", 0.001)

    def publish(db, identity, state):
        calls.append(state)
        if len(calls) == 1:
            raise ConnectionError("Temporary outage")
        stop.set()

    monkeypatch.setattr(wa, "publish", publish)
    wa.heartbeat_loop(None, stop, "worker")
    assert len(calls) == 3
    assert calls[-1] == "offline"


@pytest.mark.parametrize("mode,kind,expected", [
    ("ready", "success", "Worker online"),
    ("busy", "info", "busy"),
    ("offline", "warning", "no worker is available"),
    ("error", "warning", "status is unavailable"),
])
def test_dashboard_worker_status(monkeypatch, mode, kind, expected):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(auth_db, "is_configured", lambda: True)
    monkeypatch.setattr(auth_db, "client_for_user", object)

    def read_status(client):
        if mode == "error":
            raise ConnectionError("Unavailable")
        rows = [] if mode == "offline" else [heartbeat("recording" if mode == "busy" else "idle")]
        return wa.summarize(rows, NOW)

    monkeypatch.setattr(wa, "read_status", read_status)
    # Exercise the actual fragment without running the app's login/navigation setup.
    source = (Path(__file__).resolve().parents[1] / "meeting_app.py").read_text(encoding="utf-8")
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "worker_availability_section")
    fragment = "\n".join(source.splitlines()[function.decorator_list[0].lineno - 1:function.end_lineno])
    app = AppTest.from_string(
        "import streamlit as st\nimport auth_db\nimport worker_availability\nimport logging\n"
        "from datetime import UTC, datetime\nst.session_state['supabase_user'] = 'viewer'\n"
        + fragment + "\nworker_availability_section()\n"
    ).run()
    assert not app.exception
    assert any(expected in message.value for message in getattr(app, kind))
