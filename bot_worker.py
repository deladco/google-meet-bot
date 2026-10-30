"""Self-hosted meeting bot worker.

Run on a computer or server (not Streamlit Cloud):  python bot_worker.py

It polls the Supabase `bot_jobs` table, joins each queued meeting as a guest with
Chromium (Playwright), records the meeting audio, transcribes it with the normal
pipeline and saves the result to `synced_meetings` for the user who queued it.

Needs in .env: SUPABASE_URL (or NEXT_PUBLIC_SUPABASE_URL) and SUPABASE_SERVICE_KEY
(secret service-role key - keep it only on the worker, never in the app).
Audio capture: Windows uses WASAPI loopback (default speakers); Linux needs
PulseAudio and ffmpeg (see Dockerfile.worker).
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import wave
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=PROJECT_DIR / ".env")

import auth_db
import meeting_bot
from meeting_history import MeetingHistory
from remote_workflow import process_uploaded_recording

SAMPLE_RATE = 16000
POLL_SECONDS = 10
AUDIO_TABLE = "audio_jobs"
AUDIO_BUCKET = "recordings"
MAX_MINUTES = int(os.environ.get("BOT_MAX_MINUTES", "180"))
LOBBY_MINUTES = int(os.environ.get("BOT_LOBBY_MINUTES", "10"))
HEADLESS = os.environ.get("BOT_HEADLESS", "0" if sys.platform == "win32" else "1") == "1"

ENDED_TEXTS = (
    "You've been removed from the meeting",
    "You left the meeting",
    "The meeting has ended",
    "This meeting has been ended",
    "Meeting ended",
    "You were removed",
    "removed from this meeting",
    "Return to home screen",
)


def service_client():
    key = os.environ.get("SUPABASE_SERVICE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not key:
        raise SystemExit("Set SUPABASE_SERVICE_KEY in .env (Supabase > Settings > API > service_role).")
    from supabase import create_client

    return create_client(auth_db._url(), key)


def set_job(db, job_id, **fields):
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    db.table(meeting_bot.JOBS_TABLE).update(fields).eq("id", job_id).execute()


def set_audio_job(db, job_id, **fields):
    fields["updated_at"] = datetime.now(timezone.utc).isoformat()
    db.table(AUDIO_TABLE).update(fields).eq("id", job_id).execute()


def job_status(db, job_id):
    row = get_row(db, job_id)
    return row["status"] if row else "cancelled"


def get_row(db, job_id):
    rows = db.table(meeting_bot.JOBS_TABLE).select("*").eq("id", job_id).limit(1).execute().data
    return rows[0] if rows else None


class Recorder:
    """Records whatever the machine is playing (the meeting's audio) to a 16 kHz mono WAV."""

    def __init__(self, path):
        self.path = Path(path)
        self._stop = threading.Event()
        self._thread = None
        self._process = None

    def start(self):
        if sys.platform == "win32":
            self._thread = threading.Thread(target=self._record_loopback, daemon=True)
            self._thread.start()
        else:
            if not shutil.which("ffmpeg"):
                raise RuntimeError("ffmpeg is required on Linux for audio capture.")
            source = os.environ.get("BOT_PULSE_SOURCE", "default")
            self._process = subprocess.Popen(
                ["ffmpeg", "-y", "-loglevel", "error", "-f", "pulse", "-i", source,
                 "-ac", "1", "-ar", str(SAMPLE_RATE), str(self.path)],
                stdin=subprocess.PIPE,
            )

    def _record_loopback(self):
        import numpy as np
        import soundcard as sc

        speaker = sc.default_speaker()
        microphone = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        with wave.open(str(self.path), "wb") as out:
            out.setnchannels(1)
            out.setsampwidth(2)
            out.setframerate(SAMPLE_RATE)
            with microphone.recorder(samplerate=SAMPLE_RATE, channels=1) as source:
                while not self._stop.is_set():
                    block = source.record(numframes=SAMPLE_RATE // 2)
                    pcm = (np.clip(block[:, 0], -1, 1) * 32767).astype("<i2")
                    out.writeframes(pcm.tobytes())

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=15)
        if self._process:
            try:
                self._process.stdin.write(b"q")
                self._process.stdin.flush()
            except Exception:
                pass
            try:
                self._process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                self._process.terminate()


def _click_first(page, labels, timeout=1500):
    for label in labels:
        for locator in (
            page.get_by_role("button", name=label),
            page.get_by_text(label, exact=False),
        ):
            try:
                locator.first.click(timeout=timeout)
                return True
            except Exception:
                continue
    return False


def _fill_name(page, name):
    """Fills the guest name box and confirms the value stuck (Meet re-renders this page)."""
    for locator in (
        page.get_by_placeholder("Your name"),
        page.get_by_placeholder("Enter your name"),
        page.get_by_label("Your name"),
        page.locator("input[type=text]"),
    ):
        try:
            field = locator.first
            field.wait_for(state="visible", timeout=6000)
            field.fill(name, timeout=4000)
            if field.input_value(timeout=1000) == name:
                return True
        except Exception:
            continue
    return False


def _zoom_web_url(url):
    return url.replace("/j/", "/wc/join/", 1) if "/wc/" not in url else url


def join(page, url, name):
    platform = meeting_bot.platform_of(url)
    page.goto(_zoom_web_url(url) if platform == "zoom" else url, wait_until="domcontentloaded")

    if platform == "teams":
        page.wait_for_timeout(4000)
        _click_first(page, ["Continue on this browser", "Join on the web instead"], 6000)
        page.wait_for_timeout(3000)
    # Fill the name as soon as the box exists, then again after the page settles.
    _fill_name(page, name)
    # No camera or microphone for the bot.
    _click_first(page, ["Continue without microphone and camera", "Continue without audio or video"], 800)
    page.wait_for_timeout(1500)
    if not _fill_name(page, name) and platform == "meet":
        print("Could not fill the bot name on the join page", flush=True)
    if platform == "zoom":
        _click_first(page, ["Join Audio by Computer", "Join"], 3000)
    if not _click_first(page, ["Ask to join", "Join now", "Join"], 4000):
        if HEADLESS:
            raise RuntimeError(
                "Could not find the join button. The meeting may require a signed-in account "
                "or the host blocks anonymous guests."
            )
        return False
    return True


def in_meeting(page):
    """True once the bot is admitted: a leave control is visible and the lobby/join screens are gone."""
    if in_lobby(page):
        return False
    for label in ("Ask to join", "Join now", "Join"):
        try:
            if page.get_by_role("button", name=label, exact=True).first.is_visible(timeout=200):
                return False
        except Exception:
            continue
    for label in ("Leave call", "Hang up"):
        try:
            if page.get_by_role("button", name=label).first.is_visible(timeout=300):
                return True
        except Exception:
            continue
    return False


def meeting_over(page):
    try:
        body = page.inner_text("body", timeout=1500)
    except Exception:
        return page.is_closed()
    return any(text.lower() in body.lower() for text in ENDED_TEXTS)


DENIED_TEXTS = (
    "You can't join this video call",
    "You can’t join this video call",
    "You aren't allowed to join",
    "Your request to join was denied",
    "No one responded to your request",
    "denied your request",
)
LOBBY_TEXTS = ("Asking to be let in", "Someone will let you in", "Waiting for the host", "waiting room")
STATE_FILE = Path(os.environ.get("BOT_STATE_FILE") or PROJECT_DIR / "bot_google_state.json")
CHANNEL = os.environ.get("BOT_BROWSER_CHANNEL") or None


def page_text(page):
    try:
        return page.inner_text("body", timeout=1500)
    except Exception:
        return ""


def signed_in():
    """True only if the saved browser state holds a real Google session, not just a leftover file."""
    try:
        cookies = json.loads(STATE_FILE.read_text(encoding="utf-8")).get("cookies", [])
    except Exception:
        return False
    return any(c.get("name") in ("SID", "SAPISID", "__Secure-1PSID") for c in cookies)


def denied_reason(page):
    body = page_text(page).lower().replace("’", "'")
    if not any(text.lower().replace("’", "'") in body for text in DENIED_TEXTS):
        return None
    if "no one responded" in body:
        return "Nobody in the meeting admitted the bot in time. Keep the meeting open and admit 'AI Assistant' when it asks to join."
    if "denied" in body:
        return "The host denied the bot's request to join. Send the bot again and admit it from the meeting."
    if not signed_in():
        return (
            "The meeting does not allow anonymous guests and the bot is not signed in to Google. "
            "Run `python bot_login.py` to sign it in, then invite that Google account to the meeting."
        )
    return (
        "The meeting only allows invited accounts. Invite the bot's Google account to the "
        "calendar event or add it as a participant, or ask the host to allow everyone with the link."
    )


def in_lobby(page):
    body = page_text(page).lower()
    return any(text.lower() in body for text in LOBBY_TEXTS)


def record_meeting(db, job, wav_path):
    from playwright.sync_api import sync_playwright

    recorder = Recorder(wav_path)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=HEADLESS,
            channel=CHANNEL,
            ignore_default_args=["--enable-automation"],
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                "--disable-blink-features=AutomationControlled",
            ],
        )
        context = browser.new_context(
            viewport={"width": 1280, "height": 800},
            storage_state=str(STATE_FILE) if STATE_FILE.exists() else None,
        )
        page = context.new_page()
        try:
            clicked = join(page, job["meeting_url"], job["bot_name"])
            lobby_deadline = time.time() + LOBBY_MINUTES * 60
            last_message = None
            if not clicked:
                set_job(
                    db, job["id"],
                    message="Could not press Join automatically - sign in / join manually in the bot's browser window",
                )
                last_message = "manual"
            while not in_meeting(page):
                reason = denied_reason(page)
                if reason:
                    raise RuntimeError(reason)
                if time.time() > lobby_deadline:
                    raise RuntimeError("The bot was not admitted to the meeting in time.")
                if job_status(db, job["id"]) == "stop_requested":
                    return False
                message = (
                    "Waiting in the lobby - admit the bot from the meeting"
                    if in_lobby(page)
                    else "Opening the meeting page"
                )
                if last_message != "manual" and message != last_message:
                    set_job(db, job["id"], message=message)
                    last_message = message
                time.sleep(3)

            try:
                context.storage_state(path=str(STATE_FILE))  # keep the bot's login for next time
            except Exception:
                pass

            recorder.start()
            print("Admitted - recording started", flush=True)
            set_job(db, job["id"], status="recording", message=None)
            deadline = time.time() + MAX_MINUTES * 60
            started = time.time()
            while time.time() < deadline:
                if job_status(db, job["id"]) == "stop_requested":
                    print("Stop requested from the app after %ds" % (time.time() - started), flush=True)
                    break
                if time.time() - started > 20 and meeting_over(page):
                    print("Meeting ended after %ds: %s" % (time.time() - started, page_text(page)[:120].replace("\n", " | ")), flush=True)
                    break
                time.sleep(3)
            return True
        finally:
            recorder.stop()
            try:
                _click_first(page, ["Leave call", "Leave", "Hang up"], 1000)
            except Exception:
                pass
            browser.close()


