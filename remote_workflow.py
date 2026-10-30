import gc
import json
import os
import subprocess
import sys
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
from remote_config import get_setting
from transcript_quality import clean_audio, correct_transcript, learn_vocabulary, vocabulary

PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=PROJECT_DIR / ".env")
OUTPUT = PROJECT_DIR / "output"
OUTPUT.mkdir(parents=True, exist_ok=True)
SAMPLE_RATE = 16000
ALLOWED_AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".mp4", ".webm"}


_FORCE_CPU = False


def _add_cuda_dll_dirs():
    # pip-installed CUDA 12 cuBLAS/cuDNN (nvidia-*-cu12) are not on PATH by default on Windows.
    if os.name != "nt":
        return
    for base in map(Path, sys.path):
        nvidia = base / "nvidia"
        if nvidia.is_dir():
            for bin_dir in nvidia.glob("*/bin"):
                os.add_dll_directory(str(bin_dir))
                os.environ["PATH"] = str(bin_dir) + os.pathsep + os.environ["PATH"]
            return


@lru_cache(maxsize=1)
def _whisper_model():
    from faster_whisper import WhisperModel

    import ctranslate2

    _add_cuda_dll_dirs()
    # GPU: large-v3-turbo is far more accurate for Ukrainian/Russian than base. CPU hosts keep a smaller model.
    has_gpu = ctranslate2.get_cuda_device_count() > 0 and not _FORCE_CPU
    device = "cpu" if _FORCE_CPU else (os.getenv("WHISPER_DEVICE") or ("cuda" if has_gpu else "cpu"))
    default_model = "large-v3-turbo" if device == "cuda" else "small"
    model_name = os.getenv("WHISPER_MODEL") or default_model
    compute_type = os.getenv("WHISPER_COMPUTE_TYPE") or ("float16" if device == "cuda" else "int8")
    if _FORCE_CPU:
        compute_type = "int8"
    return WhisperModel(model_name, device=device, compute_type=compute_type)


def _release_whisper():
    # Free the speech model before diarization/analysis to stay under the host's RAM limit.
    _whisper_model.cache_clear()
    gc.collect()


