import hashlib
import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from ai_agent import MIN_TRANSCRIPT_WORDS, load_transcript
PROJECT_DIR = Path(__file__).resolve().parent
load_dotenv(dotenv_path=PROJECT_DIR / ".env")

from meeting_history import MeetingHistory
from remote_config import (
    CLOUD_OLLAMA_HOST,
    LOCAL_OLLAMA_HOST,
    get_ollama_client,
    get_ollama_host,
    get_setting,
)
from remote_workflow import process_uploaded_recording

st.set_page_config(page_title="AI Assistant", layout="wide")
st.title("AI Assistant")
st.caption("Created by Daryna Ladanovska · Version 1.0")
st.subheader("Meeting history")


def ollama_target_label(host):
    if host.rstrip("/") == CLOUD_OLLAMA_HOST.rstrip("/"):
        return "Ollama Cloud"
    if host.rstrip("/") == LOCAL_OLLAMA_HOST.rstrip("/"):
        return "Local Ollama"
    return "Custom Ollama endpoint"


with st.sidebar:
    st.subheader("Ollama connection")
    ollama_host = get_ollama_host()
    api_key_configured = bool(get_setting("OLLAMA_API_KEY"))
    st.caption(f"Endpoint: {ollama_target_label(ollama_host)}")
    st.caption(f"API key: {'configured' if api_key_configured else 'missing'}")

    if st.button(
        "Test Ollama connection",
        disabled=not api_key_configured,
        key="test_ollama_connection",
    ):
        try:
            with st.spinner("Checking Ollama API access..."):
                get_ollama_client().list()
        except Exception as error:
            status_code = getattr(error, "status_code", None)
            if status_code == 401:
                message = "Ollama rejected the API key. Check OLLAMA_API_KEY in app secrets."
            elif status_code == 402:
                message = "Ollama reports that this account needs usage credits."
            elif status_code == 429:
                message = "Ollama rate limit reached. Try again later."
            else:
                message = (
                    f"Could not reach {ollama_target_label(ollama_host)} "
                    f"({type(error).__name__}). Check OLLAMA_HOST and network access."
                )
            st.error(message)
        else:
            st.success("Connected to Ollama API.")


def transcript_is_too_short(meeting):
    if meeting["status"] not in {"analysis_failed", "too_short_to_analyze"}:
        return False
    try:
        entries = load_transcript(Path(meeting["transcript_path"]))
    except (OSError, ValueError):
        return meeting["status"] == "too_short_to_analyze"
    transcript = "\n".join(
        f"{entry.get('timestamp', '')} | "
        f"{entry.get('speaker', 'Unknown')}: {entry.get('text', '')}"
        for entry in entries
    )
    return len(transcript.split()) < MIN_TRANSCRIPT_WORDS


def display_status(meeting):
    if transcript_is_too_short(meeting):
        return "Too short to analyze"
    return meeting["status"].replace("_", " ").capitalize()


st.subheader("Record or process a meeting")
audio_source = st.segmented_control(
    "Recording source",
    options=["Browser microphone", "Upload file"],
    default="Browser microphone",
    key="audio_source",
)
consent_confirmed = st.checkbox(
    "I have consent to record and transcribe this meeting."
)


def process_audio(audio_bytes, filename):
    with st.spinner("Transcribing, diarizing, and generating meeting minutes..."):
        return process_uploaded_recording(
            audio_bytes,
            filename,
            consent_confirmed=consent_confirmed,
        )


def show_processing_result(result):
    if result["status"] == "completed":
        st.success(f"Meeting processed: {result['meeting_id']}")
        st.caption(
            f"{result['action_item_count']} action items · "
            f"Speaker diarization: {result['diarization_status']}"
        )
    elif result["status"] == "too_short_to_analyze":
        st.warning(result["error"], title="Meeting too short to analyze")
    else:
        st.warning(
            result.get("error", f"Processing finished with status: {result['status']}"),
            title="Meeting processing failed",
        )


if audio_source == "Browser microphone":
    st.caption(
        "Use the recorder’s Start/Stop controls. This captures your browser microphone, "
        "not Meet tab or system audio."
    )
    recorded_audio = st.audio_input(
        "Start meeting recording",
        sample_rate=16000,
        disabled=not consent_confirmed,
        key="meeting_microphone_recording",
    )
    if recorded_audio is None:
        st.session_state.pop("processed_browser_audio_hash", None)
    else:
        audio_bytes = recorded_audio.getvalue()
        audio_hash = hashlib.sha256(audio_bytes).hexdigest()
        if audio_hash != st.session_state.get("processed_browser_audio_hash"):
            try:
                result = process_audio(audio_bytes, "browser-recording.wav")
                st.session_state["processed_browser_audio_hash"] = audio_hash
                st.session_state["last_recording_result"] = result
            except Exception as error:
                st.error(f"Meeting processing failed: {error}")
        if st.session_state.get("last_recording_result"):
            show_processing_result(st.session_state["last_recording_result"])