def run_job(db, job):
    set_job(db, job["id"], status="joining", message=None)
    with tempfile.TemporaryDirectory() as temp_dir:
        wav_path = Path(temp_dir) / "meeting.wav"
        if not record_meeting(db, job, wav_path):
            set_job(db, job["id"], status="cancelled")
            return
        if not wav_path.exists() or wav_path.stat().st_size < 10_000:
            raise RuntimeError("No audio was captured from the meeting.")
        keep = PROJECT_DIR / "output" / "bot_last_recording.wav"
        keep.parent.mkdir(exist_ok=True)
        shutil.copyfile(wav_path, keep)  # listen to this if a meeting comes back empty

        set_job(db, job["id"], status="processing")
        current = {"status": "processing"}

        def on_progress(message):
            # Writing a step to the job lets the app show transcribing vs summarizing.
            status = "summarizing" if message.startswith(("Writing", "Saving")) else "transcribing"
            if status != current["status"]:
                current["status"] = status
                set_job(db, job["id"], status=status, message=None)

        result = process_uploaded_recording(
            wav_path.read_bytes(),
            "bot-recording.wav",
            consent_confirmed=True,
            owner_id=job["user_id"],
            owner_email=job.get("user_email"),
            progress=on_progress,
        )

    saved = next(
        (m for m in MeetingHistory().list_meetings(limit=50) if m["meeting_id"] == result["meeting_id"]),
        None,
    )
    if not saved:
        raise RuntimeError("The meeting was processed but could not be found in history.")
    auth_db.save_meeting_from_history(
        db, saved, owner_email=job.get("user_email"), user_id=job["user_id"]
    )
    set_job(db, job["id"], status="done", meeting_id=result["meeting_id"],
            message=result.get("error"))


