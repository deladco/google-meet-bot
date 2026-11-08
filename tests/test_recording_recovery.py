import struct
import sys
import wave
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_worker as bw
from worker_state import Checkpoint, RecordingHealth, repair_wav, worker_lock


def write_audio(path, amplitude=0, seconds=1):
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16000)
        output.writeframes(np.full(16000 * seconds, amplitude, dtype="<i2").tobytes())


def append_audio(path, amplitude=0):
    with path.open("ab") as output:
        output.write(np.full(16000, amplitude, dtype="<i2").tobytes())


def test_silence_warns_without_stopping_and_sound_clears_warning(tmp_path):
    path = tmp_path / "audio.wav"
    now = [0]
    health = RecordingHealth(path, clock=lambda: now[0])
    write_audio(path)
    assert health.check() == (None, False)
    for second in range(10, 61, 10):
        now[0] = second
        append_audio(path)
        warning, fatal = health.check()
        assert not fatal
    assert "No audible sound" in warning
    now[0] = 61
    append_audio(path, amplitude=1000)
    assert health.check() == (None, False)


@pytest.mark.parametrize("create_file", [True, False])
def test_missing_or_stalled_capture_stops_after_grace(tmp_path, create_file):
    now = [0]
    path = tmp_path / "audio.wav"
    if create_file:
        write_audio(path, amplitude=1000)
    health = RecordingHealth(path, clock=lambda: now[0])
    assert health.check() == (None, False)
    now[0] = 21
    warning, fatal = health.check()
    assert fatal and "stopped producing data" in warning


def test_repair_recovers_unfinalized_header_and_drops_partial_sample(tmp_path):
    path = tmp_path / "audio.wav"
    write_audio(path, amplitude=1000)
    # A killed wave/ffmpeg writer may leave stale header lengths and a partial sample.
    append_audio(path, amplitude=2000)
    with path.open("ab") as output:
        output.write(b"x")
    assert repair_wav(path) == 64000
    with wave.open(str(path)) as audio:
        assert audio.getnframes() == 32000
        assert len(audio.readframes(32000)) == 64000


def test_repair_handles_ffmpeg_metadata_chunk(tmp_path):
    path = tmp_path / "audio.wav"
    write_audio(path)
    original = path.read_bytes()
    path.write_bytes(original[:36] + b"LIST" + struct.pack("<I", 4) + b"INFO" + original[36:])
    assert repair_wav(path) == 32000
    with wave.open(str(path)) as audio:
        assert audio.getnframes() == 16000


def test_worker_lock_rejects_second_worker_and_releases(tmp_path):
    with worker_lock(tmp_path), pytest.raises(RuntimeError, match="Another worker"), worker_lock(tmp_path):
        pytest.fail("Second worker acquired the lock")
    with worker_lock(tmp_path):
        pass


class Database:
    def __init__(self, status="queued"):
        self.row = {"id": "job", "status": status, "user_id": "owner", "meeting_url": "https://meet.google.com/abc"}

    def table(self, name):
        db = self

        class Query:
            def __init__(self):
                self.filters = {}
                self.fields = None

            def select(self, columns):
                return self

            def limit(self, count):
                return self

            def eq(self, key, value):
                self.filters[key] = value
                return self

            def update(self, fields):
                self.fields = fields
                return self

            def execute(self):
                if all(db.row.get(key) == value for key, value in self.filters.items()):
                    if self.fields is not None:
                        db.row.update(self.fields)
                    return SimpleNamespace(data=[dict(db.row)])
                return SimpleNamespace(data=[])

        return Query()


@pytest.fixture
def recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(bw, "RECOVERY_DIR", tmp_path)
    monkeypatch.setattr(bw, "MeetingHistory", lambda: SimpleNamespace(get_meeting=lambda key: {"meeting_id": key}))
    saved = []
    monkeypatch.setattr(bw.auth_db, "save_meeting_from_history", lambda db, meeting, **kwargs: saved.append(meeting))
    return tmp_path, saved


def test_restart_salvages_partial_recording_without_rejoining(monkeypatch, recovery):
    root, saved = recovery
    db = Database()

    def crash(db, job, path):
        write_audio(path, amplitude=1000)
        append_audio(path, amplitude=2000)
        raise RuntimeError("Simulated worker interruption")

    monkeypatch.setattr(bw, "record_meeting", crash)
    with pytest.raises(RuntimeError, match="interruption"):
        bw.run_job(db, dict(db.row))
    assert list(root.glob("*.wav"))
    monkeypatch.setattr(bw, "record_meeting", lambda *args: pytest.fail("Must not rejoin"))
    monkeypatch.setattr(bw, "process_uploaded_recording", lambda *args, **kwargs: {"meeting_id": "recovered", "status": "completed"})
    bw.recover_jobs(db)
    assert db.row["status"] == "done"
    assert db.row["meeting_id"] == "recovered"
    assert "Recovered the captured portion" in db.row["message"]
    assert saved == [{"meeting_id": "recovered"}]
    assert not list(root.glob("*.json")) and not list(root.glob("*.wav"))


