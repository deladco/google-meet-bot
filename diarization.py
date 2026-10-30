import json
import os
import wave
from pathlib import Path

import numpy as np

MODEL = "pyannote/speaker-diarization-community-1"


def assign_speakers(entries, turns):
    ordered_turns = sorted(turns, key=lambda turn: turn[0])
    speaker_names = {}
    for _, _, speaker in ordered_turns:
        if speaker not in speaker_names:
            speaker_names[speaker] = f"SPEAKER_{len(speaker_names):02d}"

    for entry in entries:
        try:
            start = float(entry["start"])
            end = float(entry["end"])
        except (KeyError, TypeError, ValueError):
            entry["speaker"] = "Unknown"
            continue

        overlaps = {}
        for turn_start, turn_end, speaker in ordered_turns:
            overlap = max(0.0, min(end, turn_end) - max(start, turn_start))
            if overlap:
                overlaps[speaker] = overlaps.get(speaker, 0.0) + overlap

        entry["speaker"] = (
            speaker_names[max(overlaps, key=overlaps.get)]
            if overlaps
            else "Unknown"
        )

    return entries


def _load_audio_tensor(audio_path):
    import torch

    with wave.open(str(audio_path), "rb") as audio_file:
        channels = audio_file.getnchannels()
        sample_width = audio_file.getsampwidth()
        sample_rate = audio_file.getframerate()
        frame_count = audio_file.getnframes()
        if sample_width != 2:
            raise ValueError("Expected a 16-bit PCM WAV file.")
        samples = np.frombuffer(
            audio_file.readframes(frame_count), dtype="<i2"
        ).astype(np.float32) / 32768.0

    if channels > 1:
        samples = samples.reshape(-1, channels).mean(axis=1)

    return {
        "waveform": torch.from_numpy(samples.copy()).unsqueeze(0),
        "sample_rate": sample_rate,
    }


def diarize_transcript(audio_path, transcript_path, token=None):
    audio_path = Path(audio_path)
    transcript_path = Path(transcript_path)

    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")
    if not transcript_path.exists():
        raise FileNotFoundError(f"Transcript not found: {transcript_path}")

    token = token or os.environ.get("HF_TOKEN")
    if not token:
        raise RuntimeError(
            "Set HF_TOKEN in the project .env file and accept the "
            "pyannote/speaker-diarization-community-1 model conditions."
        )

    try:
        from pyannote.audio import Pipeline
    except ImportError as error:
        raise RuntimeError(
            "pyannote.audio is not installed in the active environment."
        ) from error

    entries = [
        json.loads(line)
        for line in transcript_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not entries:
        raise ValueError("Transcript is empty.")

    pipeline = Pipeline.from_pretrained(MODEL, token=token)
    import torch

    if torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))
    output = pipeline(_load_audio_tensor(audio_path))
    annotation = getattr(
        output, "exclusive_speaker_diarization", output.speaker_diarization
    )
    turns = [
        (turn.start, turn.end, str(speaker))
        for turn, speaker in annotation
    ]
    entries = assign_speakers(entries, turns)

    temporary_path = transcript_path.with_suffix(
        transcript_path.suffix + ".tmp"
    )
    temporary_path.write_text(
        "".join(json.dumps(entry, ensure_ascii=False) + "\n" for entry in entries),
        encoding="utf-8",
    )
    temporary_path.replace(transcript_path)

    return entries
