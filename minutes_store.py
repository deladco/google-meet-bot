"""Editing, approval and task state for AI meeting minutes.

Everything is stored inside the minutes JSON of `synced_meetings`, so no schema
change is needed:
  minutes["review"]      -> {"status": "draft"|"approved", "edited": bool, ...}
  action_items[i]        -> adds "id", "status" ("open"|"done"), "due" (YYYY-MM-DD), "completed_at"
"""
import copy
import hashlib
import uuid
from datetime import date, datetime, timedelta, timezone

MEETINGS_TABLE = "synced_meetings"
EMPTY_VALUES = {"", "not specified", "none", "n/a", "tbd"}


def now_iso():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def is_blank(value):
    return str(value or "").strip().casefold() in EMPTY_VALUES


def parse_due(value):
    """ISO date string (or the start of one) -> date, else None."""
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def ensure_ids(minutes, meeting_id):
    """Gives every action item a stable id and a status; ids are deterministic until first saved."""
    if not isinstance(minutes, dict):
        return minutes
    for index, item in enumerate(minutes.get("action_items") or []):
        if not isinstance(item, dict):
            continue
        if not item.get("id"):
            seed = f"{meeting_id}|{index}|{item.get('task', '')}"
            item["id"] = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
        item.setdefault("status", "open")
        if not item.get("due"):
            guessed = parse_due(item.get("deadline"))
            if guessed:
                item["due"] = guessed.isoformat()
    return minutes


def review(minutes):
    value = (minutes or {}).get("review")
    return value if isinstance(value, dict) else {}


def is_approved(minutes):
    return review(minutes).get("status") == "approved"


def approve(minutes, by=None):
    minutes["review"] = {**review(minutes), "status": "approved", "approved_at": now_iso(), "approved_by": by}
    return minutes


def apply_edits(minutes, summary, decisions_text, actions):
    """Returns a new minutes dict with the user's edits; edited notes go back to draft.

    actions: list of dicts with task, owner, deadline and optionally id/remove.
    Source references are kept for items whose text (decisions) or id (tasks) is unchanged.
    """
    updated = copy.deepcopy(minutes)
    if (summary or "").strip() != str(minutes.get("summary") or "").strip():
        updated.pop("summary_refs", None)  # the sources no longer back the edited text
    updated["summary"] = (summary or "").strip()

    old_decisions = {str(d.get("decision", "")).strip(): d for d in minutes.get("decisions") or []}
    decisions = []
    for line in (decisions_text or "").splitlines():
        text = line.strip().lstrip("-•").strip()
        if not text:
            continue
        item = copy.deepcopy(old_decisions.get(text)) or {"decision": text}
        decisions.append(item)
    updated["decisions"] = decisions

    old_actions = {a.get("id"): a for a in minutes.get("action_items") or [] if a.get("id")}
    items = []
    for row in actions:
        task = (row.get("task") or "").strip()
        if not task or row.get("remove"):
            continue
        base = copy.deepcopy(old_actions.get(row.get("id"))) or {"id": uuid.uuid4().hex[:10], "status": "open"}
        base["task"] = task
        base["owner"] = (row.get("owner") or "").strip() or "Not specified"
        base["deadline"] = (row.get("deadline") or "").strip() or "Not specified"
        if not parse_due(base.get("due")) or base["deadline"] != (old_actions.get(row.get("id")) or {}).get("deadline"):
            guessed = parse_due(base["deadline"])
            if guessed:
                base["due"] = guessed.isoformat()
        items.append(base)
    updated["action_items"] = items
    updated["review"] = {"status": "draft", "edited": True, "edited_at": now_iso()}
    return updated


def set_task(minutes, task_id, **fields):
    """Updates one action item in place; returns True if it was found."""
    for item in (minutes or {}).get("action_items") or []:
        if item.get("id") != task_id:
            continue
        if "status" in fields:
            item["status"] = "done" if fields["status"] == "done" else "open"
            if item["status"] == "done":
                item["completed_at"] = now_iso()
            else:
                item.pop("completed_at", None)
        if "due" in fields:
            due = parse_due(fields["due"])
            if due:
                item["due"] = due.isoformat()
            else:
                item.pop("due", None)
        if "owner" in fields:
            item["owner"] = (fields["owner"] or "").strip() or "Not specified"
        return True
    return False


def task_rows(meetings):
    rows = []
    for meeting in meetings:
        minutes = meeting.get("minutes")
        if not isinstance(minutes, dict):
            continue
        ensure_ids(minutes, meeting["id"])
        for item in minutes.get("action_items") or []:
            rows.append(
                {
                    "id": item["id"],
                    "task": item.get("task", ""),
                    "owner": item.get("owner") or "Not specified",
                    "deadline": item.get("deadline") or "Not specified",
                    "due": parse_due(item.get("due")),
                    "done": item.get("status") == "done",
                    "refs": item.get("transcript_refs") or [],
                    "meeting": meeting,
                }
            )
    return rows


def due_bucket(row, today=None):
    """'done', 'overdue', 'today', 'week', 'later' or 'none'."""
    today = today or date.today()
    if row["done"]:
        return "done"
    due = row["due"]
    if not due:
        return "none"
    if due < today:
        return "overdue"
    if due == today:
        return "today"
    if due <= today + timedelta(days=7):
        return "week"
    return "later"


def ref_time(ref):
    start = ref.get("start")
    if isinstance(start, (int, float)):
        seconds = int(start)
        hours, rest = divmod(seconds, 3600)
        return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60:02d}:{rest % 60:02d}"
    return str(ref.get("timestamp") or "")


def source_text(item, limit=2, quote_chars=120):
    """One caption line per source reference: '03:12 Anna — "quote"'."""
    lines = []
    for ref in (item.get("transcript_refs") or item.get("refs") or [])[:limit]:
        quote = str(ref.get("quote", "")).strip()
        if len(quote) > quote_chars:
            quote = quote[: quote_chars - 1].rstrip() + "…"
        speaker = str(ref.get("speaker", "")).strip()
        lines.append(f"{ref_time(ref)} {speaker} — “{quote}”".replace("$", "\\$"))
    return lines


def save_minutes(client, meeting, minutes, user_id=None):
    """Persists edited minutes (and the regenerated notes) for one cloud meeting."""
    ensure_ids(minutes, meeting["id"])
    fields = {"minutes": minutes}
    try:
        import ai_agent

        fields["notes"] = ai_agent.render_markdown(minutes)
    except Exception:
        pass  # keep the old notes if they can't be regenerated
    client.table(MEETINGS_TABLE).update(fields).eq("meeting_id", meeting["id"]).execute()
    meeting["minutes"] = minutes
    if "notes" in fields:
        meeting["notes"] = fields["notes"]
        try:
            import rag

            rag.index_meeting(
                client, meeting["id"], meeting["recorded_at"], meeting["transcript"], fields["notes"], user_id=user_id
            )
        except Exception:
            pass  # search index is refreshed later by index_missing
    return meeting
