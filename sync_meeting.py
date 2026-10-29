"""Upload local meetings (transcript, notes, minutes) to Supabase.

Usage:  python sync_meeting.py
Needs SUPABASE_URL and SUPABASE_ANON_KEY in .env, plus the SYNC_EMAIL and
SYNC_PASSWORD of your Supabase user (or you are prompted).
"""
import getpass
import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(dotenv_path=Path(__file__).resolve().parent / ".env")

import auth_db
from meeting_history import MeetingHistory


def _read(path):
    if not path:
        return None
    path = Path(path)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def sync(limit=500):
    if not auth_db.is_configured():
        raise SystemExit("Set SUPABASE_URL and SUPABASE_ANON_KEY in .env first.")
    email = os.environ.get("SYNC_EMAIL") or input("Supabase email: ")
    password = os.environ.get("SYNC_PASSWORD") or getpass.getpass("Password: ")

    client = auth_db.create_client()
    auth_db.sign_in(client, email, password)

    synced = 0
    for meeting in MeetingHistory().list_meetings(limit=limit):
        transcript = _read(meeting["transcript_path"])
        if transcript is None:
            continue
        minutes = json.loads(meeting["minutes_json"]) if meeting["minutes_json"] else None
        auth_db.upsert_meeting(
            client,
            {
                "meeting_id": meeting["meeting_id"],
                "recorded_at": meeting["timestamp"],
                "status": meeting["status"],
                "transcript": transcript,
                "notes": _read(meeting["notes_path"]),
                "minutes": minutes,
            },
        )
        synced += 1
    auth_db.sign_out(client)
    print(f"Synced {synced} meeting(s) to Supabase.")


if __name__ == "__main__":
    sync()
