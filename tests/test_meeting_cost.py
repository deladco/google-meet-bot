import ast
import copy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth_db
import meeting_cost


def test_cost_uses_duration_attendance_and_rate():
    assert meeting_cost.estimate(90, 4, 50, "GBP")["estimated_cost"] == 300
    assert meeting_cost.estimate(1, 1, 0.30, "USD")["estimated_cost"] == 0.01
    assert meeting_cost.estimate(60, 2, 0, "EUR")["estimated_cost"] == 0


@pytest.mark.parametrize("duration,people,rate,currency", [
    (0, 2, 50, "GBP"), (-1, 2, 50, "GBP"), (float("nan"), 2, 50, "GBP"),
    (60, 0, 50, "GBP"), (60, 1.5, 50, "GBP"), (60, True, 50, "GBP"),
    (60, 2, -1, "GBP"), (60, 2, float("inf"), "GBP"), (60, 2, 50, "XXX"),
])
def test_invalid_inputs_rejected(duration, people, rate, currency):
    with pytest.raises(ValueError):
        meeting_cost.estimate(duration, people, rate, currency)


def test_dashboard_totals_do_not_mix_currencies_or_include_missing_estimates():
    meetings = [
        {"minutes": {"cost_estimate": meeting_cost.estimate(60, 2, 50, "GBP")}},
        {"minutes": {"cost_estimate": meeting_cost.estimate(30, 2, 50, "GBP")}},
        {"minutes": {"cost_estimate": meeting_cost.estimate(60, 1, 75, "USD")}},
        {"minutes": None}, {"minutes": {"cost_estimate": {"duration_minutes": "broken"}}},
    ]
    assert meeting_cost.totals(meetings) == {"GBP": {"total": 150, "meetings": 2}, "USD": {"total": 75, "meetings": 1}}


class Client:
    def __init__(self, allowed=True):
        self.allowed, self.filters = allowed, {}

    def table(self, name):
        assert name == "synced_meetings"
        return self

    def update(self, fields):
        self.fields = fields
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def execute(self):
        return SimpleNamespace(data=[self.fields] if self.allowed else [])


def test_save_keeps_notes_tasks_and_approval():
    meeting = {"id": "meeting", "source": "cloud", "minutes": {"summary": "Summary", "action_items": [{"id": "task"}], "review": {"status": "approved"}}}
    original = copy.deepcopy(meeting["minutes"])
    client = Client()
    meeting_cost.save(client, meeting, meeting_cost.estimate(60, 2, 40, "GBP"), "owner")
    assert client.filters == {"meeting_id": "meeting", "user_id": "owner"}
    assert set(client.fields) == {"minutes"}
    assert all(meeting["minutes"][key] == value for key, value in original.items())
    assert meeting["minutes"]["cost_estimate"]["estimated_cost"] == 80


def test_denied_save_does_not_update_cached_meeting():
    meeting = {"id": "meeting", "source": "cloud", "minutes": {"summary": "Summary"}}
    with pytest.raises(PermissionError):
        meeting_cost.save(Client(False), meeting, meeting_cost.estimate(60, 2, 40, "GBP"), "other")
    assert "cost_estimate" not in meeting["minutes"]


def test_saved_total_is_recomputed_from_inputs():
    cost = {**meeting_cost.estimate(60, 2, 40, "GBP"), "estimated_cost": 999999}
    assert meeting_cost.saved_estimate({"minutes": {"cost_estimate": cost}})["estimated_cost"] == 80


def test_cost_panel_calculates_and_saves_user_inputs(monkeypatch):
    from streamlit.testing.v1 import AppTest

    client = Client()
    monkeypatch.setattr(auth_db, "client_for_user", lambda: client)
    source = (Path(__file__).resolve().parents[1] / "meeting_app.py").read_text(encoding="utf-8")
    node = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "meeting_cost_section")
    function = "\n".join(source.splitlines()[node.decorator_list[0].lineno - 1:node.end_lineno])
    app = AppTest.from_string(
        "import streamlit as st\nimport meeting_cost\nimport auth_db\nimport logging\n"
        "st.session_state['user_id']='owner'\n"
        "st.session_state.setdefault('meeting', {'id':'meeting','source':'cloud','minutes':{'summary':'Summary'}})\n"
        "def meeting_duration_hours(meeting): return 1\n"
        "def refresh_meetings(): pass\n"
        + function + "\nmeeting_cost_section(st.session_state['meeting'])\n"
    ).run()
    assert not app.exception
    app.number_input[1].set_value(4)
    app.number_input[2].set_value(50.0).run()
    assert app.metric[0].value == "GBP 200.00"
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state["meeting"]["minutes"]["cost_estimate"]["estimated_cost"] == 200
