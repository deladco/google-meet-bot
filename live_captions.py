"""Near-real-time captions: transcribes the growing meeting recording in short chunks."""
import threading
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
BYTES_PER_SECOND = SAMPLE_RATE * 2
MAX_CHUNK_SECONDS = 30


def _data_offset(path):
    """Where the PCM starts. Python's wave writer and ffmpeg put different headers in front of it."""
    with open(path, "rb") as handle:
        head = handle.read(512)
    index = head.find(b"data")
    return index + 8 if index >= 0 else None


def _clock(seconds):
    seconds = int(seconds)
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class LiveCaptions:
    """Runs beside the recorder. publish(text) receives the latest captions every few seconds.

    Never raises into the recording: if anything fails, captions stop and the recording carries on.
    """

    def __init__(self, wav_path, publish, interval=10, max_chars=6000):
        self.path = Path(wav_path)
        self.publish = publish
        self.interval = interval
        self.max_chars = max_chars
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=90)  # the speech model must be free before final transcription

    def _run(self):
        try:
            self._loop()
        except Exception as error:
            print(f"Live captions stopped: {error}", flush=True)

    def _loop(self):
        from remote_workflow import _transcribe_samples, _whisper_model

        position = None
        lines = []
        while not self._stop.wait(self.interval):
            try:
                if position is None:
                    position = _data_offset(self.path)
                    if position is None:
                        continue
                    origin = position
                size = self.path.stat().st_size
            except OSError:
                continue
            available = (size - position) // 2 * 2
            if available < self.interval * BYTES_PER_SECOND // 2:
                continue
            take = min(available, MAX_CHUNK_SECONDS * BYTES_PER_SECOND)
            with open(self.path, "rb") as handle:
                handle.seek(position)
                raw = handle.read(take)
            offset_seconds = (position - origin) / BYTES_PER_SECOND
            position += len(raw)
            samples = np.frombuffer(raw[: len(raw) // 2 * 2], dtype="<i2").astype(np.float32) / 32768.0
            entries = _transcribe_samples(_whisper_model(), samples, datetime.now(timezone.utc), True)
            added = [f"[{_clock(offset_seconds + e['start'])}] {e['text']}" for e in entries]
            if not added:
                continue
            lines.extend(added)
            text = "\n".join(lines)
            if len(text) > self.max_chars:
                text = text[-self.max_chars :].split("\n", 1)[-1]
                lines = text.split("\n")
            self.publish(text)
