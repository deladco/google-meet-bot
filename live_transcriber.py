import json
import os
import signal
import wave
from contextlib import ExitStack
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import soundcard as sc
from dotenv import load_dotenv
from scipy.signal import resample_poly
from faster_whisper import WhisperModel

# Audio configuration
SAMPLE_RATE = 48000
WHISPER_RATE = 16000
CHUNK_SECONDS = 5
DEFAULT_DEVICE_INDICES = (0, 1)
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=PROJECT_DIR / ".env")

OUTPUT = PROJECT_DIR / "output"
OUTPUT.mkdir(exist_ok=True)

configured_transcript = Path(
    os.getenv("SESSION_TRANSCRIPT_PATH", str(OUTPUT / "transcript.jsonl"))
)
TRANSCRIPT_FILE = (
    configured_transcript
    if configured_transcript.is_absolute()
    else PROJECT_DIR / configured_transcript
)
TRANSCRIPT_FILE.parent.mkdir(parents=True, exist_ok=True)


def parse_device_indices(selection, device_count):
    if not selection:
        indices = list(DEFAULT_DEVICE_INDICES)
    else:
        try:
            indices = [int(value.strip()) for value in selection.split(",")]
        except ValueError as error:
            raise ValueError("Enter device numbers separated by commas.") from error

    if not indices or len(indices) != len(set(indices)):
        raise ValueError("Device numbers must be unique.")

    if any(index < 0 or index >= device_count for index in indices):
        raise ValueError(
            f"Choose device numbers between 0 and {device_count - 1}."
        )

    return indices


def list_devices():
    devices = sc.all_microphones(include_loopback=True)

    print("\nAvailable recording devices:")
    for index, device in enumerate(devices):
        print(f"{index}: {device.name}")

    return devices


def select_microphones(selection=None):
    devices = list_devices()

    if not devices:
        raise RuntimeError("No audio devices found")

    if selection is None:
        selection = os.environ.get("AUDIO_DEVICE_INDICES")

    device_indices = parse_device_indices(selection, len(devices))

    return [devices[index] for index in device_indices]


def transcribe_live(
    microphones=None,
    stop_event=None,
    started_event=None,
    transcript_file=TRANSCRIPT_FILE,
    audio_file=None,
):
    if microphones is None:
        microphones = select_microphones()

    transcript_file = Path(transcript_file)
    audio_file = Path(audio_file) if audio_file else transcript_file.with_suffix(".wav")
    transcript_file.parent.mkdir(parents=True, exist_ok=True)
    audio_file.parent.mkdir(parents=True, exist_ok=True)

    print("\nUsing: " + ", ".join(microphone.name for microphone in microphones))
    print("Loading Whisper model...")

    model = WhisperModel(
        "base",
        device="cpu",
        compute_type="int8"
    )

    print("Live transcription started.")
    print("Press Ctrl+C to stop.\n")

    audio_offset = 0.0
    with ExitStack() as stack:
        recorders = [
            stack.enter_context(
                microphone.recorder(samplerate=SAMPLE_RATE)
            )
            for microphone in microphones
        ]
        with wave.open(str(audio_file), "wb") as audio_writer:
            audio_writer.setnchannels(1)
            audio_writer.setsampwidth(2)
            audio_writer.setframerate(SAMPLE_RATE)

            ready_path = os.environ.get("TRANSCRIPTION_READY_PATH")
            if ready_path:
                Path(ready_path).write_text("ready", encoding="utf-8")

            if started_event is not None:
                started_event.set()

            with ThreadPoolExecutor(max_workers=len(recorders)) as executor:
                try:
                    while stop_event is None or not stop_event.is_set():
                        futures = [
                            executor.submit(
                                recorder.record,
                                numframes=SAMPLE_RATE * CHUNK_SECONDS
                            )
                            for recorder in recorders
                        ]
                        audio_chunks = [future.result() for future in futures]
                        if stop_event is not None and stop_event.is_set():
                            break

                        mono_chunks = [
                            np.mean(chunk, axis=1) if chunk.ndim == 2 else chunk
                            for chunk in audio_chunks
                        ]
                        audio = np.mean(np.stack(mono_chunks), axis=0)
                        chunk_start = audio_offset
                        audio_offset += len(audio) / SAMPLE_RATE

                        pcm = np.round(
                            np.clip(audio, -1.0, 1.0) * 32767
                        ).astype("<i2")
                        audio_writer.writeframes(pcm.tobytes())

                        rms = np.sqrt(
                            np.mean(audio.astype(np.float64) ** 2)
                        )
                        if rms < 0.002:
                            continue

                        audio_16k = resample_poly(
                            audio, 1, 3
                        ).astype(np.float32)

                        segments, info = model.transcribe(
                            audio_16k,
                            beam_size=3,
                            vad_filter=True,
                            condition_on_previous_text=False
                        )

                        timestamp = datetime.now(
                            timezone.utc
                        ).isoformat()

                        for segment in segments:
                            text = segment.text.strip()
                            if not text:
                                continue

                            record = {
                                "timestamp": timestamp,
                                "start": round(chunk_start + segment.start, 3),
                                "end": round(chunk_start + segment.end, 3),
                                "speaker": "Unknown",
                                "text": text
                            }

                            print(f"[{timestamp}] {text}")
                            with transcript_file.open("a", encoding="utf-8") as file:
                                file.write(
                                    json.dumps(record, ensure_ascii=False) + "\n"
                                )
                except KeyboardInterrupt:
                    pass

    print("\nTranscription stopped.")
    print(f"Saved: {transcript_file}")
    print(f"Audio saved: {audio_file}")


if __name__ == "__main__":
    if hasattr(signal, "SIGBREAK"):
        signal.signal(signal.SIGBREAK, signal.default_int_handler)
    transcribe_live()
