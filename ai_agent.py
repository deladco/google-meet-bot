import argparse
import json
from datetime import datetime
from pathlib import Path

from remote_config import get_ollama_client

PROJECT_DIR = Path(__file__).resolve().parent
OUTPUT = PROJECT_DIR / "output"
DEFAULT_TRANSCRIPT_FILE = OUTPUT / "transcript.jsonl"
MODEL = "llama3.2:3b"
MIN_TRANSCRIPT_WORDS = 20
MINUTES_SCHEMA = {
	"type": "object",
	"properties": {
		"summary": {"type": "string"},
		"summary_refs": {
			"type": "array",
			"items": {"type": "integer", "minimum": 0},
		},
		"discussion_topics": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"topic": {"type": "string"},
					"details": {"type": "string"},
					"segment_indices": {
						"type": "array",
						"items": {"type": "integer", "minimum": 0},
					},
				},
				"required": ["topic", "details", "segment_indices"],
				"additionalProperties": False,
			},
		},
		"decisions": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"decision": {"type": "string"},
					"segment_indices": {
						"type": "array",
						"items": {"type": "integer", "minimum": 0},
					},
				},
				"required": ["decision", "segment_indices"],
				"additionalProperties": False,
			},
		},
		"action_items": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"task": {"type": "string"},
					"owner": {"type": "string"},
					"deadline": {"type": "string"},
					"segment_indices": {
						"type": "array",
						"items": {"type": "integer", "minimum": 0},
					},
				},
				"required": ["task", "owner", "deadline", "segment_indices"],
				"additionalProperties": False,
			},
		},
		"unresolved_questions": {
			"type": "array",
			"items": {
				"type": "object",
				"properties": {
					"question": {"type": "string"},
					"segment_indices": {
						"type": "array",
						"items": {"type": "integer", "minimum": 0},
					},
				},
				"required": ["question", "segment_indices"],
				"additionalProperties": False,
			},
		},
	},
	"required": [
		"summary",
		"summary_refs",
		"discussion_topics",
		"decisions",
		"action_items",
		"unresolved_questions",
	],
	"additionalProperties": False,
}


def load_transcript(path=DEFAULT_TRANSCRIPT_FILE):
	if not path.exists():
		raise FileNotFoundError(f"No transcript found at {path}")

	with path.open("r", encoding="utf-8") as file:
		entries = [
			json.loads(line)
			for line in file
			if line.strip()
		]

	if not entries:
		raise ValueError(f"Transcript is empty: {path}")

	return entries



def _references(segment_indices, entries):
	references = []
	seen = set()
	for index in segment_indices:
		if not isinstance(index, int) or index < 0 or index >= len(entries) or index in seen:
			continue
		seen.add(index)
		entry = entries[index]
		references.append({
			"segment_index": index,
			"timestamp": entry.get("timestamp", ""),
			"start": entry.get("start"),
			"end": entry.get("end"),
			"speaker": entry.get("speaker", "Unknown"),
			"quote": entry.get("text", ""),
		})
	return references


def _attach_references(items, entries, key):
	validated = []
	for item in items:
		references = _references(item.pop("segment_indices", []), entries)
		if references:
			item["transcript_refs"] = references
			validated.append(item)
	return validated


def analyze_meeting(transcript_path=DEFAULT_TRANSCRIPT_FILE):
	entries = load_transcript(transcript_path)
	conversation = "\n".join(
		f"[{index}] {entry.get('timestamp', '')} "
		f"({entry.get('start', '?')}-{entry.get('end', '?')}s) "
		f"{entry.get('speaker', 'Unknown')}: {entry.get('text', '')}"
		for index, entry in enumerate(entries)
	)
	if len(conversation.split()) < MIN_TRANSCRIPT_WORDS:
		raise ValueError(
			"Transcript is too short for reliable meeting notes; "
			"record more audio before analyzing."
		)

	response = get_ollama_client().chat(
		model=MODEL,
		format=MINUTES_SCHEMA,
		options={"temperature": 0},
		messages=[
			{
				"role": "system",
				"content": (
					"Return structured meeting minutes. Every topic, decision, action, "
					"question, and summary must cite relevant segment_indices from the "
					"provided transcript. Never invent or infer owners, deadlines, "
					"decisions, or facts. Use 'Not specified' when an owner or deadline "
					"is not explicit. Return empty arrays when there is no evidence."
				)
			},
			{"role": "user", "content": conversation}
		]
	)

	minutes = json.loads(response.message.content)
	minutes["summary_refs"] = _references(minutes.get("summary_refs", []), entries)
	for key in ("discussion_topics", "decisions", "action_items", "unresolved_questions"):
		minutes[key] = _attach_references(minutes.get(key, []), entries, key)
	return minutes


def render_markdown(minutes):
	lines = ["# Meeting Minutes", "", "## Summary", "", minutes["summary"]]

	def add_reference_lines(references):
		for reference in references:
			start = reference.get("start")
			end = reference.get("end")
			if isinstance(start, (int, float)) and isinstance(end, (int, float)):
				timing = f"{start:.2f}-{end:.2f}s"
			else:
				timing = "audio offset unavailable"
			timestamp = reference.get("timestamp", "timestamp unavailable")
			speaker = reference.get("speaker", "Unknown")
			quote = reference.get("quote", "")
			lines.append(
				f'  - [{timing}; {timestamp}] {speaker}: "{quote}"'
			)

	add_reference_lines(minutes.get("summary_refs", []))
	sections = (
		("Discussion Topics", "discussion_topics", "topic"),
		("Decisions", "decisions", "decision"),
		("Action Items", "action_items", "task"),
		("Unresolved Questions", "unresolved_questions", "question"),
	)
	for title, key, label in sections:
		lines.extend(["", f"## {title}"])
		items = minutes.get(key, [])
		if not items:
			lines.extend(["", "Not captured in transcript."])
			continue
		for item in items:
			if key == "action_items":
				owner = item.get("owner") or "Not specified"
				deadline = item.get("deadline") or "Not specified"
				lines.extend([
					"",
					f"- {item[label]} (Owner: {owner}; Deadline: {deadline})",
				])
			else:
				lines.extend(["", f"- {item[label]}"])
			if item.get("details"):
				lines.append(f"  {item['details']}")
			add_reference_lines(item.get("transcript_refs", []))
	return "\n".join(lines) + "\n"


if __name__ == "__main__":
	parser = argparse.ArgumentParser()
	parser.add_argument(
		"transcript",
		nargs="?",
		type=Path,
		default=DEFAULT_TRANSCRIPT_FILE
	)
	parser.add_argument(
		"--output",
		type=Path,
		default=None
	)
	parser.add_argument("--json-output", type=Path, default=None)
	args = parser.parse_args()

	print("Analyzing transcript locally with Ollama...", flush=True)
	minutes = analyze_meeting(args.transcript)
	markdown = render_markdown(minutes)
	print(markdown)
	output_path = args.output or OUTPUT / (
		"meeting_notes_" + datetime.now().strftime("%Y%m%d_%H%M%S") + ".md"
	)
	json_path = args.json_output or output_path.with_suffix(".json")
	output_path.parent.mkdir(parents=True, exist_ok=True)
	json_path.parent.mkdir(parents=True, exist_ok=True)
	output_path.write_text(markdown, encoding="utf-8")
	json_path.write_text(json.dumps(minutes, indent=2, ensure_ascii=False), encoding="utf-8")
	print(f"Meeting notes saved: {output_path.resolve()}")
	print(f"Structured minutes saved: {json_path.resolve()}")
