import ast
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import auth_db
import bot_worker as bw
import job_retry
from worker_state import Checkpoint


class Database:
    def __init__(self, status="failed_processing"):
        self.row = {"id": "job", "status": status, "user_id": "owner"}
        self.updates = []

    def table(self, table):
        database = self

        class Query:
            def __init__(self):
                self.filters = {}
                self.fields = None

            def select(self, columns):
                return self

            def limit(self, count):
                return self

            def update(self, fields):
                self.fields = fields
                return self

            def eq(self, key, value):
                self.filters[key] = value
                return self

            def execute(self):
                if all(database.row.get(key) == value for key, value in self.filters.items()):
                    if self.fields is not None:
                        database.row.update(self.fields)
                        database.updates.append(dict(self.fields))
                    return SimpleNamespace(data=[dict(database.row)])
                return SimpleNamespace(data=[])

        return Query()


def test_request_is_atomic_and_reuses_same_job():
    db = Database()
    failed = dict(db.row)
    job_retry.request(db, "bot_jobs", failed)
    assert db.row["status"] == "retry_requested"
    assert db.row["id"] == "job" and db.row["user_id"] == "owner"
    with pytest.raises(ValueError, match="already changed"):
        job_retry.request(db, "bot_jobs", failed)
    assert len(db.updates) == 1


@pytest.mark.parametrize("status", ["queued", "recording", "processing", "done", "failed_exhausted"])
def test_active_completed_and_exhausted_jobs_cannot_be_retried(status):
    db = Database(status)
    with pytest.raises(ValueError, match="cannot be retried"):
        job_retry.request(db, "bot_jobs", dict(db.row))
    assert not db.updates


def test_request_cannot_change_another_job():
    db = Database()
    with pytest.raises(ValueError, match="already changed"):
        job_retry.request(db, "bot_jobs", {"id": "other", "status": "failed_processing"})
    assert db.row["status"] == "failed_processing"


def test_rejoin_requires_explicit_no_audio_bot_failure():
    db = Database("failed_saving")
    with pytest.raises(ValueError, match="without captured audio"):
        job_retry.request(db, "bot_jobs", dict(db.row), rejoin=True)
    db.row["status"] = "failed_no_audio"
    with pytest.raises(ValueError, match="no saved audio"):
        job_retry.request(db, "bot_jobs", dict(db.row))
    job_retry.request(db, "bot_jobs", dict(db.row), rejoin=True)
    assert db.row["status"] == "rejoin_requested"


@pytest.fixture
def checkpoint(monkeypatch, tmp_path):
    monkeypatch.setattr(bw, "RECOVERY_DIR", tmp_path)
    return Checkpoint(tmp_path, "bot_jobs", "job")


def test_processing_retry_reuses_audio_and_does_not_rejoin(monkeypatch, checkpoint):
    db = Database("retry_requested")
    checkpoint.audio.write_bytes(b"x" * 12000)
    checkpoint.write(stage="processing")
    processed = []
    monkeypatch.setattr(bw, "process_bot_recording", lambda db, job, cp: processed.append(cp.audio))
    monkeypatch.setattr(bw, "record_meeting", lambda *args: pytest.fail("Must not rejoin"))
    bw.retry_job(db, dict(db.row), "bot_jobs")
    assert processed == [checkpoint.audio]
    assert checkpoint.read()["attempts"] == 1
    # A second worker's stale request cannot claim this job again.
    bw.retry_job(db, {**db.row, "status": "retry_requested"}, "bot_jobs")
    assert len(processed) == 1


def test_saving_retry_does_not_transcribe_or_rejoin(monkeypatch, checkpoint):
    db = Database("retry_requested")
    checkpoint.write(stage="saving", result={"meeting_id": "existing"})
    saved = []
    monkeypatch.setattr(bw, "finish_job", lambda db, job, cp: saved.append(cp.read()["result"]))
    monkeypatch.setattr(bw, "process_uploaded_recording", lambda *args, **kwargs: pytest.fail("Must not transcribe"))
    monkeypatch.setattr(bw, "record_meeting", lambda *args: pytest.fail("Must not rejoin"))
    bw.retry_job(db, dict(db.row), "bot_jobs")
    assert saved == [{"meeting_id": "existing"}]


def test_missing_audio_retry_requires_separate_send_bot_action(monkeypatch, checkpoint):
    db = Database("retry_requested")
    monkeypatch.setattr(bw, "record_meeting", lambda *args: pytest.fail("Must not rejoin"))
    bw.retry_job(db, dict(db.row), "bot_jobs")
    assert db.row["status"] == "failed_no_audio"
    assert "Send the bot again" in db.row["message"]


