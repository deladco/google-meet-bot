"""Re-process a saved recording and add it to a user's dashboard.

  python retranscribe.py [path\\to\\recording.wav] [--job JOB_ID]

Defaults to output/bot_last_recording.wav and the owner of the newest bot job.
"""
import argparse
from pathlib import Path

import auth_db
import bot_worker
import meeting_bot
from meeting_history import MeetingHistory
from remote_workflow import process_uploaded_recording


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("wav", nargs="?", default=str(bot_worker.PROJECT_DIR / "output" / "bot_last_recording.wav"))
    parser.add_argument("--job", help="bot job id whose owner receives the meeting (default: newest job)")
    args = parser.parse_args()

    db = bot_worker.service_client()
    query = db.table(meeting_bot.JOBS_TABLE).select("*").order("created_at", desc=True)
    if args.job:
        query = query.eq("id", args.job)
    jobs = query.limit(1).execute().data
    if not jobs:
        raise SystemExit("No bot job found to take the owner from.")
    job = jobs[0]
    print(f"Owner: {job.get('user_email')}  (job {job['id']}, {job.get('meeting_url')})")

    result = process_uploaded_recording(
        Path(args.wav).read_bytes(),
        "bot-recording.wav",
        consent_confirmed=True,
        owner_id=job["user_id"],
        owner_email=job.get("user_email"),
        progress=print,
    )
    saved = next(
        (m for m in MeetingHistory().list_meetings(limit=50) if m["meeting_id"] == result["meeting_id"]),
        None,
    )
    if not saved:
        raise SystemExit("Processed, but the meeting was not found in history.")
    auth_db.save_meeting_from_history(
        db, saved, owner_email=job.get("user_email"), user_id=job["user_id"]
    )
    print("Added to the dashboard:", result["meeting_id"], result.get("error") or "")


if __name__ == "__main__":
    main()
