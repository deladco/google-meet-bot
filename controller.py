import os
import signal
import subprocess
import sys
import time
import webbrowser
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from meeting_history import MeetingHistory

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=PROJECT_DIR / ".env")

MEET_URL = os.environ.get("MEET_URL")
OUTPUT = PROJECT_DIR / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)


def stop_process(process):
    if process.poll() is not None:
        return

    try:
        if sys.platform == "win32":
            process.send_signal(signal.CTRL_BREAK_EVENT)
        else:
            process.send_signal(signal.SIGINT)
    except (OSError, ValueError):
        process.terminate()

    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def classify_analysis_failure(returncode, stdout="", stderr=""):
    output = f"{stdout}\n{stderr}"
    if "Transcript is too short for reliable meeting notes" in output:
        return (
            "too_short_to_analyze",
            "Meeting was too short to analyze. The transcript is saved; record "
            "more audio before generating minutes.",
        )

    detail = next(
        (line.strip() for line in reversed(output.splitlines()) if line.strip()),
        f"Analyzer exited with code {returncode}.",
    )
    return "analysis_failed", detail


def start_recording(meet_url, consent_confirmed, device_indices=None):
    if not meet_url:
        raise ValueError("Enter a Google Meet URL before starting.")
    if not consent_confirmed:
        raise ValueError("Confirm that participants consented before recording.")

    session_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    transcript_path = OUTPUT / f"transcript_{session_id}.jsonl"
    notes_path = OUTPUT / f"notes_{session_id}.md"
    minutes_path = OUTPUT / f"minutes_{session_id}.json"
    ready_path = OUTPUT / f".transcriber_ready_{session_id}.tmp"
    history = MeetingHistory()
    history.create_meeting(
        meeting_id=session_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        transcript_path=transcript_path,
        notes_path=notes_path,
        minutes_path=minutes_path,
        status="starting",
    )

    environment = os.environ.copy()
    environment["SESSION_TRANSCRIPT_PATH"] = str(transcript_path)
    environment["TRANSCRIPTION_READY_PATH"] = str(ready_path)
    if device_indices:
        environment["AUDIO_DEVICE_INDICES"] = str(device_indices)

    flags = (
        subprocess.CREATE_NEW_PROCESS_GROUP
        if sys.platform == "win32"
        else 0
    )
    process = None
    try:
        process = subprocess.Popen(
            [sys.executable, "-u", str(PROJECT_DIR / "live_transcriber.py")],
            cwd=PROJECT_DIR,
            env=environment,
            creationflags=flags,
        )
        ready_deadline = time.monotonic() + 300
        while not ready_path.exists():
            if process.poll() is not None:
                raise RuntimeError(
                    "Transcriber exited before the audio devices were ready."
                )
            if time.monotonic() >= ready_deadline:
                raise TimeoutError("Transcriber did not become ready in time.")
            time.sleep(0.25)

    except Exception as error:
        if process is not None:
            stop_process(process)
        ready_path.unlink(missing_ok=True)
        history.update_status(
            session_id,
            "transcription_failed",
            error=str(error),
        )
        raise

    history.update_status(session_id, "recording")
    return {
        "meeting_id": session_id,
        "process": process,
        "environment": environment,
        "transcript_path": transcript_path,
        "notes_path": notes_path,
        "minutes_path": minutes_path,
        "ready_path": ready_path,
    }


def finish_recording(session):
    history = MeetingHistory()
    meeting_id = session["meeting_id"]
    process = session["process"]
    transcript_path = session["transcript_path"]
    notes_path = session["notes_path"]
    minutes_path = session["minutes_path"]
    environment = session["environment"]

    try:
        stop_process(process)
    finally:
        session["ready_path"].unlink(missing_ok=True)

    if process.returncode not in (0, None):
        error = f"Transcriber exited with code {process.returncode}."
        history.update_status(meeting_id, "transcription_failed", error=error)
        return {"status": "transcription_failed", "error": error}

    if not transcript_path.exists():
        history.update_status(meeting_id, "no_transcript")
        return {"status": "no_transcript"}

    if transcript_path.stat().st_size == 0:
        history.update_status(meeting_id, "empty_transcript")
        return {"status": "empty_transcript"}

    history.index_file(meeting_id, "transcript", transcript_path)
    history.update_status(meeting_id, "transcribed")

    audio_path = transcript_path.with_suffix(".wav")
    try:
        from diarization import diarize_transcript

        diarize_transcript(audio_path, transcript_path)
        history.index_file(meeting_id, "transcript", transcript_path)
    except Exception as error:
        print(f"Speaker diarization unavailable: {error}")
        history.update_status(meeting_id, "diarization_unavailable", error=str(error))

    history.update_status(meeting_id, "analyzing")
    result = subprocess.run(
        [
            sys.executable,
            str(PROJECT_DIR / "ai_agent.py"),
            str(transcript_path),
            "--output",
            str(notes_path),
            "--json-output",
            str(minutes_path),
        ],
        cwd=PROJECT_DIR,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    if result.returncode != 0:
        status, error = classify_analysis_failure(
            result.returncode,
            result.stdout,
            result.stderr,
        )
        history.update_status(meeting_id, status, error=error)
        return {"status": status, "error": error}

    if not notes_path.exists() or not minutes_path.exists():
        error = "Analyzer returned success without creating notes and JSON minutes."
        history.update_status(meeting_id, "analysis_failed", error=error)
        return {"status": "analysis_failed", "error": error}

    history.index_file(meeting_id, "notes", notes_path)
    history.index_file(meeting_id, "minutes_json", minutes_path)
    history.update_status(
        meeting_id,
        "completed",
        notes_path=notes_path,
        minutes_path=minutes_path,
        minutes_json=minutes_path.read_text(encoding="utf-8"),
    )
    return {
        "status": "completed",
        "meeting_id": meeting_id,
        "transcript_path": transcript_path,
        "notes_path": notes_path,
        "minutes_path": minutes_path,
    }


def run_meeting():
    if not MEET_URL:
        raise RuntimeError("Set MEET_URL in the project .env file.")

    print("Opening Google Meet...")
    webbrowser.open(MEET_URL)
    input("Join the meeting and confirm consent, then press Enter to start...")
    session = start_recording(
        MEET_URL,
        consent_confirmed=True,
        device_indices=os.environ.get("AUDIO_DEVICE_INDICES"),
    )
    print("Transcription is active. Press Enter when the meeting ends...")
    try:
        input()
    except KeyboardInterrupt:
        print("\nStop requested.")

    print("Stopping transcription and processing the meeting...")
    result = finish_recording(session)
    if result["status"] == "completed":
        print(f"Meeting completed: {result['notes_path']}")
    else:
        print(f"Meeting finished with status: {result['status']}")
        if result.get("error"):
            print(result["error"])


if __name__ == "__main__":
    run_meeting()
