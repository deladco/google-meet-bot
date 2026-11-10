import ast
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import share_notes


@pytest.fixture
def meeting():
    return {
        "id": "meeting", "source": "cloud", "owner_email": "PRIVATE_OWNER",
        "notes": "PRIVATE_RAW_NOTES",
        "minutes": {
            "title": "PRIVATE_TITLE", "summary": "Anna approved the plan.",
            "summary_refs": [{"quote": "PRIVATE_QUOTE"}],
            "decisions": [{"decision": "Ship the update", "transcript_refs": [{"quote": "PRIVATE_CITATION"}]}],
            "action_items": [{"task": "Send report", "owner": "PRIVATE_TASK_OWNER", "deadline": "Friday", "project": "PRIVATE_PROJECT"}],
            "discussion_topics": [{"topic": "Roadmap", "details": "PRIVATE_TOPIC_DETAILS"}],
            "unresolved_questions": [{"question": "PRIVATE_QUESTION"}],
            "review": {"approved_by": "PRIVATE_REVIEWER"},
            "cost_estimate": {"estimated_cost": "PRIVATE_COST"},
        },
        "transcript": json.dumps({"speaker": "PRIVATE_SPEAKER", "start": 61, "text": "PRIVATE_TRANSCRIPT", "timestamp": "PRIVATE_TIMESTAMP", "original_speaker": "PRIVATE_ORIGINAL"}) + "\n",
    }


def test_default_export_uses_allowlist_and_does_not_mutate_source(meeting):
    original = copy.deepcopy(meeting)
    result = share_notes.build(meeting, ["Summary", "Decisions", "Action items"])
    assert "Anna approved" in result and "Send report" in result
    assert "PRIVATE_" not in result
    assert meeting == original


def test_transcript_and_owner_fields_require_explicit_selection(meeting):
    result = share_notes.build(meeting, ["Action items", "Transcript"], owners=True, speakers=True)
    assert "PRIVATE_TASK_OWNER" in result and "PRIVATE_SPEAKER" in result
    assert "[01:01]" in result and "PRIVATE_TRANSCRIPT" in result
    assert "PRIVATE_ORIGINAL" not in result and "PRIVATE_TIMESTAMP" not in result
    hidden = share_notes.build(meeting, ["Transcript"])
    assert "PRIVATE_TRANSCRIPT" in hidden and "PRIVATE_SPEAKER" not in hidden


def test_excluded_sections_and_references_do_not_reappear(meeting):
    result = share_notes.build(meeting, ["Decisions"])
    assert "Ship the update" in result
    assert "Anna" not in result and "Send report" not in result and "PRIVATE_" not in result


def test_redaction_handles_names_email_and_phone_without_corrupting_dates():
    original = "Anna met Ann on 2026-11-09 at 12:30. anna@example.com +44 20 7946 0958. Annual planning."
    result = share_notes.redact(original, ["Anna", "Ann"])
    assert "Anna met" not in result and "Ann on" not in result
    assert "@example.com" not in result and "7946" not in result
    assert "2026-11-09" in result and "12:30" in result and "Annual planning" in result


def test_literal_phrases_do_not_execute_regex_and_toggles_are_respected():
    result = share_notes.redact("C++ discussed a.b@example.com and +44 20 7946 0958", ["C++"], emails=False, phones=False)
    assert "C++" not in result and "a.b@example.com" in result and "7946" in result


def test_no_selected_sections_rejected(meeting):
    with pytest.raises(ValueError):
        share_notes.build(meeting, [])


def sharing_app(meeting):
    from streamlit.testing.v1 import AppTest

    source = (Path(__file__).resolve().parents[1] / "meeting_app.py").read_text(encoding="utf-8")
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "shareable_notes_section")
    code = "\n".join(source.splitlines()[function.decorator_list[0].lineno - 1:function.end_lineno])
    return AppTest.from_string(
        "import streamlit as st\nimport share_notes\nimport hashlib\nimport json\n"
        "st.session_state['user_id']='owner'\n"
        f"st.session_state.setdefault('meeting', {meeting!r})\n"
        + code + "\nshareable_notes_section(st.session_state['meeting'])\n"
    ).run()


def build_draft(app):
    next(button for button in app.button if button.label == "Create review draft").click().run()
    assert not app.exception


def download_disabled(app):
    return app.get("download_button")[0].proto.disabled


def test_sharing_requires_review_and_editing_resets_confirmation(meeting):
    original = copy.deepcopy(meeting)
    app = sharing_app(meeting)
    build_draft(app)
    assert download_disabled(app)
    next(box for box in app.checkbox if box.label.startswith("I reviewed")).check().run()
    assert not download_disabled(app)
    editor = next(area for area in app.text_area if area.label.startswith("Edit sharing copy"))
    editor.set_value("Edited text with new@example.com").run()
    assert not app.exception and download_disabled(app)
    assert "new@example.com" not in app.code[0].value
    assert "[email removed]" in app.code[0].value
    assert app.session_state["meeting"] == original


def test_changed_section_selection_blocks_old_draft_download(meeting):
    app = sharing_app(meeting)
    build_draft(app)
    next(box for box in app.checkbox if box.label.startswith("I reviewed")).check().run()
    app.multiselect[0].set_value(["Summary"]).run()
    assert not app.exception and download_disabled(app)
    assert any("Rebuild the draft" in warning.value for warning in app.warning)
    next(button for button in app.button if button.label.startswith("Rebuild review draft")).click().run()
    assert "Send report" not in app.code[0].value
    assert not next(box for box in app.checkbox if box.label.startswith("I reviewed")).value


def test_names_are_redacted_from_title_and_prose(meeting):
    app = sharing_app(meeting)
    app.text_input[0].set_value("Anna meeting")
    next(area for area in app.text_area if area.label.startswith("Names or phrases")).set_value("Anna")
    build_draft(app)
    assert "Anna" not in app.code[0].value
    assert "[redacted]" in app.code[0].value
