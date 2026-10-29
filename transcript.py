import json
import time
from pathlib import Path

TRANSCRIPT_FILE = Path(__file__).resolve().parent / "output" / "transcript.jsonl"


def load_transcript(path=TRANSCRIPT_FILE):
	with path.open("r", encoding="utf-8") as file:
		return [
			json.loads(line)
			for line in file
			if line.strip()
		]


def wait_for_transcript(timeout=900, poll_interval=30):
	deadline = time.monotonic() + timeout

	while True:
		try:
			if load_transcript():
				return TRANSCRIPT_FILE
		except (FileNotFoundError, json.JSONDecodeError):
			pass

		remaining = deadline - time.monotonic()
		if remaining <= 0:
			raise TimeoutError(
				"Timed out waiting for a local Faster-Whisper transcript."
			) from None

		wait_seconds = min(poll_interval, remaining)
		print(
			f"Local transcript not ready; checking again in "
			f"{wait_seconds:.0f} seconds...",
			flush=True
		)
		time.sleep(wait_seconds)


if __name__ == "__main__":
	file = wait_for_transcript()
	print(f"Local transcript ready: {file}")
