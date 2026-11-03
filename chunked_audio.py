"""Worker helpers for recordings that the browser streams to storage as small chunks.

MediaRecorder chunks are pieces of one WebM stream: only the first has the header, so they must be
joined in order and decoded together. A recording whose tab was closed can end mid-packet, so decoding
keeps whatever was readable instead of failing.
"""
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

SAMPLE_RATE = 16000
BUCKET = "recordings"
STALE_SECONDS = 150  # no new chunk for this long means the browser is gone


def list_chunks(db, prefix):
    """Chunk objects under prefix, oldest first (names are zero-padded sequence numbers)."""
    items = db.storage.from_(BUCKET).list(prefix, {"limit": 10000, "sortBy": {"column": "name", "order": "asc"}})
    return sorted((i for i in items if i.get("name") and i["name"] != ".emptyFolderPlaceholder"), key=lambda i: i["name"])


def newest_activity(chunks):
    """Datetime of the most recent chunk, or None."""
    stamps = []
    for item in chunks:
        raw = item.get("updated_at") or item.get("created_at")
        if raw:
            stamps.append(datetime.fromisoformat(raw.replace("Z", "+00:00")))
    return max(stamps) if stamps else None


def is_stale(chunks, fallback_iso):
    last = newest_activity(chunks)
    if last is None:
        last = datetime.fromisoformat(fallback_iso.replace("Z", "+00:00"))
    return (datetime.now(timezone.utc) - last).total_seconds() > STALE_SECONDS


def download_chunks(db, prefix, chunks, target, done=None):
    """Appends chunks not in `done` to the file `target`, in order; returns the updated set of names."""
    done = set(done or ())
    with open(target, "ab") as out:
        for item in chunks:
            if item["name"] in done:
                continue
            out.write(db.storage.from_(BUCKET).download(f"{prefix}/{item['name']}"))
            done.add(item["name"])
    return done


def decode_partial(path):
    """All readable audio as 16 kHz mono float32; tolerates a file that stops mid-packet."""
    import av

    pieces = []
    resampler = av.AudioResampler(format="s16", layout="mono", rate=SAMPLE_RATE)
    try:
        container = av.open(str(path))
    except Exception:
        return np.zeros(0, dtype=np.float32)
    try:
        stream = next((s for s in container.streams if s.type == "audio"), None)
        if stream is None:
            return np.zeros(0, dtype=np.float32)
        try:
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    pieces.append(out.to_ndarray().reshape(-1))
        except Exception:
            pass  # truncated tail: keep what decoded
    finally:
        container.close()
    if not pieces:
        return np.zeros(0, dtype=np.float32)
    return np.concatenate(pieces).astype(np.float32) / 32768.0


def write_wav(samples, target):
    import wave

    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm.tobytes())


class LiveRecordingCaptions:
    """Keeps a local copy of a recording that is still being uploaded and captions the new speech.

    tick() is called from the worker loop; it does at most one transcription per call.
    """

    MIN_NEW_SECONDS = 10
    MAX_CHUNK_SECONDS = 30
    MAX_CHARS = 6000

    def __init__(self, job_id, prefix, workdir):
        self.job_id = job_id
        self.prefix = prefix
        self.path = Path(workdir) / f"{job_id}.webm"
        self.done = set()
        self.position = 0  # samples already captioned
        self.lines = []
        self.last_run = 0.0

    def tick(self, db, publish):
        from remote_workflow import _transcribe_samples, _whisper_model

        chunks = list_chunks(db, self.prefix)
        if not [c for c in chunks if c["name"] not in self.done]:
            return
        self.done = download_chunks(db, self.prefix, chunks, self.path, self.done)
        samples = decode_partial(self.path)
        if len(samples) - self.position < self.MIN_NEW_SECONDS * SAMPLE_RATE:
            return
        end = min(len(samples), self.position + self.MAX_CHUNK_SECONDS * SAMPLE_RATE)
        piece = samples[self.position:end]
        offset = self.position / SAMPLE_RATE
        self.position = end
        entries = _transcribe_samples(_whisper_model(), piece, datetime.now(timezone.utc), True)
        added = [f"[{int((offset + e['start']) // 60):02d}:{int((offset + e['start']) % 60):02d}] {e['text']}" for e in entries]
        if not added:
            return
        self.lines.extend(added)
        text = "\n".join(self.lines)
        if len(text) > self.MAX_CHARS:
            text = text[-self.MAX_CHARS:].split("\n", 1)[-1]
            self.lines = text.split("\n")
        publish(text)

    def cleanup(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            pass
