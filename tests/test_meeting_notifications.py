import ast
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth_db
import bot_worker as bw
import meeting_notifications as notices

NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def job(status="done", owner="owner"):
    return {"id": "job", "user_id": owner, "status": status, "meeting_id": "meeting", "updated_at": NOW.isoformat()}


class Client:
    def __init__(self):
        self.tables = {"bot_jobs": [], "audio_jobs": [], notices.TABLE: []}

    def table(self, name):
        client = self

        class Query:
            def __init__(self):
                self.filters = []
                self.fields = None
                self.items = None
                self.maximum = None
                self.head = False

            def select(self, columns, **kwargs):
                self.head = kwargs.get("head", False)
                return self

            def eq(self, key, value):
                self.filters.append(lambda row: row.get(key) == value)
                return self

            def is_(self, key, value):
                assert value == "null"
                self.filters.append(lambda row: row.get(key) is None)
                return self

            def in_(self, key, values):
                self.filters.append(lambda row: row.get(key) in values)
                return self

            def order(self, key, **kwargs):
                return self

            def limit(self, maximum):
                self.maximum = maximum
                return self

            def update(self, fields):
                self.fields = fields
                return self

            def upsert(self, items, **kwargs):
                assert kwargs == {"on_conflict": "user_id,event_key", "ignore_duplicates": True}
                self.items = items
                return self

            def execute(self):
                if self.items is not None:
                    for item in self.items:
                        if not any(row["user_id"] == item["user_id"] and row["event_key"] == item["event_key"] for row in client.tables[name]):
                            client.tables[name].append({"id": str(len(client.tables[name])), "read_at": None, **item})
                rows = [row for row in client.tables[name] if all(test(row) for test in self.filters)]
                if self.fields is not None:
                    for row in rows:
                        row.update(self.fields)
                count = len(rows)
                rows = rows[:self.maximum] if self.maximum is not None else rows
                return SimpleNamespace(data=[] if self.head else [dict(row) for row in rows], count=count)

        return Query()


@pytest.mark.parametrize("status,kind,text", [
    ("done", "completed", "ready"),
    ("failed_processing", "failed", "retained recording"),
    ("failed_saving", "failed", "without transcribing"),
    ("failed_no_audio", "failed", "No usable audio"),
    ("failed_exhausted", "failed", "retry limit"),
])
def test_job_notifications_describe_saved_work(status, kind, text):
    item = notices.job_notification("bot_jobs", job(status))
    assert item["kind"] == kind and text in item["body"]
    assert item["job_id"] == "job" and item["meeting_id"] == "meeting"


@pytest.mark.parametrize("status", ["queued", "recording", "transcribing", "retry_requested", "cancelled"])
def test_active_or_cancelled_jobs_do_not_create_outcome_alerts(status):
    assert notices.job_notification("bot_jobs", job(status)) is None


def test_polling_and_worker_notifications_do_not_duplicate_or_reset_read_state():
    client = Client()
    client.tables["bot_jobs"] = [job()]
    notices.safe_publish_job(client, "bot_jobs", job())
    notices.mark_read(client, "owner", "0")
    notices.sync_recent_jobs(client, "owner")
    assert len(client.tables[notices.TABLE]) == 1
    assert notices.inbox(client, "owner")[1] == 0
    assert client.tables[notices.TABLE][0]["read_at"] is not None


def test_another_failure_after_retry_creates_a_new_alert():
    original = job("failed_saving")
    retried = {**original, "updated_at": (NOW + timedelta(minutes=1)).isoformat()}
    assert notices.job_notification("bot_jobs", original)["event_key"] != notices.job_notification("bot_jobs", retried)["event_key"]


def test_inbox_and_mark_all_are_scoped_to_current_account():
    client = Client()
    notices.store(client, [notices.job_notification("bot_jobs", job(owner=owner)) for owner in ("owner", "other")])
    rows, unread = notices.inbox(client, "owner")
    assert unread == 1 and all(row["user_id"] == "owner" for row in rows)
    notices.mark_read(client, "owner")
    assert notices.inbox(client, "owner")[1] == 0
    assert notices.inbox(client, "other")[1] == 1


def test_notification_outage_does_not_fail_meeting_processing():
    class Broken:
        def table(self, name):
            raise ConnectionError("Database unavailable")

    notices.safe_publish_job(Broken(), "bot_jobs", job())


def event(key="event", minutes=10, **extra):
    return {"key": key, "start": NOW + timedelta(minutes=minutes), "link": "https://meet.google.com/abc", "title": "Planning", **extra}


def test_worker_alerts_cover_offline_and_busy_but_not_ready_workers():
    offline = notices.worker_alerts("owner", [event()], {"online": [], "available": []}, now=NOW)
    busy = notices.worker_alerts("owner", [event()], {"online": ["worker"], "available": []}, now=NOW)
    assert "No worker was online" in offline[0]["body"]
    assert "All workers were busy" in busy[0]["body"]
    assert notices.worker_alerts("owner", [event()], {"online": ["worker"], "available": ["worker"]}, now=NOW) == []