else:
    with st.form("meeting_upload", clear_on_submit=False):
        uploaded_audio = st.file_uploader(
            "Meeting audio or video",
            type=["wav", "mp3", "m4a", "flac", "ogg", "mp4", "webm"],
            max_upload_size=512,
            help="Maximum 512 MB. The uploaded file is processed temporarily and is not retained.",
        )
        process_recording = st.form_submit_button(
            "Transcribe and analyze",
            type="primary",
            disabled=uploaded_audio is None or not consent_confirmed,
            width="stretch",
        )

    if process_recording and uploaded_audio is not None:
        try:
            result = process_audio(uploaded_audio.getvalue(), uploaded_audio.name)
            show_processing_result(result)
        except Exception as error:
            st.error(f"Meeting processing failed: {error}")

st.divider()

history = MeetingHistory()
meetings = history.list_meetings(limit=500)
statuses = sorted({meeting["status"] for meeting in meetings})

with st.sidebar:
    st.header("Filters")
    selected_status = st.selectbox("Status", ["All", *statuses])
    st.divider()
    st.caption("Natural-language questions use matching local history and Ollama.")

filtered_meetings = [
    meeting
    for meeting in meetings
    if selected_status == "All" or meeting["status"] == selected_status
]
completed_count = sum(meeting["status"] == "completed" for meeting in meetings)

metric_columns = st.columns(3)
metric_columns[0].metric("Meetings", len(meetings))
metric_columns[1].metric("Completed", completed_count)
metric_columns[2].metric("Needs attention", len(meetings) - completed_count)

list_tab, search_tab, ask_tab = st.tabs(["Meetings", "Search", "Ask"])

with list_tab:
    if not filtered_meetings:
        st.info("No meetings match this status yet.")
    else:
        table_rows = [
            {
                "Timestamp": meeting["timestamp"],
                "Meeting ID": meeting["meeting_id"],
                "Status": display_status(meeting),
                "Notes": "Available" if meeting["notes_path"] else "Pending",
            }
            for meeting in filtered_meetings
        ]
        st.dataframe(table_rows, width="stretch", hide_index=True)

        selected_id = st.selectbox(
            "Meeting details",
            [meeting["meeting_id"] for meeting in filtered_meetings],
        )
        selected = next(
            meeting for meeting in filtered_meetings
            if meeting["meeting_id"] == selected_id
        )

        left, right = st.columns(2)
        left.markdown(f"**Timestamp**  \n{selected['timestamp']}")
        selected_too_short = transcript_is_too_short(selected)
        right.markdown(f"**Status**  \n{display_status(selected)}")
        st.caption(f"Transcript: {selected['transcript_path']}")
        if selected["error"]:
            if selected_too_short:
                st.warning(
                    "The transcript is saved, but it contains too little speech "
                    "for reliable minutes. Record more audio and try again.",
                    title="Meeting too short to analyze",
                )
            else:
                st.warning(selected["error"])

        if selected["minutes_json"]:
            minutes = json.loads(selected["minutes_json"])
            st.subheader("Action items")
            action_items = minutes.get("action_items", [])
            if action_items:
                st.dataframe(action_items, width="stretch", hide_index=True)
            else:
                st.caption("No action items captured.")

            with st.expander("Structured minutes and transcript references"):
                st.json(minutes)

        if selected["notes_path"]:
            notes_path = Path(selected["notes_path"])
            if notes_path.exists():
                notes_markdown = notes_path.read_text(encoding="utf-8")
                with st.expander("Meeting notes", expanded=True):
                    st.markdown(notes_markdown)
                download_columns = st.columns(2)
                download_columns[0].download_button(
                    "Download notes (.md)",
                    data=notes_markdown,
                    file_name=f"meeting_notes_{selected['meeting_id']}.md",
                    mime="text/markdown",
                    key=f"download_notes_{selected['meeting_id']}",
                )
                if selected["minutes_json"]:
                    download_columns[1].download_button(
                        "Download structured minutes (.json)",
                        data=selected["minutes_json"],
                        file_name=f"meeting_minutes_{selected['meeting_id']}.json",
                        mime="application/json",
                        key=f"download_minutes_{selected['meeting_id']}",
                    )

with search_tab:
    query = st.text_input("Search transcripts, notes, owners, or deadlines")
    if st.button("Search", key="search_button", disabled=not query.strip()):
        try:
            results = history.search(query, limit=30)
        except Exception as error:
            st.error(f"Search failed: {error}")
        else:
            if not results:
                st.info("No matching meeting history found.")
            for result in results:
                with st.container(border=True):
                    st.markdown(
                        f"**{result['timestamp']}** · `{result['meeting_id']}` · "
                        f"{result['kind']} · {result['status']}"
                    )
                    st.write(result["excerpt"])

with ask_tab:
    question = st.text_input(
        "Ask about decisions, assigned tasks, deadlines, or open questions",
        placeholder="Which tasks were assigned last week?",
    )
    if st.button("Ask", key="ask_button", disabled=not question.strip()):
        try:
            with st.spinner("Searching meeting history and asking Ollama..."):
                answer = history.answer(question, limit=5)
        except Exception as error:
            st.error(f"Could not answer from local history: {error}")
        else:
            st.markdown(answer)
