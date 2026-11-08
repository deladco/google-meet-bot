"""Single-host worker checkpoints and health checks for growing PCM WAV files."""
import json
import os
import re
import struct
import time
from contextlib import contextmanager
from pathlib import Path

import numpy as np


class Checkpoint:
    def __init__(self, root, table, job_id):
        if table not in {"bot_jobs", "audio_jobs"} or not re.fullmatch(r"[\w-]+", str(job_id)):
            raise ValueError("Invalid checkpoint identifier")
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / f"{table}_{job_id}.json"
        self.audio = self.path.with_suffix(".wav")
        self.table, self.job_id = table, job_id

    def read(self):
        return json.loads(self.path.read_text(encoding="utf-8"))

    def write(self, **fields):
        state = self.read() if self.path.exists() else {"table": self.table, "job_id": self.job_id, "attempts": 0}
        state.update(fields)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(state, handle, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(self.path)

    def cleanup(self):
        # Keep the journal until removal of the recording succeeds.
        self.audio.unlink(missing_ok=True)
        self.path.unlink(missing_ok=True)


@contextmanager
def worker_lock(root):
    """The OS releases this lock even if the worker is killed."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / "worker.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if not handle.tell():
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise RuntimeError("Another worker is already using this recovery directory.") from error
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def pcm_offset(path):
    """Read actual WAV chunks; ffmpeg can include a LIST chunk before data."""
    with Path(path).open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12 or header[:4] != b"RIFF" or header[8:] != b"WAVE":
            raise ValueError("Recording is not a PCM WAV file")
        while chunk := handle.read(8):
            if len(chunk) != 8:
                break
            name, size = struct.unpack("<4sI", chunk)
            if name == b"data":
                return handle.tell()
            handle.seek(size + size % 2, 1)
    raise ValueError("Recording has no audio data header")


def repair_wav(path):
    """Finalize lengths after a crash, without decoding or copying the audio."""
    path = Path(path)
    offset = pcm_offset(path)
    size = path.stat().st_size
    length = (size - offset) // 2 * 2
    with path.open("r+b") as handle:
        handle.truncate(offset + length)
        handle.seek(4)
        handle.write(struct.pack("<I", offset + length - 8))
        handle.seek(offset - 4)
        handle.write(struct.pack("<I", length))
    return length


class RecordingHealth:
    def __init__(self, path, *, stall_seconds=20, silence_seconds=60, clock=time.monotonic):
        self.path = Path(path)
        self.clock = clock
        self.stall_seconds, self.silence_seconds = stall_seconds, silence_seconds
        self.last_growth = self.last_sound = clock()
        self.position = None

    def check(self):
        """Return (warning, fatal). Silence warns but never ends a meeting."""
        now = self.clock()
        try:
            if self.position is None:
                self.position = pcm_offset(self.path)
            size = self.path.stat().st_size
            available = (size - self.position) // 2 * 2
            if available > 0:
                # Bound memory and inspect only the newest five seconds.
                with self.path.open("rb") as handle:
                    handle.seek(self.position + max(0, available - 16000 * 2 * 5))
                    data = handle.read(min(available, 16000 * 2 * 5))
                self.position += available
                self.last_growth = now
                samples = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768
                if samples.size and float(np.sqrt(np.mean(samples * samples))) >= 0.001:
                    self.last_sound = now
        except (OSError, ValueError):
            # A new recorder may not have written its header yet.
            if now - self.last_growth < self.stall_seconds:
                return None, False
        if now - self.last_growth >= self.stall_seconds:
            return "Audio capture stopped producing data. Saving the captured portion.", True
        if now - self.last_sound >= self.silence_seconds:
            return f"No audible sound for at least {self.silence_seconds:g} seconds. Check meeting playback and the audio output device.", False
        return None, False
