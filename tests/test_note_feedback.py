import minutes_store
import note_feedback


def test_meeting_project_flows_to_tasks_and_survives_rename_and_delete():
    minutes = {"summary": "s", "decisions": [], "action_items": [{"task": "A", "owner": "x", "deadline": "y"}]}
    minutes_store.set_meeting_project(minutes, "  Apollo  ")
    meeting = {"id": "m1", "minutes": minutes}
    assert minutes_store.meeting_project(meeting) == "Apollo"

    rows = minutes_store.task_rows([meeting])
    assert rows[0]["project"] == "Apollo"
    assert minutes_store.all_projects([], rows, [meeting]) == ["Apollo"]
    assert minutes_store.project_stats(rows, ["Apollo"], [meeting])["Apollo"] == {"open": 1, "done": 0, "meetings": 1}

    assert minutes_store.rename_project([meeting], "apollo", "Gemini") == [meeting]
    assert minutes_store.meeting_project(meeting) == "Gemini"
    assert minutes_store.remove_project([meeting], "Gemini") == [meeting]
    assert minutes_store.meeting_project(meeting) == ""
    assert minutes_store.task_rows([meeting])[0]["project"] == ""

    edited = minutes_store.apply_edits(minutes_store.set_meeting_project(minutes, "Apollo"), "s2", "", [])
    assert edited["project"] == "Apollo"

MINUTES = {
    "summary": "We talked about stuff.",
    "decisions": [{"decision": "Use Postgres"}],
    "action_items": [
        {"id": "a1", "task": "Send report", "owner": "Not specified", "deadline": "Not specified"},
        {"id": "a2", "task": "Say hello", "owner": "Bob", "deadline": "Not specified"},
    ],
}


def test_edits_are_recorded_and_fed_back(tmp_path, monkeypatch):
    monkeypatch.setattr(note_feedback, "FEEDBACK_FILE", tmp_path / "feedback.json")
    rows = [
        {"id": "a1", "task": "Send report", "owner": "Anna", "deadline": "Friday"},
        {"id": "a2", "task": "Say hello", "owner": "Bob", "deadline": "", "remove": True},
    ]
    updated = minutes_store.apply_edits(MINUTES, "Short summary.", "Use Postgres", rows)

    assert note_feedback.record_edit("u1", MINUTES, updated) >= 3
    text = note_feedback.guidance("u1")
    assert "Short summary." in text
    assert 'owner changed from "Not specified" to "Anna"' in text
    assert "Say hello" in text
    assert note_feedback.guidance("someone-else") == ""


def test_unchanged_save_records_nothing(tmp_path, monkeypatch):
    monkeypatch.setattr(note_feedback, "FEEDBACK_FILE", tmp_path / "feedback.json")
    same = minutes_store.apply_edits(MINUTES, MINUTES["summary"], "Use Postgres", [dict(a) for a in MINUTES["action_items"]])
    assert note_feedback.record_edit("u1", MINUTES, same) == 0
    assert note_feedback.guidance("u1") == ""
