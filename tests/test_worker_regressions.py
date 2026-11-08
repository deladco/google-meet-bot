import sys
import threading
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import bot_worker as bw
import remote_workflow as workflow


@pytest.fixture(autouse=True)
def isolated_recovery(monkeypatch, tmp_path):
    monkeypatch.setattr(bw, "RECOVERY_DIR", tmp_path)


def test_transcription_preserves_repeated_responses(monkeypatch):
    monkeypatch.setattr(workflow, "vocabulary", list)
    segments = [
        SimpleNamespace(start=i * 60, end=i * 60 + 1, text="Yes.")
        for i in range(5)
    ]
    # Confidence filtering must still reject a low-confidence hallucination.
    segments.append(SimpleNamespace(start=301, end=302, text="Noise", no_speech_prob=0.99))
    whisper = SimpleNamespace(transcribe=lambda *args, **kwargs: (segments, None))
    entries = workflow._transcribe_once(whisper, workflow.np.zeros(16), datetime.now(UTC), True)
    assert [entry["start"] for entry in entries] == [0, 60, 120, 180, 240]
    assert all(entry["text"] == "Yes." for entry in entries)


class JobDatabase:
    def __init__(self, status):
        self.row = {"id": "job", "status": status}

    def table(self, name):
        database = self

        class Update:
            def __init__(self):
                self.filters = {}

            def update(self, fields):
                self.fields = fields
                return self

            def eq(self, key, value):
                self.filters[key] = value
                return self

            def execute(self):
                if all(database.row.get(key) == value for key, value in self.filters.items()):
                    database.row.update(self.fields)
                    return SimpleNamespace(data=[dict(database.row)])
                return SimpleNamespace(data=[])

        return Update()


@pytest.mark.parametrize("status,expected", [
    ("stop_requested", "cancelled"), ("cancelled", "cancelled"),
    ("recording", "recording"), ("joining", "joining"),
])
def test_stale_queue_read_does_not_start_or_overwrite_job(monkeypatch, status, expected):
    db = JobDatabase(status)
    monkeypatch.setattr(bw, "record_meeting", lambda *args: pytest.fail("Should not join"))
    bw.run_job(db, {"id": "job", "status": "queued"})
    assert db.row["status"] == expected


def test_only_first_worker_claims_job(monkeypatch):
    db = JobDatabase("queued")
    calls = []

    def record(*args):
        calls.append(True)
        assert db.row["status"] == "joining"
        # Another worker with an old copy cannot claim the same job.
        bw.run_job(db, {"id": "job", "status": "queued"})
        return False

    monkeypatch.setattr(bw, "record_meeting", record)
    bw.run_job(db, {"id": "job", "status": "queued"})
    assert calls == [True]
    assert db.row["status"] == "cancelled"


def test_calendar_continues_while_worker_is_busy_and_survives_errors(monkeypatch):
    polled = threading.Event()
    calls = []
    clients = iter([object(), object(), object()])
    monkeypatch.setattr(bw, "service_client", lambda: next(clients))
    monkeypatch.setattr(bw, "CALENDAR_SYNC_SECONDS", 0.01)
    monkeypatch.setattr(bw.worker_availability, "heartbeat_loop", lambda db, stop, identity: stop.wait(2))

    def sync(db):
        calls.append(db)
        if len(calls) == 1:
            raise RuntimeError("Temporary calendar failure")
        polled.set()

    def busy_worker(db):
        assert polled.wait(2), "Calendar polling blocked behind the worker"
        assert len(calls) >= 2
        assert calls[0] is not db
        raise KeyboardInterrupt

    monkeypatch.setattr(bw.calendar_sync, "sync_feeds", sync)
    monkeypatch.setattr(bw, "worker_loop", busy_worker)
    with pytest.raises(KeyboardInterrupt):
        bw.main()
