"""Privacy-safe worker heartbeats shared by the dashboard and worker process."""
import logging
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from uuid import UUID, uuid4

TABLE = "worker_heartbeats"
HEARTBEAT_SECONDS = 20
OFFLINE_SECONDS = 90
ACTIVITY_LABELS = {
    "idle": "Ready for a meeting",
    "joining": "Joining a meeting",
    "recording": "Recording a meeting",
    "processing": "Processing a recording",
    "transcribing": "Transcribing audio",
    "summarizing": "Writing meeting notes",
    "recovering": "Recovering interrupted work",
    "offline": "Stopped",
}


class Activity:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = "idle"
        self._job = None

    def snapshot(self):
        with self._lock:
            return self._state

    def update_job(self, table, job_id, status):
        with self._lock:
            if self._job == (table, job_id) and status in ACTIVITY_LABELS:
                self._state = status

    @contextmanager
    def working(self, state, job=None):
        with self._lock:
            previous = self._state, self._job
            self._state, self._job = state, job
        try:
            yield
        finally:
            with self._lock:
                self._state, self._job = previous


ACTIVITY = Activity()


def track_job(table):
    def decorate(function):
        @wraps(function)
        def wrapped(db, job, *args, **kwargs):
            state = "joining" if table == "bot_jobs" else "processing"
            with ACTIVITY.working(state, (table, job["id"])):
                return function(db, job, *args, **kwargs)
        return wrapped
    return decorate


def worker_id(root):
    """Persist an opaque ID; never publish the host name or any meeting metadata."""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    path = root / "worker_id.txt"
    if path.exists():
        try:
            return str(UUID(path.read_text(encoding="utf-8").strip()))
        except ValueError:
            logging.getLogger(__name__).warning("Replacing invalid worker identity")
    identity = str(uuid4())
    temporary = path.with_suffix(".tmp")
    temporary.write_text(identity, encoding="utf-8")
    temporary.replace(path)
    return identity


def publish(db, identity, state):
    db.table(TABLE).upsert({
        "worker_id": identity,
        "last_seen_at": datetime.now(UTC).isoformat(),
        "activity": state,
    }, on_conflict="worker_id").execute()


def heartbeat_loop(db, stop, identity):
    """Independent of inference, capture, and calendar polling."""
    while not stop.is_set():
        try:
            publish(db, identity, ACTIVITY.snapshot())
        except Exception:
            logging.getLogger(__name__).exception("Worker heartbeat failed; check the worker availability migration and database connection")
        if stop.wait(HEARTBEAT_SECONDS):
            break
    try:
        publish(db, identity, "offline")
    except Exception:
        logging.getLogger(__name__).debug("Could not publish worker shutdown; heartbeat expiry will mark it offline", exc_info=True)


def summarize(rows, now=None):
    now = now or datetime.now(UTC)
    online = []
    newest = None
    for row in rows:
        try:
            seen = datetime.fromisoformat(row["last_seen_at"])
            age = (now - seen).total_seconds()
        except (KeyError, TypeError, ValueError):
            continue
        # Do not present future timestamps from a badly skewed clock as healthy.
        if age < -30:
            continue
        newest = max(newest, seen) if newest else seen
        if age <= OFFLINE_SECONDS and row.get("activity") in ACTIVITY_LABELS and row["activity"] != "offline":
            online.append(row)
    available = [row for row in online if row["activity"] == "idle"]
    return {"online": online, "available": available, "last_seen": newest}


def read_status(client):
    rows = client.table(TABLE).select("worker_id,last_seen_at,activity").order("last_seen_at", desc=True).limit(100).execute().data
    return summarize(rows)