def test_worker_alerts_skip_past_distant_all_day_and_already_handled_events():
    events = [event(minutes=-1), event(minutes=16), event(all_day=True), event(link=None), event(key="joined")]
    assert notices.worker_alerts("owner", events, {"online": [], "available": []}, {"joined"}, NOW) == []


def test_worker_alert_is_deduplicated_across_repeated_checks():
    client = Client()
    for now in (NOW, NOW + timedelta(seconds=30)):
        notices.store(client, notices.worker_alerts("owner", [event()], {"online": [], "available": []}, now=now))
    assert len(client.tables[notices.TABLE]) == 1


def test_calendar_alerts_respect_enabled_auto_join_and_existing_bot():
    client = Client()
    agenda = {"feed": {"enabled": False}, "events": [event()]}
    notices.sync_worker_alerts(client, "owner", agenda, {"online": [], "available": []}, NOW)
    assert not client.tables[notices.TABLE]
    agenda["feed"]["enabled"] = True
    client.tables["bot_jobs"] = [{"user_id": "owner", "event_key": "event", "status": "recording"}]
    notices.sync_worker_alerts(client, "owner", agenda, {"online": [], "available": []}, NOW)
    assert not client.tables[notices.TABLE]
    client.tables["bot_jobs"][0]["status"] = "queued"
    notices.sync_worker_alerts(client, "owner", agenda, {"online": [], "available": []}, NOW)
    assert len(client.tables[notices.TABLE]) == 1


def test_worker_publishes_outcome_after_updating_job(monkeypatch):
    client = Client()
    client.tables["bot_jobs"] = [job("processing")]
    published = []
    monkeypatch.setattr(notices, "safe_publish_job", lambda db, table, row: published.append((table, row["status"])))
    bw.set_job(client, "job", status="transcribing")
    assert not published
    bw.set_job(client, "job", status="done", meeting_id="meeting")
    assert published == [("bot_jobs", "done")]


def notification_app(monkeypatch, client):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    monkeypatch.setattr(auth_db, "is_configured", lambda: True)
    monkeypatch.setattr(auth_db, "client_for_user", lambda: client)
    monkeypatch.setattr(notices, "sync_recent_jobs", lambda *args: None)
    monkeypatch.setattr(st, "switch_page", lambda value: st.session_state.__setitem__("destination", value))
    source = (Path(__file__).resolve().parents[1] / "meeting_app.py").read_text(encoding="utf-8")
    functions = []
    for node in ast.parse(source).body:
        if isinstance(node, ast.FunctionDef) and node.name in {"notification_action", "notifications_panel"}:
            start = node.decorator_list[0].lineno if node.decorator_list else node.lineno
            functions.append("\n".join(source.splitlines()[start - 1:node.end_lineno]))
    script = (
        "import streamlit as st\nimport auth_db\nimport meeting_notifications\nimport worker_availability\nimport time\nimport logging\n"
        "st.session_state['user_id'] = 'owner'\nst.session_state['supabase_user'] = 'viewer'\n"
        "PAGE_DASHBOARD = 'dashboard'\n"
        "def calendar_agenda(**kwargs): return {'feed': None, 'events': [], 'error': None}\n"
        "def when_full(value): return value\n"
        "def refresh_meetings(): pass\n"
        "def open_meeting(value): st.session_state['opened_meeting'] = value\n"
        + "\n".join(functions) + "\nnotifications_panel()\n"
    )
    return AppTest.from_string(script).run()


@pytest.mark.parametrize("status,label", [("done", "Open transcript and minutes"), ("failed_saving", "Review saved work and retry")])
def test_inbox_actions_open_the_correct_meeting_or_retry_job(monkeypatch, status, label):
    client = Client()
    notices.store(client, [notices.job_notification("bot_jobs", job(status))])
    app = notification_app(monkeypatch, client)
    assert not app.exception
    assert "1 unread" in app.get("popover")[0].proto.popover.label
    next(button for button in app.button if button.label == label).click().run()
    assert not app.exception
    if status == "done":
        assert app.session_state["opened_meeting"] == "meeting"
    else:
        assert app.session_state["active_bot_id"] == "job"
        assert app.session_state["destination"] == "dashboard"
    assert notices.inbox(client, "owner")[1] == 0


def test_mark_all_read_updates_badge_and_persists(monkeypatch):
    client = Client()
    notices.store(client, [notices.job_notification("bot_jobs", job())])
    app = notification_app(monkeypatch, client)
    next(button for button in app.button if button.label == "Mark all read").click().run()
    assert not app.exception
    assert "0 unread" in app.get("popover")[0].proto.popover.label


def test_worker_unavailable_notification_opens_dashboard_status(monkeypatch):
    client = Client()
    notices.store(client, notices.worker_alerts("owner", [event()], {"online": [], "available": []}, now=NOW))
    app = notification_app(monkeypatch, client)
    next(button for button in app.button if button.label == "View worker status").click().run()
    assert not app.exception and app.session_state["destination"] == "dashboard"
    assert notices.inbox(client, "owner")[1] == 0


def test_missing_notification_table_does_not_break_dashboard(monkeypatch):
    class Broken:
        def table(self, name):
            raise ConnectionError("Table unavailable")

    app = notification_app(monkeypatch, Broken())
    assert not app.exception
    assert any("Notifications are unavailable" in caption.value for caption in app.caption)