def test_restart_retries_cloud_save_without_retranscribing(monkeypatch, recovery):
    root, saved = recovery
    db = Database("summarizing")
    checkpoint = Checkpoint(root, "bot_jobs", "job")
    write_audio(checkpoint.audio)
    checkpoint.write(stage="saving", result={"meeting_id": "existing", "status": "completed"})
    monkeypatch.setattr(bw, "process_uploaded_recording", lambda *args, **kwargs: pytest.fail("Must not retranscribe"))
    bw.recover_jobs(db)
    assert db.row["status"] == "done" and saved == [{"meeting_id": "existing"}]


def test_failed_cloud_save_retains_checkpoint_and_audio(monkeypatch, recovery):
    root, _ = recovery
    db = Database("processing")
    checkpoint = Checkpoint(root, "bot_jobs", "job")
    write_audio(checkpoint.audio)
    checkpoint.write(stage="saving", result={"meeting_id": "existing"})

    def offline(*args, **kwargs):
        raise ConnectionError("Offline")

    monkeypatch.setattr(bw.auth_db, "save_meeting_from_history", offline)
    bw.recover_jobs(db)
    assert checkpoint.path.exists() and checkpoint.audio.exists()
    assert checkpoint.read()["attempts"] == 1


def test_restart_before_audio_does_not_leave_job_stuck(recovery):
    root, _ = recovery
    db = Database("joining")
    Checkpoint(root, "bot_jobs", "job").write(stage="recording")
    bw.recover_jobs(db)
    assert db.row["status"] == "failed" and "Send the bot again" in db.row["message"]


def test_interrupted_audio_upload_processing_returns_to_queue(recovery):
    root, _ = recovery
    db = Database("processing")
    checkpoint = Checkpoint(root, "audio_jobs", "job")
    checkpoint.write(stage="processing")
    bw.recover_jobs(db)
    assert db.row["status"] == "queued"
    assert checkpoint.read()["attempts"] == 1


def test_recovery_retries_are_bounded(recovery):
    root, _ = recovery
    db = Database("processing")
    checkpoint = Checkpoint(root, "bot_jobs", "job")
    write_audio(checkpoint.audio)
    checkpoint.write(stage="processing", attempts=3)
    bw.recover_jobs(db)
    assert db.row["status"] == "failed" and "retry limit" in db.row["message"]
    assert checkpoint.audio.exists()


@pytest.mark.parametrize("kind", ["thread", "process", "device"])
def test_recorder_failure_is_detected(tmp_path, kind):
    recorder = bw.Recorder(tmp_path / "audio.wav")
    if kind == "thread":
        recorder._thread = SimpleNamespace(is_alive=lambda: False)
    elif kind == "process":
        recorder._process = SimpleNamespace(poll=lambda: 1)
    else:
        recorder.error = "Device unavailable"
    assert recorder.failure()


@pytest.mark.parametrize("cause", ["stalled", "recorder", "browser"])
def test_recording_stops_and_preserves_warning_on_health_failure(monkeypatch, tmp_path, cause):
    from playwright import sync_api

    stopped = []
    page = object()
    context = SimpleNamespace(new_page=lambda: page, storage_state=lambda **kwargs: None)
    browser = SimpleNamespace(new_context=lambda **kwargs: context, close=lambda: None)
    fake_playwright = SimpleNamespace(chromium=SimpleNamespace(launch=lambda **kwargs: browser))
    monkeypatch.setattr(sync_api, "sync_playwright", lambda: nullcontext(fake_playwright))
    recorder = SimpleNamespace(
        start=lambda: None, stop=lambda: stopped.append(True),
        failure=lambda: "Capture process failed" if cause == "recorder" else None,
    )
    monkeypatch.setattr(bw, "Recorder", lambda path: recorder)
    monkeypatch.setattr(bw, "RecordingHealth", lambda path: SimpleNamespace(
        check=lambda: ("Capture stalled", True) if cause == "stalled" else (None, False),
    ))
    monkeypatch.setattr(bw, "LIVE_CAPTIONS", False)
    monkeypatch.setattr(bw, "join", lambda *args: True)
    monkeypatch.setattr(bw, "in_meeting", lambda *args: True)
    monkeypatch.setattr(bw, "browser_closed", lambda *args: cause == "browser")
    monkeypatch.setattr(bw, "job_status", lambda *args: "recording")
    monkeypatch.setattr(bw, "set_job", lambda *args, **kwargs: None)
    monkeypatch.setattr(bw, "_click_first", lambda *args: True)
    job = {"id": "job", "meeting_url": "https://meet.google.com/abc", "bot_name": "Bot"}
    assert bw.record_meeting(None, job, tmp_path / "audio.wav") is True
    assert stopped == [True]
    assert job["recording_warning"]
