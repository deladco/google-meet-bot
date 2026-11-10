"""Build a separate, editable sharing copy from an explicit content allowlist."""
import re

SECTIONS = {
    "Summary": "summary", "Decisions": "decisions", "Action items": "action_items",
    "Discussion topics": "discussion_topics", "Open questions": "unresolved_questions",
    "Transcript": "transcript",
}
EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE = re.compile(r"(?<![\w:])\+?\d[\d ().-]{7,}\d(?![\w:])")


def redact(text, phrases=(), *, emails=True, phones=True):
    terms = sorted({str(term).strip() for term in phrases if str(term).strip()}, key=len, reverse=True)
    if emails:
        text = EMAIL.sub("[email removed]", text)
    if phones:
        def replace_phone(match):
            value = match.group()
            digits = sum(char.isdigit() for char in value)
            # Do not mistake ISO dates or short transcript times for phone numbers.
            if 10 <= digits <= 15 and not re.search(r"\b\d{4}-\d{2}-\d{2}\b", value):
                return "[phone removed]"
            return value
        text = PHONE.sub(replace_phone, text)
    # Remove whole identifiers before replacing names inside them, otherwise a
    # name such as Anna could turn anna@example.com into an unmatched fragment.
    if terms:
        pattern = "|".join(r"(?<!\w)" + re.escape(term) + r"(?!\w)" for term in terms)
        text = re.sub(pattern, "[redacted]", text, flags=re.IGNORECASE)
    return text


def build(meeting, sections, *, title="Meeting notes", owners=False, speakers=False):
    if not sections or any(section not in SECTIONS for section in sections):
        raise ValueError("Choose at least one section to share.")
    minutes = meeting.get("minutes") or {}
    lines = ["# " + (title.strip() or "Meeting notes")]
    for label in sections:
        key = SECTIONS[label]
        lines.extend(["", "## " + label, ""])
        if key == "summary":
            lines.append(str(minutes.get("summary") or "No summary available."))
        elif key == "transcript":
            import json

            for line in (meeting.get("transcript") or "").splitlines():
                if not line.strip():
                    continue
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    raise TypeError("The transcript contains an invalid segment.")
                start = entry.get("start")
                stamp = ""
                if type(start) in (int, float) and 0 <= start < 1e9:
                    stamp = f"[{int(start) // 60:02d}:{int(start) % 60:02d}] "
                speaker = f"{entry.get('speaker') or 'Unknown'}: " if speakers else ""
                lines.append(f"{stamp}{speaker}{entry.get('text') or ''}")
        else:
            primary = {"decisions": "decision", "action_items": "task", "discussion_topics": "topic", "unresolved_questions": "question"}[key]
            items = minutes.get(key) or []
            if not items:
                lines.append("No items recorded.")
            for item in items:
                text = str(item.get(primary) or "")
                if key == "action_items":
                    if owners and item.get("owner"):
                        text += f" — Owner: {item['owner']}"
                    if item.get("deadline"):
                        text += f" — Deadline: {item['deadline']}"
                lines.append("- " + text)
                if key == "discussion_topics" and item.get("details"):
                    lines.append("  " + str(item["details"]))
    return "\n".join(lines).strip() + "\n"