def convert_to_wav(source, target):
    """Phone recordings are usually .m4a/.caf; decode them with PyAV to 16 kHz mono WAV."""
    import numpy as np
    from faster_whisper.audio import decode_audio

    samples = decode_audio(str(source), sampling_rate=SAMPLE_RATE)
    pcm = (np.clip(samples, -1, 1) * 32767).astype("<i2")
    with wave.open(str(target), "wb") as out:
        out.setnchannels(1)
        out.setsampwidth(2)
        out.setframerate(SAMPLE_RATE)
        out.writeframes(pcm.tobytes())


def run_audio_job(db, job):
    """Transcribe a recording uploaded from the mobile app."""
    set_audio_job(db, job["id"], status="processing")
    with tempfile.TemporaryDirectory() as temp_dir:
        raw_path = Path(temp_dir) / "upload"
        raw_path.write_bytes(db.storage.from_(AUDIO_BUCKET).download(job["storage_path"]))
        wav_path = Path(temp_dir) / "recording.wav"
        convert_to_wav(raw_path, wav_path)
        if wav_path.stat().st_size < 10_000:
            raise RuntimeError("The recording is empty or too short.")
        result = process_uploaded_recording(
            wav_path.read_bytes(),
            "mobile-recording.wav",
            consent_confirmed=True,
            owner_id=job["user_id"],
            owner_email=job.get("user_email"),
            language=job.get("language") or None,
        )
    saved = next(
        (m for m in MeetingHistory().list_meetings(limit=50) if m["meeting_id"] == result["meeting_id"]),
        None,
    )
    if not saved:
        raise RuntimeError("The recording was processed but could not be found in history.")
    auth_db.save_meeting_from_history(db, saved, owner_email=job.get("user_email"), user_id=job["user_id"])
    set_audio_job(db, job["id"], status="done", meeting_id=result["meeting_id"], message=result.get("error"))
    try:
        db.storage.from_(AUDIO_BUCKET).remove([job["storage_path"]])  # keep no audio after transcription
    except Exception:
        pass