def test_explicit_rejoin_keeps_job_id_and_attempt_count(monkeypatch, checkpoint):
    db = Database("rejoin_requested")
    checkpoint.write(stage="recording", attempts=1)
    captured = []
    monkeypatch.setattr(bw, "run_job", lambda db, job: captured.append(job))
    bw.retry_job(db, dict(db.row), "bot_jobs")
    assert captured[0]["id"] == "job" and captured[0]["status"] == "queued"
    assert checkpoint.read()["attempts"] == 2


def test_even_explicit_rejoin_preserves_preexisting_audio(monkeypatch, checkpoint):
    db = Database("rejoin_requested")
    checkpoint.audio.write_bytes(b"x" * 12000)
    checkpoint.write(stage="processing")
    reused = []
    monkeypatch.setattr(bw, "process_bot_recording", lambda *args: reused.append(True))
    monkeypatch.setattr(bw, "run_job", lambda *args: pytest.fail("Must preserve old recording"))
    bw.retry_job(db, dict(db.row), "bot_jobs")
    assert reused == [True]


def test_worker_enforces_retry_limit_independently_of_browser(monkeypatch, checkpoint):
    db = Database("retry_requested")
    checkpoint.write(stage="processing", attempts=3)
    monkeypatch.setattr(bw, "process_bot_recording", lambda *args: pytest.fail("Retry limit bypassed"))
    bw.retry_job(db, dict(db.row), "bot_jobs")
    assert db.row["status"] == "failed_exhausted"
    assert checkpoint.read()["attempts"] == 3


def test_audio_retry_returns_to_queue_and_preserves_attempt_count(monkeypatch, tmp_path):
    monkeypatch.setattr(bw, "RECOVERY_DIR", tmp_path)
    checkpoint = Checkpoint(tmp_path, "audio_jobs", "job")
    checkpoint.write(stage="processing", attempts=1)
    db = Database("retry_requested")
    bw.retry_job(db, dict(db.row), "audio_jobs")
    assert db.row["status"] == "queued"
    assert checkpoint.read()["attempts"] == 2


@pytest.mark.parametrize("stage,status", [("saving", "failed_saving"), ("processing", "failed_processing")])
def test_failures_publish_correct_retry_action(checkpoint, stage, status):
    checkpoint.write(stage=stage, result={"meeting_id": "saved"})
    checkpoint.audio.write_bytes(b"x" * 12000)
    db = Database("processing")
    bw.fail_job(db, dict(db.row), "bot_jobs", RuntimeError("Offline"))
    assert db.row["status"] == status


def test_damaged_checkpoint_does_not_crash_failure_handler(checkpoint):
    checkpoint.path.write_text("broken json", encoding="utf-8")
    db = Database("processing")
    bw.fail_job(db, dict(db.row), "bot_jobs", RuntimeError("Bad checkpoint"))
    assert db.row["status"] == "failed_exhausted"


@pytest.mark.parametrize("status", ["failed_no_audio", "retry_requested", "rejoin_requested"])
def test_restart_does_not_consume_pending_manual_retry(checkpoint, status):
    db = Database(status)
    checkpoint.write(stage="recording", attempts=1)
    bw.recover_jobs(db)
    assert db.row["status"] == status
    assert checkpoint.read()["attempts"] == 1


@pytest.mark.parametrize("status,label,next_status", [
    ("failed_saving", "Retry saving", "retry_requested"),
    ("failed_processing", "Retry processing", "retry_requested"),
    ("failed_no_audio", "Send bot again", "rejoin_requested"),
])
def test_dashboard_retry_button_queues_existing_job(monkeypatch, status, label, next_status):
    from streamlit.testing.v1 import AppTest

    db = Database(status)
    monkeypatch.setattr(auth_db, "client_for_user", lambda: db)
    source = (Path(__file__).resolve().parents[1] / "meeting_app.py").read_text(encoding="utf-8")
    function = next(node for node in ast.parse(source).body if isinstance(node, ast.FunctionDef) and node.name == "render_retry_controls")
    fragment = ast.get_source_segment(source, function)
    app = AppTest.from_string(
        "import streamlit as st\nimport auth_db\nimport job_retry\nimport logging\n" + fragment +
        "\nclient = auth_db.client_for_user()\n"
        "if client.row['status'] in job_retry.FAILURE_STATUSES:\n"
        "    render_retry_controls(client, dict(client.row), 'bot_jobs', 'test')\n"
        "else:\n    st.info('Request queued')\n"
    ).run()
    assert not app.exception and app.button[0].label == label
    app.button[0].click().run()
    assert not app.exception
    assert db.row["status"] == next_status and db.row["id"] == "job"
    assert not app.button