def _diarize_in_subprocess(wav_path, transcript_path, token, timeout=900):
    # Separate process: if the host kills it for memory, the app itself survives.
    code = (
        "import os, sys\n"
        "from diarization import diarize_transcript\n"
        "diarize_transcript(sys.argv[1], sys.argv[2], token=os.environ['HF_TOKEN'])\n"
    )
    try:
        result = subprocess.run(
            [sys.executable, "-c", code, str(wav_path), str(transcript_path)],
            cwd=PROJECT_DIR,
            env={**os.environ, "HF_TOKEN": token},
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("Speaker identification timed out; speakers remain Unknown.") from error
    if result.returncode != 0:
        if result.returncode in (-9, 137):
            reason = "server ran out of memory"
        else:
            lines = result.stderr.strip().splitlines()
            reason = lines[-1] if lines else f"exit code {result.returncode}"
        raise RuntimeError(f"Speaker identification failed ({reason}); speakers remain Unknown.")


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


def _normalize_level(samples):
    peak = float(np.abs(samples).max()) if samples.size else 0.0
    if 0.005 < peak < 0.5:
        gain = min(0.9 / peak, 10.0)
        return np.clip(samples * gain, -1.0, 1.0).astype(np.float32)
    return samples


def _is_hallucination(segment):
    if getattr(segment, "no_speech_prob", 0.0) > 0.6:
        return True
    if getattr(segment, "avg_logprob", 0.0) < -1.0:
        return True
    return getattr(segment, "compression_ratio", 0.0) > 2.4


def _drop_repeated_loops(entries):
    # Whisper often loops one phrase on noise; keep a phrase at most twice.
    seen = {}
    kept = []
    for entry in entries:
        key = "".join(ch for ch in entry["text"].casefold() if ch.isalnum())
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= 2:
            kept.append(entry)
    return kept


def _transcribe_samples(whisper, samples, meeting_time, vad_filter):
    global _FORCE_CPU
    try:
        return _transcribe_once(whisper, samples, meeting_time, vad_filter)
    except RuntimeError as error:
        if _FORCE_CPU or not any(k in str(error).casefold() for k in ("cublas", "cudnn", "cuda")):
            raise
        # Broken/missing GPU runtime: retry on CPU instead of failing the meeting.
        _FORCE_CPU = True
        _whisper_model.cache_clear()
        return _transcribe_once(_whisper_model(), samples, meeting_time, vad_filter)


def _transcribe_once(whisper, samples, meeting_time, vad_filter):
    language = os.getenv("WHISPER_LANGUAGE") or None
    terms = vocabulary()
    segments, _ = whisper.transcribe(
        _normalize_level(samples),
        beam_size=5,
        hotwords=", ".join(terms) or None,
        language=language,
        # Without a fixed language, detect it per segment so mixed English/Ukrainian/Russian speech works.
        multilingual=language is None,
        vad_filter=vad_filter,
        vad_parameters={"min_silence_duration_ms": 500, "speech_pad_ms": 300},
        condition_on_previous_text=False,
        no_speech_threshold=0.6,
        log_prob_threshold=-1.0,
        compression_ratio_threshold=2.4,
    )
    entries = []
    for segment in segments:
        text = segment.text.strip()
        if not text or _is_hallucination(segment):
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
    return _drop_repeated_loops(entries)


def process_uploaded_recording(
    file_bytes, filename, consent_confirmed, owner_id=None, owner_email=None
):
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
        owner_id=owner_id,
        owner_email=owner_email,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="meet-audio-") as temp_dir:
            audio_path = Path(temp_dir) / f"upload{suffix}"
            audio_path.write_bytes(file_bytes)

            samples = decode_audio(str(audio_path), sampling_rate=SAMPLE_RATE)
            if samples.size == 0:
                raise ValueError("No decodable audio was found in the upload.")

            whisper = _whisper_model()
            diarization_samples = samples
            samples = clean_audio(samples)
            entries = _transcribe_samples(
                whisper,
                samples,
                meeting_time,
                vad_filter=True,
            )
            if not entries:
                entries = _transcribe_samples(
                    whisper,
                    samples,
                    meeting_time,
                    vad_filter=False,
                )

            if not entries:
                duration = samples.size / SAMPLE_RATE
                peak = float(abs(samples).max())
                if peak < 0.01:
                    hint = (
                        "The audio is silent or nearly silent. Check that the "
                        "recording captured the meeting's audio (mic or system sound)."
                    )
                else:
                    hint = (
                        "Audio was present but no words were recognized. Check that "
                        "the speech is audible and in a supported language."
                    )
                message = (
                    f"No speech was detected in the uploaded recording "
                    f"({duration:.1f}s, peak level {peak:.3f}). {hint}"
                )
                transcript_path.write_text("", encoding="utf-8")
                history.index_file(session_id, "transcript", transcript_path)
                history.update_status(session_id, "no_speech_detected", error=message)
                return {
                    "status": "no_speech_detected",
                    "meeting_id": session_id,
                    "transcript_path": transcript_path,
                    "error": message,
                }

            _release_whisper()
            _write_jsonl(transcript_path, entries)
            history.index_file(session_id, "transcript", transcript_path)
            history.update_status(session_id, "transcribed")

            diarization_status = "unavailable"
            hf_token = get_setting("HF_TOKEN")
            if hf_token:
                try:
                    wav_path = Path(temp_dir) / "diarization.wav"
                    _write_pcm_wav(wav_path, diarization_samples, SAMPLE_RATE)
                    _diarize_in_subprocess(wav_path, transcript_path, hf_token)
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

        history.update_status(session_id, "correcting")
        entries, corrected_count = correct_transcript(entries)
        if corrected_count:
            _write_jsonl(transcript_path, entries)
            history.index_file(session_id, "transcript", transcript_path)

        learn_vocabulary(entries, session_id)

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
