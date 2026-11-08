"""Learns from the user's edits to AI meeting notes.

When a user saves edited notes, what they changed (before -> after) is stored per user. The next time notes are
generated for that user, the most recent corrections are added to the prompt so the model repeats less of what
was wrong. Stored in output/note_feedback.json; only that user's own corrections are ever used for their notes.
"""
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path

FEEDBACK_FILE = Path(os.environ.get("NOTE_FEEDBACK_FILE") or Path(__file__).resolve().parent / "output" / "note_feedback.json")
MAX_PER_USER = 60
PROMPT_EXAMPLES = 12
TEXT_LIMIT = 220
_lock = threading.Lock()


def _clip(value):
    text = " ".join(str(value or "").split())
    return text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"


def _owner(value):
    return value if value and str(value).strip().casefold() != "not specified" else ""


def diff_minutes(old, new):
    """Corrections the user made: a list of {kind, before, after} dicts (empty when nothing changed)."""
    changes = []
    old_summary, new_summary = _clip(old.get("summary")), _clip(new.get("summary"))
    if new_summary and old_summary != new_summary:
        changes.append({"kind": "summary", "before": old_summary, "after": new_summary})

    old_decisions = [str(d.get("decision", "")).strip() for d in old.get("decisions") or []]
    new_decisions = [str(d.get("decision", "")).strip() for d in new.get("decisions") or []]
    for text in old_decisions:
        if text and text not in new_decisions:
            changes.append({"kind": "decision_removed_or_reworded", "before": _clip(text), "after": ""})
    for text in new_decisions:
        if text and text not in old_decisions:
            changes.append({"kind": "decision_added_or_reworded", "before": "", "after": _clip(text)})

    new_by_id = {a.get("id"): a for a in new.get("action_items") or [] if a.get("id")}
    for item in old.get("action_items") or []:
        match = new_by_id.get(item.get("id"))
        if match is None:
            changes.append({"kind": "action_removed", "before": _clip(item.get("task")), "after": ""})
            continue
        for field in ("task", "owner", "deadline"):
            before, after = str(item.get(field) or "").strip(), str(match.get(field) or "").strip()
            if before != after:
                changes.append({
                    "kind": f"action_{field}_changed",
                    "context": _clip(match.get("task")),
                    "before": _clip(before),
                    "after": _clip(after),
                })
    old_ids = {a.get("id") for a in old.get("action_items") or []}
    for item in new.get("action_items") or []:
        if item.get("id") not in old_ids:
            changes.append({"kind": "action_added", "before": "", "after": _clip(item.get("task"))})
    return changes


def _load():
    try:
        data = json.loads(FEEDBACK_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record_edit(user_id, old_minutes, new_minutes):
    """Stores the corrections from one save. Returns how many were recorded."""
    if not user_id:
        return 0
    changes = diff_minutes(old_minutes or {}, new_minutes or {})
    if not changes:
        return 0
    stamp = datetime.now(UTC).isoformat(timespec="seconds")
    with _lock:
        data = _load()
        entries = data.get(str(user_id), []) + [{**change, "at": stamp} for change in changes]
        data[str(user_id)] = entries[-MAX_PER_USER:]
        FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp = FEEDBACK_FILE.with_suffix(".tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(FEEDBACK_FILE)
    return len(changes)


def _describe(entry):
    kind, before, after = entry.get("kind", ""), entry.get("before", ""), entry.get("after", "")
    if kind == "summary":
        return f'Summary was rewritten. Before: "{before}" After: "{after}"'
    if kind == "action_removed":
        return f'Action item was removed as wrong or not a real task: "{before}"'
    if kind == "action_added":
        return f'Action item the notes missed: "{after}"'
    if kind == "decision_removed_or_reworded":
        return f'Decision removed or reworded: "{before}"'
    if kind == "decision_added_or_reworded":
        return f'Decision the user wrote or corrected: "{after}"'
    if kind.startswith("action_") and kind.endswith("_changed"):
        field = kind[len("action_"):-len("_changed")]
        return f'Task "{entry.get("context", "")}": {field} changed from "{before}" to "{after}"'
    return ""


def guidance(user_id, limit=PROMPT_EXAMPLES):
    """Prompt text with the user's most recent corrections, or '' when there are none."""
    if not user_id:
        return ""
    lines = [_describe(entry) for entry in _load().get(str(user_id), [])[-limit:]]
    lines = [line for line in lines if line]
    if not lines:
        return ""
    return (
        " The user has corrected earlier AI notes. Learn from these corrections about their preferences "
        "(wording, level of detail, what counts as a decision or task, owners) and apply them where they "
        "fit, but never copy facts from them into these notes:\n- " + "\n- ".join(lines)
    )