def poll_audio_jobs(db):
    try:
        jobs = (
            db.table(AUDIO_TABLE).select("*").eq("status", "queued").order("created_at").limit(1).execute().data
        )
    except Exception:
        return False  # table not created yet
    if not jobs:
        return False
    job = jobs[0]
    print("Starting audio job", job["id"])
    try:
        run_audio_job(db, job)
    except Exception as error:
        print("Audio job failed:", error)
        set_audio_job(db, job["id"], status="failed", message=str(error)[:500])
    return True


def main():
    db = service_client()
    print(f"Bot worker running (headless={HEADLESS}). Waiting for jobs...")
    while True:
        if poll_audio_jobs(db):
            continue
        try:
            jobs = (
                db.table(meeting_bot.JOBS_TABLE)
                .select("*")
                .eq("status", "queued")
                .order("created_at")
                .limit(1)
                .execute()
                .data
            )
        except Exception as error:
            print("Could not read jobs:", error)
            jobs = []
        if not jobs:
            time.sleep(POLL_SECONDS)
            continue
        job = jobs[0]
        print("Starting job", job["id"], job["meeting_url"])
        try:
            run_job(db, job)
        except Exception as error:
            print("Job failed:", error)
            set_job(db, job["id"], status="failed", message=str(error)[:500])


if __name__ == "__main__":
    main()
