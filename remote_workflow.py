import json
import os
import tempfile
import wave
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path
from uuid import uuid4

import numpy as np
from dotenv import load_dotenv
from faster_whisper.audio import decode_audio

from ai_agent import (
    MIN_TRANSCRIPT_WORDS,
    analyze_meeting,
    render_markdown,
)
from meeting_history import MeetingHistory

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=PROJECT_DIR / ".env")
OUTPUT = PROJECT_DIR / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)
SAMPLE_RATE = 16000
ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4", ".webm"}


@lru_cache(maxsize=1)
def _whisper_model():
    from faster_whisper import WhisperModel

    model_name = os.getenv("WHISPER_MODEL", "base")
    return WhisperModel(model_name, device="cpu", compute_type="int8")


def _write_pcm_wav(path, samples, sample_rate):
    pcm = np.round(np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as audio_file:
        audio_file.setnchannels(1)
        audio_file.setsampwidth(2)
        audio_file.setframerate(sample_rate)
        audio_file.writeframes(pcm.tobytes())


def _write_jsonl(path, entries):
    temporary_path = path.with_suffix(path.suffix + ".tmp")
    temporary_path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    temporary_path.replace(path)


def process_uploaded_recording(file_bytes, filename, consent_confirmed):
    if not consent_confirmed:
        raise ValueError("Confirm participant consent before processing a recording.")
    if not file_bytes:
        raise ValueError("The uploaded recording is empty.")

    suffix = Path(filename).suffix.casefold()
    if suffix not in ALLOWED_AUDIO_EXTENSIONS:
        allowed = ", ".join(sorted(ALLOWED_AUDIO_EXTENSIONS))
        raise ValueError(f"Unsupported audio file type. Upload one of: {allowed}")

    OUTPUT.mkdir(parents=True, exist_ok=True)
    session_id = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
    session_id += "_" + uuid4().hex[:8]
    transcript_path = OUTPUT / f"transcript_{session_id}.jsonl"
    notes_path = OUTPUT / f"notes_{session_id}.md"
    minutes_path = OUTPUT / f"minutes_{session_id}.json"
    meeting_time = datetime.now(timezone.utc)
    history = MeetingHistory()
    history.create_meeting(
        meeting_id=session_id,
        timestamp=meeting_time.isoformat(),
        transcript_path=transcript_path,
        notes_path=notes_path,
        minutes_path=minutes_path,
        status="processing",
    )

    try:
        with tempfile.TemporaryDirectory(prefix="meet-audio-") as temp_dir:
            audio_path = Path(temp_dir) / f"upload{suffix}"
            audio_path.write_bytes(file_bytes)

            samples = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
            if samples.size == 0:
                raise ValueError("No decodable audio was found in the upload.")

            whisper = _whisper_model()
            segments, info = whisper.transcribe(
                samples,
                beam_size=3,
                vad_filter=True,
                condition_on_previous_text=False,
            )
            entries = []
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue
                start = float(segment.start)
                end = float(segment.end)
                entries.append(
                    {
                        "timestamp": (meeting_time + timedelta(seconds=start)).isoformat(),
                        "start": round(start, 3),
                        "end": round(end, 3),
                        "speaker": "Unknown",
                        "text": text,
                    }
                )

            if not entries:
                raise ValueError("No speech was detected in the uploaded recording.")

            _write_jsonl(transcript_path, entries)
            history.index_file(session_id, "transcript", transcript_path)
            history.update_status(session_id, "transcribed")

            diarization_status = "unavailable"
            hf_token = os.getenv("HF_TOKEN")
            if hf_token:
                try:
                    from diarization import diarize_transcript

                    wav_path = Path(temp_dir) / "diarization.wav"
                    _write_pcm_wav(wav_path, samples, SAMPLE_RATE)
                    diarize_transcript(wav_path, transcript_path, token=hf_token)
                    entries = [
                        json.loads(line)
                        for line in transcript_path.read_text(encoding="utf-8").splitlines()
                        if line.strip()
                    ]
                    history.index_file(session_id, "transcript", transcript_path)
                    diarization_status = "completed"
                except Exception as error:
                    diarization_status = "unavailable"
                    history.update_status(
                        session_id,
                        "diarization_unavailable",
                        error=str(error),
                    )
            else:
                history.update_status(
                    session_id,
                    "diarization_unavailable",
                    error="HF_TOKEN is not configured; speakers remain Unknown.",
                )

        conversation = "\n".join(
            f"[{index}] {entry.get('timestamp', '')} "
            f"({entry.get('start', '?')}-{entry.get('end', '?')}s) "
            f"{entry.get('speaker', 'Unknown')}: {entry.get('text', '')}"
            for index, entry in enumerate(entries)
        )
        if len(conversation.split()) < MIN_TRANSCRIPT_WORDS:
            message = "Meeting was too short to analyze. The transcript is saved; upload more audio for reliable minutes."
            history.update_status(session_id, "too_short_to_analyze", error=message)
            return {
                "status": "too_short_to_analyze",
                "meeting_id": session_id,
                "transcript_path": transcript_path,
                "error": message,
                "diarization_status": diarization_status,
            }

        history.update_status(session_id, "analyzing")
        minutes = analyze_meeting(transcript_path)
        markdown = render_markdown(minutes)
        notes_path.write_text(markdown, encoding="utf-8")
        minutes_json = json.dumps(minutes, indent=2, ensure_ascii=False)
        minutes_path.write_text(minutes_json, encoding="utf-8")

        history.index_file(session_id, "notes", notes_path)
        history.index_file(session_id, "minutes_json", minutes_path)
        history.update_status(
            session_id,
            "completed",
            notes_path=notes_path,
            minutes_path=minutes_path,
            minutes_json=minutes_json,
        )
        return {
            "status": "completed",
            "meeting_id": session_id,
            "transcript_path": transcript_path,
            "notes_path": notes_path,
            "minutes_path": minutes_path,
            "action_item_count": len(minutes.get("action_items", [])),
            "diarization_status": diarization_status,
        }
    except ValueError as error:
        if "too short" in str(error).casefold():
            history.update_status(
                session_id,
                "too_short_to_analyze",
                error=str(error),
            )
            return {
                "status": "too_short_to_analyze",
                "meeting_id": session_id,
                "transcript_path": transcript_path,
                "error": str(error),
            }
        history.update_status(session_id, "processing_failed", error=str(error))
        raise
    except Exception as error:
        history.update_status(session_id, "processing_failed", error=str(error))
        raise
