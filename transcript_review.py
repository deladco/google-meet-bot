"""Human attribution corrections and evidence checks, without invented confidence scores."""
import copy
import json
import logging
import re

import minutes_store


def generic_speaker(value):
    text = str(value or "").strip()
    return not text or text.casefold() in {"unknown", "not specified"} or bool(re.fullmatch(r"speaker[ _-]*\d+", text, re.IGNORECASE))


def rename_speakers(entries, minutes, names):
    speakers = {str(entry.get("speaker") or "Unknown") for entry in entries}
    mapping = {}
    for original, name in names.items():
        if original not in speakers:
            raise ValueError("A speaker label is no longer present. Refresh the meeting.")
        name = " ".join(str(name or "").split())
        if not name or len(name) > 100:
            raise ValueError("Speaker names must contain between 1 and 100 characters.")
        if name != original:
            mapping[original] = name
    updated_entries, updated_minutes = copy.deepcopy(entries), copy.deepcopy(minutes or {})
    if not mapping:
        return updated_entries, updated_minutes
    for entry in updated_entries:
        original = str(entry.get("speaker") or "Unknown")
        if original in mapping:
            entry.setdefault("original_speaker", original)
            entry["speaker"] = mapping[original]
    # Apply simultaneously: swapping labels must not rename a segment twice.
    def update_refs(refs):
        for ref in refs or []:
            if isinstance(ref, dict) and ref.get("speaker") in mapping:
                ref["speaker"] = mapping[ref["speaker"]]

    update_refs(updated_minutes.get("summary_refs"))
    for section in ("discussion_topics", "decisions", "action_items", "unresolved_questions"):
        for item in updated_minutes.get(section) or []:
            update_refs(item.get("transcript_refs"))
            update_refs(item.get("refs"))
            if section == "action_items" and item.get("owner") in mapping:
                item["owner"] = mapping[item["owner"]]
    if updated_minutes:
        updated_minutes["speaker_name_changes"] = {**updated_minutes.get("speaker_name_changes", {}), **mapping}
        updated_minutes["review"] = {"status": "draft", "edited": True, "edited_at": minutes_store.now_iso()}
    return updated_entries, updated_minutes


def evidence_issues(item, entries):
    refs = item.get("transcript_refs") or item.get("refs") or []
    if not refs:
        return ["No transcript source attached"]
    issues = []
    for ref in refs:
        index = ref.get("segment_index") if isinstance(ref, dict) else None
        if type(index) is not int or not 0 <= index < len(entries):
            issues.append("Source segment is unavailable")
        elif not str(ref.get("quote", "")).strip():
            issues.append("Source quote is empty")
        elif str(ref.get("quote", "")).strip() != str(entries[index].get("text", "")).strip():
            issues.append("Source quote differs from the current transcript")
        elif ref.get("speaker", "Unknown") != entries[index].get("speaker", "Unknown"):
            issues.append("Source speaker differs from the current transcript")
    return list(dict.fromkeys(issues))


def action_issues(item, entries):
    issues = evidence_issues(item, entries)
    owner = str(item.get("owner") or "").strip()
    refs = item.get("transcript_refs") or item.get("refs") or []
    if minutes_store.is_blank(owner) or generic_speaker(owner):
        issues.append("Owner needs confirmation")
    else:
        sources = [str(ref.get("quote", "")) for ref in refs if isinstance(ref, dict)]
        speakers = [str(ref.get("speaker", "")) for ref in refs if isinstance(ref, dict)]
        if owner.casefold() not in {s.casefold() for s in speakers} and not any(
            re.search(r"(?<!\w)" + re.escape(owner) + r"(?!\w)", quote, re.IGNORECASE) for quote in sources
        ):
            issues.append("Owner is not identified in the cited sources")
    if minutes_store.is_blank(item.get("deadline")):
        issues.append("No deadline stated")
    elif not any(str(item["deadline"]).casefold() in str(ref.get("quote", "")).casefold() for ref in refs if isinstance(ref, dict)):
        issues.append("Deadline needs source confirmation")
    return issues


def review_issues(minutes, entries):
    issues = []
    narrative = [str(minutes.get("summary") or "")]
    for section, keys in (
        ("decisions", ("decision",)), ("action_items", ("task",)),
        ("discussion_topics", ("topic", "details")), ("unresolved_questions", ("question",)),
    ):
        narrative.extend(str(item.get(key) or "") for item in minutes.get(section) or [] for key in keys)
    for old, new in minutes.get("speaker_name_changes", {}).items():
        if old != new and any(re.search(r"(?<!\w)" + re.escape(old) + r"(?!\w)", text) for text in narrative):
            issues.append(f"Notes still mention renamed speaker '{old}'; check whether that text should say '{new}'")
    if any(generic_speaker(entry.get("speaker")) for entry in entries):
        issues.append("Some speaker labels have not been confirmed")
    if minutes.get("summary"):
        issues.extend("Summary: " + text for text in evidence_issues({"transcript_refs": minutes.get("summary_refs")}, entries))
    for index, item in enumerate(minutes.get("decisions") or []):
        issues.extend(f"Decision {index + 1}: {text}" for text in evidence_issues(item, entries))
    for index, item in enumerate(minutes.get("action_items") or []):
        issues.extend(f"Task {index + 1}: {text}" for text in action_issues(item, entries))
    return issues


def save_speakers(client, meeting, entries, minutes, user_id):
    """Save transcript, references, approval state and rendered notes in one update."""
    if meeting.get("source") != "cloud" or not user_id:
        raise ValueError("Speaker corrections require your cloud-saved meeting.")
    transcript = "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries)
    fields = {"transcript": transcript, "minutes": minutes or None}
    if minutes:
        from ai_agent import render_markdown

        fields["notes"] = render_markdown(minutes)
    rows = client.table(minutes_store.MEETINGS_TABLE).update(fields).eq(
        "meeting_id", meeting["id"],
    ).eq("user_id", user_id).execute().data
    if not rows:
        raise PermissionError("The meeting could not be updated. Refresh and check that it belongs to your account.")
    meeting.update(fields)
    try:
        import rag

        rag.index_meeting(client, meeting["id"], meeting["recorded_at"], transcript, fields.get("notes", meeting.get("notes", "")), user_id=user_id)
    except Exception:
        logging.getLogger(__name__).debug("Speaker corrections saved; search indexing will retry later", exc_info=True)
