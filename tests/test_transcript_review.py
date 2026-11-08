import ast
import copy
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import ai_agent
import auth_db
import minutes_store
import rag
import transcript_review as review


@pytest.fixture
def meeting():
    entries = [
        {"speaker": "SPEAKER_00", "text": "I will send the report on Friday.", "start": 10, "end": 15},
        {"speaker": "SPEAKER_01", "text": "Thank you.", "start": 16, "end": 18},
    ]
    ref = {"segment_index": 0, "speaker": "SPEAKER_00", "quote": entries[0]["text"], "start": 10, "end": 15}
    minutes = {
        "summary": "SPEAKER_00 will send the report.", "summary_refs": [copy.deepcopy(ref)],
        "decisions": [{"decision": "Send report", "transcript_refs": [copy.deepcopy(ref)]}],
        "action_items": [{"id": "task1", "task": "Send report", "owner": "SPEAKER_00", "deadline": "Friday", "transcript_refs": [copy.deepcopy(ref)]}],
        "discussion_topics": [], "unresolved_questions": [], "review": {"status": "approved"},
    }
    return {"id": "meeting", "source": "cloud", "recorded_at": "2026-11-08T12:00:00+00:00", "transcript": "".join(json.dumps(e) + "\n" for e in entries), "minutes": minutes, "notes": "old"}


def entries(meeting):
    return [json.loads(line) for line in meeting["transcript"].splitlines()]


def test_speaker_rename_updates_attribution_and_exact_owner_without_rewriting_claims(meeting):
    original = copy.deepcopy(meeting)
    transcript, minutes = review.rename_speakers(entries(meeting), meeting["minutes"], {"SPEAKER_00": "Anna"})
    assert transcript[0]["speaker"] == "Anna" and transcript[0]["original_speaker"] == "SPEAKER_00"
    assert minutes["summary_refs"][0]["speaker"] == "Anna"
    assert minutes["decisions"][0]["transcript_refs"][0]["speaker"] == "Anna"
    assert minutes["action_items"][0]["owner"] == "Anna"
    assert minutes["action_items"][0]["id"] == "task1"
    assert minutes["summary"] == original["minutes"]["summary"]
    assert not minutes_store.is_approved(minutes)
    assert meeting == original


def test_swapping_names_applies_once_and_preserves_original_labels(meeting):
    transcript, minutes = review.rename_speakers(entries(meeting), meeting["minutes"], {"SPEAKER_00": "SPEAKER_01", "SPEAKER_01": "SPEAKER_00"})
    assert [entry["speaker"] for entry in transcript] == ["SPEAKER_01", "SPEAKER_00"]
    updated, _ = review.rename_speakers(transcript, minutes, {"SPEAKER_01": "Anna"})
    assert updated[0]["original_speaker"] == "SPEAKER_00"


@pytest.mark.parametrize("names", [{"missing": "Anna"}, {"SPEAKER_00": " "}, {"SPEAKER_00": "a" * 101}])
def test_invalid_name_changes_are_rejected(meeting, names):
    with pytest.raises(ValueError):
        review.rename_speakers(entries(meeting), meeting["minutes"], names)


def test_no_change_keeps_approval(meeting):
    _, minutes = review.rename_speakers(entries(meeting), meeting["minutes"], {"SPEAKER_00": "SPEAKER_00"})
    assert minutes_store.is_approved(minutes)


def test_review_flags_are_based_on_evidence_not_confidence_guesses(meeting):
    issues = review.review_issues(meeting["minutes"], entries(meeting))
    assert any("speaker labels" in issue for issue in issues)
    assert any("Owner needs confirmation" in issue for issue in issues)
    renamed, minutes = review.rename_speakers(entries(meeting), meeting["minutes"], {"SPEAKER_00": "Anna", "SPEAKER_01": "Bob"})
    assert any("still mention renamed speaker" in issue for issue in review.review_issues(minutes, renamed))
    minutes["summary"] = "The report will be sent."
    assert review.review_issues(minutes, renamed) == []


@pytest.mark.parametrize("change,expected", [
    ({"segment_index": True}, "Source segment is unavailable"),
    ({"segment_index": 99}, "Source segment is unavailable"),
    ({"quote": "Invented commitment"}, "Source quote differs"),
    ({"speaker": "Someone else"}, "Source speaker differs"),
])
def test_stale_or_invalid_sources_are_flagged(meeting, change, expected):
    item = meeting["minutes"]["action_items"][0]
    item["transcript_refs"][0].update(change)
    assert any(expected in issue for issue in review.action_issues(item, entries(meeting)))


def test_unstated_owners_and_deadlines_are_flagged(meeting):
    item = meeting["minutes"]["action_items"][0]
    item.update(owner="Ann", deadline="2027-01-01")
    issues = review.action_issues(item, entries(meeting))
    assert "Owner is not identified in the cited sources" in issues
    assert "Deadline needs source confirmation" in issues
    item["deadline"] = "Not specified"
    assert "No deadline stated" in review.action_issues(item, entries(meeting))


