"""Meeting-bot jobs. The app queues a job in Supabase; bot_worker.py joins and records it."""
from urllib.parse import urlparse

import auth_db

JOBS_TABLE = "bot_jobs"
SUPPORTED_HOSTS = (
    "meet.google.com",
    "zoom.us",
    "zoom.com",
    "teams.microsoft.com",
    "teams.live.com",
)
FINISHED = {"done"}
FAILED = {"failed", "cancelled"}
STATUS_LABELS = {
    "queued": "Waiting for the bot worker to pick this up",
    "joining": "Joining the meeting (admit the bot if it is in the lobby)",
    "recording": "In the meeting and recording",
    "stop_requested": "Asked to leave, finishing up",
    "processing": "Meeting ended, transcribing and writing minutes",
    "done": "Done - the meeting is in your history",
    "failed": "The bot failed",
    "cancelled": "Cancelled",
}


def is_configured():
    return auth_db.is_configured()


def platform_of(url):
    host = (urlparse(url).hostname or "").lower()
    if host == "meet.google.com":
        return "meet"
    if host.endswith("zoom.us") or host.endswith("zoom.com"):
        return "zoom"
    return "teams"


def validate_meeting_url(url):
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        raise ValueError("Enter the full meeting link starting with https://")
    host = (urlparse(url).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS):
        raise ValueError("Only Google Meet, Zoom and Microsoft Teams links are supported.")
    return url


def join_meeting(client, meeting_url, bot_name="AI Assistant", user_email=None):
    """Queues a job for the worker. Returns the job id."""
    row = {
        "meeting_url": validate_meeting_url(meeting_url),
        "bot_name": (bot_name or "AI Assistant").strip()[:100],
        "user_email": user_email,
    }
    return client.table(JOBS_TABLE).insert(row).execute().data[0]["id"]


def get_job(client, job_id):
    rows = client.table(JOBS_TABLE).select("*").eq("id", job_id).limit(1).execute().data
    return rows[0] if rows else None


def leave_meeting(client, job_id):
    client.table(JOBS_TABLE).update({"status": "stop_requested"}).eq("id", job_id).in_(
        "status", ["queued", "joining", "recording"]
    ).execute()


def describe_status(code):
    return STATUS_LABELS.get(code, code.replace("_", " ").capitalize())
