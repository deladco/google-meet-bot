import difflib
import json
import os
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent
BATCH_SIZE = 40


def vocabulary():
    """Names/terms Whisper should spell correctly: WHISPER_VOCABULARY (comma-separated) and vocabulary.txt."""
    terms = [t.strip() for t in os.getenv("WHISPER_VOCABULARY", "").split(",")]
    path = PROJECT_DIR / "vocabulary.txt"
    if path.is_file():
        terms += [line.strip() for line in path.read_text(encoding="utf-8").splitlines()]
    seen = set()
    return [t for t in terms if t and not t.startswith("#") and not (t in seen or seen.add(t))]


def clean_audio(samples):
    """Remove rumble below speech range and, if installed, stationary background noise."""
    if os.getenv("AUDIO_CLEANUP", "1") == "0" or samples.size < 16000:
        return samples
    from scipy.signal import butter, sosfilt

    cleaned = sosfilt(butter(4, 80, btype="highpass", fs=16000, output="sos"), samples)
    try:
        import noisereduce

        cleaned = noisereduce.reduce_noise(y=cleaned, sr=16000, stationary=True, prop_decrease=0.8)
    except ImportError:
        pass
    return np.asarray(cleaned, dtype=np.float32)


def _similar(a, b):
    return difflib.SequenceMatcher(None, a.casefold(), b.casefold()).ratio()


def _correct_batch(client, model, batch, terms, context):
    lines = "\n".join(f"{i}|{e['text']}" for i, e in batch)
    system = (
        "You fix speech-recognition errors in a meeting transcript (English, Ukrainian, Russian, often mixed). "
        "Correct only clear mistakes: misspelled names and technical terms, wrong word forms, missing punctuation. "
        "Keep each line in its original language and wording; never translate, summarize, add or remove content. "
        "If a line is fine or you are unsure, omit it. Return JSON: {\"fixes\":[{\"i\":<line number>,\"text\":\"<corrected text>\"}]}."
    )
    if terms:
        system += " Known names/terms (use exact spelling): " + ", ".join(terms) + "."
    user = (f"Previous lines for context:\n{context}\n\n" if context else "") + "Lines to check:\n" + lines
    response = client.chat(
        model=model,
        format="json",
        think="low",
        options={"temperature": 0},
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    return json.loads(response.message.content).get("fixes", [])


def correct_transcript(entries):
    """LLM second pass. Returns (entries, number_changed); on any failure the original entries are kept."""
    if os.getenv("TRANSCRIPT_LLM_CORRECTION", "1") == "0" or not entries:
        return entries, 0
    try:
        from remote_config import get_ollama_client, get_ollama_model

        client, model = get_ollama_client(), get_ollama_model()
    except Exception:
        return entries, 0

    terms = vocabulary()
    corrected = [dict(e) for e in entries]
    changed = 0
    for start in range(0, len(corrected), BATCH_SIZE):
        batch = list(enumerate(corrected))[start : start + BATCH_SIZE]
        context = "\n".join(e["text"] for e in corrected[max(0, start - 3) : start])
        try:
            fixes = _correct_batch(client, model, batch, terms, context)
        except Exception:
            continue
        for fix in fixes if isinstance(fixes, list) else []:
            index, text = fix.get("i"), fix.get("text")
            if not isinstance(index, int) or not isinstance(text, str) or not 0 <= index < len(corrected):
                continue
            original = corrected[index]["text"]
            text = text.strip()
            # Reject rewrites, translations and truncations: only near-identical text is accepted.
            if text and text != original and _similar(original, text) >= 0.7 and 0.7 <= len(text) / len(original) <= 1.4:
                corrected[index].setdefault("raw_text", original)
                corrected[index]["text"] = text
                changed += 1
    return corrected, changed
