"""Retry requests use the existing job row and its RLS-protected status field."""
from datetime import UTC, datetime

FAILURE_STATUSES = {"failed", "failed_processing", "failed_saving", "failed_no_audio", "failed_exhausted"}
REQUEST_STATUSES = {"retry_requested", "rejoin_requested"}


def request(client, table, job, *, rejoin=False):
    if table not in {"bot_jobs", "audio_jobs"}:
        raise ValueError("Unsupported job type")
    if job["status"] not in FAILURE_STATUSES - {"failed_exhausted"}:
        raise ValueError("This job cannot be retried")
    if rejoin and (table != "bot_jobs" or job["status"] != "failed_no_audio"):
        raise ValueError("Only a bot job without captured audio can be sent again")
    if not rejoin and job["status"] == "failed_no_audio":
        raise ValueError("There is no saved audio to process")
    rows = client.table(table).update({
        "status": "rejoin_requested" if rejoin else "retry_requested",
        "message": "Asked to send the bot again" if rejoin else "Retry requested; waiting for the worker",
        "updated_at": datetime.now(UTC).isoformat(),
    }).eq("id", job["id"]).eq("status", job["status"]).execute().data
    if not rows:
        raise ValueError("The job has already changed. Refresh its status before trying again.")


def description(table, status):
    if status == "failed_exhausted":
        return None, "The retry limit was reached. Contact the administrator to recover retained audio manually."
    if status == "failed_no_audio":
        if table == "bot_jobs":
            return "Send bot again", "No usable audio was captured. This sends the bot into the meeting again; admit it if asked."
        return None, "No usable recording is available. Upload or record the audio again."
    if status == "failed_saving":
        return "Retry saving", "Processing finished. This retries saving the same meeting without transcribing it again."
    if status == "failed_processing":
        return "Retry processing", "The worker will reuse the retained recording and check that it is still available."
    return "Retry saved work", "The worker will check for saved notes or audio. It will not rejoin the meeting automatically."
