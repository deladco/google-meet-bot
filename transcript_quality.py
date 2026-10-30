import difflib
import json
import os
from pathlib import Path

import numpy as np

PROJECT_DIR = Path(__file__).resolve().parent
BATCH_SIZE = 40


LEARNED_FILE = PROJECT_DIR / "vocabulary_learned.txt"
CANDIDATES_FILE = PROJECT_DIR / "vocabulary_candidates.json"
PROMOTE_AFTER = 2
MAX_LEARNED = 150
MAX_HOTWORDS = 60


def _read_terms(path):
    if not path.is_file():
        return []
    lines = (line.strip() for line in path.read_text(encoding="utf-8").splitlines())
    return [line for line in lines if line and not line.startswith("#")]


def vocabulary():
    """Terms Whisper should spell correctly: WHISPER_VOCABULARY, vocabulary.txt, then auto-learned terms."""
    terms = [t.strip() for t in os.getenv("WHISPER_VOCABULARY", "").split(",")]
    terms += _read_terms(PROJECT_DIR / "vocabulary.txt")
    learned = _read_terms(LEARNED_FILE)[-MAX_HOTWORDS:]
    seen = set()
    result = []
    for term in terms + learned:
        if term and term.casefold() not in seen:
            seen.add(term.casefold())
            result.append(term)
    return result


def learn_vocabulary(entries, meeting_id):
    """Extract names/products/jargon from a transcript and remember them for future recordings.

    Safeguards against learning recognizer mistakes: a term must literally occur in the transcript, be at most
    two words, and be seen in two different meetings before it is promoted into vocabulary_learned.txt.
    Returns the list of newly promoted terms.
    """
    if os.getenv("VOCABULARY_LEARNING", "1") == "0" or not entries:
        return []
    try:
        from remote_config import get_ollama_client, get_ollama_model

        client, model = get_ollama_client(), get_ollama_model()
    except Exception:
        return []

    texts = [e.get("text", "") for e in entries]
    full_text = "\n".join(texts).casefold()
    known = {t.casefold() for t in vocabulary()}
    system = (
        "From a meeting transcript (English, Ukrainian, Russian, often mixed) list proper names of people, "
        "companies, products, tools, and technical terms or acronyms that a speech recognizer could misspell. "
        "Skip ordinary words and phrases. Each term is one or two words, copied exactly as written in the transcript. "
        "Return JSON: {\"terms\":[\"...\"]}."
    )
    seen_terms = {}
    for start in range(0, len(texts), 120):
        chunk = "\n".join(texts[start : start + 120])
        try:
            response = client.chat(
                model=model,
                format="json",
                think="low",
                options={"temperature": 0},
                messages=[{"role": "system", "content": system}, {"role": "user", "content": chunk}],
            )
            terms = json.loads(response.message.content).get("terms", [])
        except Exception:
            continue
        for term in terms if isinstance(terms, list) else []:
            if not isinstance(term, str):
                continue
            term = term.strip()
            key = term.casefold()
            if (
                2 <= len(term) <= 40
                and len(term.split()) <= 2
                and any(ch.isalpha() for ch in term)
                and key in full_text
                and key not in known
            ):
                seen_terms[key] = term

    candidates = {}
    if CANDIDATES_FILE.is_file():
        try:
            candidates = json.loads(CANDIDATES_FILE.read_text(encoding="utf-8"))
        except ValueError:
            candidates = {}
    promoted = []
    for key, term in seen_terms.items():
        entry = candidates.setdefault(key, {"term": term, "meetings": []})
        if meeting_id not in entry["meetings"]:
            entry["meetings"].append(meeting_id)
        if len(entry["meetings"]) >= PROMOTE_AFTER:
            promoted.append(entry["term"])
            del candidates[key]
    CANDIDATES_FILE.write_text(json.dumps(candidates, ensure_ascii=False, indent=1), encoding="utf-8")
    if promoted:
        merged = (_read_terms(LEARNED_FILE) + promoted)[-MAX_LEARNED:]
        header = "# Auto-learned from transcripts. Safe to edit; move good terms to vocabulary.txt, delete bad ones.\n"
        LEARNED_FILE.write_text(header + "\n".join(merged) + "\n", encoding="utf-8")
    return promoted


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