def test_task_text_edits_clear_sources_and_removed_deadline_clears_due(meeting):
    minutes = meeting["minutes"]
    minutes["action_items"][0].update(deadline="2026-12-01", due="2026-12-01")
    edited = minutes_store.apply_edits(minutes, minutes["summary"], "Send report", [
        {"id": "task1", "task": "Do something else", "owner": "Anna", "deadline": "Not specified"},
    ])
    item = edited["action_items"][0]
    assert "transcript_refs" not in item and "due" not in item
    assert not minutes_store.is_approved(edited)


def test_task_owner_edits_reopen_approval_but_completion_does_not(meeting):
    minutes_store.set_task(meeting["minutes"], "task1", status="done")
    assert minutes_store.is_approved(meeting["minutes"])
    minutes_store.set_task(meeting["minutes"], "task1", owner="Anna")
    assert not minutes_store.is_approved(meeting["minutes"])
    assert minutes_store.task_rows([meeting])[0]["approved"] is False


def test_boolean_source_indices_are_not_accepted(meeting):
    assert ai_agent._references([True, False], entries(meeting)) == []


class Client:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.filters = {}

    def table(self, table):
        assert table == "synced_meetings"
        return self

    def update(self, fields):
        self.fields = fields
        return self

    def eq(self, key, value):
        self.filters[key] = value
        return self

    def execute(self):
        return SimpleNamespace(data=[self.fields] if self.allowed else [])


def test_speaker_save_updates_transcript_minutes_and_notes_together(meeting, monkeypatch):
    monkeypatch.setattr(rag, "index_meeting", lambda *args, **kwargs: None)
    transcript, minutes = review.rename_speakers(entries(meeting), meeting["minutes"], {"SPEAKER_00": "Anna"})
    client = Client()
    review.save_speakers(client, meeting, transcript, minutes, "owner")
    assert set(client.fields) == {"transcript", "minutes", "notes"}
    assert client.filters == {"meeting_id": "meeting", "user_id": "owner"}
    assert "Anna" in meeting["notes"] and '"speaker": "Anna"' in meeting["transcript"]


def test_unauthorized_save_does_not_mutate_cached_meeting(meeting):
    original = copy.deepcopy(meeting)
    with pytest.raises(PermissionError):
        review.save_speakers(Client(False), meeting, entries(meeting), meeting["minutes"], "other")
    assert meeting == original


def extracted(names):
    source = (Path(__file__).resolve().parents[1] / "meeting_app.py").read_text(encoding="utf-8")
    return "\n\n".join(ast.get_source_segment(source, node) for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name in names)


def test_speaker_correction_form_saves_new_name(meeting, monkeypatch):
    from streamlit.testing.v1 import AppTest

    client = Client()
    monkeypatch.setattr(auth_db, "client_for_user", lambda: client)
    monkeypatch.setattr(rag, "index_meeting", lambda *args, **kwargs: None)
    app = AppTest.from_string(
        "import streamlit as st\nimport auth_db\nimport transcript_review\nimport json\nimport logging\n"
        "st.session_state['user_id'] = 'owner'\n"
        f"st.session_state.setdefault('meeting', {meeting!r})\n"
        "def refresh_meetings(): pass\n" + extracted({"speaker_review_form"}) +
        "\nmeeting = st.session_state['meeting']\n"
        "speaker_review_form(meeting, [json.loads(line) for line in meeting['transcript'].splitlines()])\n"
    ).run()
    app.text_input[0].set_value("Anna")
    app.button[0].click().run()
    assert not app.exception
    assert app.session_state["meeting"]["minutes"]["action_items"][0]["owner"] == "Anna"
    assert not minutes_store.is_approved(app.session_state["meeting"]["minutes"])


def test_approval_requires_acknowledging_review_flags(meeting, monkeypatch):
    import streamlit as st
    from streamlit.testing.v1 import AppTest

    meeting["minutes"]["review"] = {"status": "draft"}
    monkeypatch.setattr(st, "page_link", lambda *args, **kwargs: None)
    app = AppTest.from_string(
        "import streamlit as st\nimport transcript_review\nimport minutes_store\nimport json\nimport hashlib\n"
        f"st.session_state.setdefault('meeting', {meeting!r})\n"
        "viewer_email='reviewer'\nPAGE_ACTIONS='tasks'\n"
        "def parse_transcript(text): return [json.loads(line) for line in text.splitlines()]\n"
        "def minutes_list(meeting, key): return meeting['minutes'].get(key, [])\n"
        "def when_full(value): return str(value)\n"
        "def project_picker(*args): pass\n"
        "def persist_minutes(meeting, minutes, message):\n    meeting['minutes']=minutes\n    return True\n"
        + extracted({"sources_caption", "show_minutes"}) + "\nshow_minutes(st.session_state['meeting'])\n"
    ).run()
    assert not app.exception
    assert next(button for button in app.button if button.label == "Approve notes").disabled
    next(box for box in app.checkbox if box.label.startswith("I checked")).check().run()
    approve = next(button for button in app.button if button.label == "Approve notes")
    assert not approve.disabled
    approve.click().run()
    assert not app.exception and minutes_store.is_approved(app.session_state["meeting"]["minutes"])
