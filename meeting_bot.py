"""Send a Recall.ai bot into a Google Meet, Zoom or Microsoft Teams meeting."""
import httpx
from urllib.parse import urlparse
from remote_config import get_setting

DEFAULT_REGION = "us-east-1"
SUPPORTED_HOSTS = (
    "meet.google.com",
    "zoom.us",
    "zoom.com",
    "teams.microsoft.com",
    "teams.live.com",
)
FINISHED = {"done"}
FAILED = {"fatal", "call_ended_by_host_error"}
STATUS_LABELS = {
    "joining_call": "Joining the meeting",
    "in_waiting_room": "Waiting in the lobby - admit the bot",
    "in_call_not_recording": "In the call, not recording yet",
    "recording_permission_allowed": "Recording permission granted",
    "in_call_recording": "In the call and recording",
    "call_ended": "Meeting ended, preparing the recording",
    "done": "Recording ready",
    "fatal": "The bot failed",
}


def is_configured():
    return bool(get_setting("RECALL_API_KEY"))


def _base_url():
    region = get_setting("RECALL_REGION") or DEFAULT_REGION
    return f"https://{region}.recall.ai/api/v1"


def _headers():
    key = get_setting("RECALL_API_KEY")
    if not key:
        raise RuntimeError("Set RECALL_API_KEY in the app secrets to use the meeting bot.")
    return {"Authorization": f"Token {key}", "Accept": "application/json"}


def _check(response):
    if response.status_code >= 400:
        detail = response.text.strip()[:300]
        raise RuntimeError(f"Recall.ai error {response.status_code}: {detail}")
    return response


def validate_meeting_url(url):
    url = (url or "").strip()
    if not url.lower().startswith("https://"):
        raise ValueError("Enter the full meeting link starting with https://")
    host = (urlparse(url).hostname or "").lower()
    if not any(host == h or host.endswith("." + h) for h in SUPPORTED_HOSTS):
        raise ValueError("Only Google Meet, Zoom and Microsoft Teams links are supported.")
    return url


def join_meeting(meeting_url, bot_name="AI Assistant"):
    """Creates a bot that joins the meeting and records mixed audio. Returns the bot id."""
    payload = {
        "meeting_url": validate_meeting_url(meeting_url),
        "bot_name": (bot_name or "AI Assistant").strip()[:100],
        "recording_config": {"audio_mixed_mp3": {}},
    }
    response = _check(
        httpx.post(f"{_base_url()}/bot", headers=_headers(), json=payload, timeout=30)
    )
    return response.json()["id"]


def get_bot(bot_id):
    return _check(
        httpx.get(f"{_base_url()}/bot/{bot_id}", headers=_headers(), timeout=30)
    ).json()


def leave_meeting(bot_id):
    _check(
        httpx.post(f"{_base_url()}/bot/{bot_id}/leave_call", headers=_headers(), timeout=30)
    )


def status_code(bot):
    changes = bot.get("status_changes") or []
    return changes[-1].get("code", "") if changes else ""


def describe_status(code):
    return STATUS_LABELS.get(code, code.replace("_", " ").capitalize() or "Starting")


def audio_download_url(bot):
    for recording in bot.get("recordings") or []:
        shortcut = (recording.get("media_shortcuts") or {}).get("audio_mixed") or {}
        url = (shortcut.get("data") or {}).get("download_url")
        if url:
            return url
    return None


def download_audio(bot):
    url = audio_download_url(bot)
    if not url:
        raise RuntimeError("The bot finished but no audio recording is available yet.")
    response = httpx.get(url, timeout=300, follow_redirects=True)
    response.raise_for_status()
    return response.content
