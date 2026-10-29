import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from ai_agent import MIN_TRANSCRIPT_WORDS, load_transcript
from controller import PROJECT_DIR, finish_recording, start_recording
from meeting_history import MeetingHistory

load_dotenv(dotenv_path=PROJECT_DIR / ".env")

st.set_page_config(page_title="Meeting History", layout="wide")
st.title("Meeting History")
st.caption("Local transcripts, notes, decisions, action items, and source references")


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


if "active_recording" not in st.session_state:
    st.session_state.active_recording = None
if "last_recording_result" not in st.session_state:
    st.session_state.last_recording_result = None

st.subheader("Record a meeting")
meet_url = st.text_input(
    "Google Meet URL",
    value=os.environ.get("MEET_URL", ""),
    disabled=st.session_state.active_recording is not None,
)
if meet_url.strip():
    st.link_button("Open Meet", meet_url.strip(), disabled=st.session_state.active_recording is not None)

consent_confirmed = st.checkbox(
    "I joined the meeting and confirmed participants consent to recording.",
    disabled=st.session_state.active_recording is not None,
)
device_indices = st.text_input(
    "Audio device indices",
    value=os.environ.get("AUDIO_DEVICE_INDICES", "0,1"),
    help="Comma-separated SoundCard device indexes.",
    disabled=st.session_state.active_recording is not None,
)

start_column, stop_column, session_column = st.columns([1, 1, 3])
with start_column:
    if st.button(
        "Start recording",
        type="primary",
        disabled=(
            st.session_state.active_recording is not None
            or not meet_url.strip()
            or not consent_confirmed
        ),
        use_container_width=True,
    ):
        try:
            with st.spinner("Starting local transcription..."):
                st.session_state.active_recording = start_recording(
                    meet_url.strip(),
                    consent_confirmed=consent_confirmed,
                    device_indices=device_indices.strip(),
                )
            st.session_state.last_recording_result = None
            st.success("Recording is active.")
        except Exception as error:
            st.error(f"Could not start recording: {error}")

with stop_column:
    if st.button(
        "Stop and analyze",
        type="secondary",
        disabled=st.session_state.active_recording is None,
        use_container_width=True,
    ):
        session = st.session_state.active_recording
        try:
            with st.spinner("Stopping, diarizing, and generating minutes..."):
                st.session_state.last_recording_result = finish_recording(session)
        except Exception as error:
            st.error(f"Recording stopped with an error: {error}")
        finally:
            st.session_state.active_recording = None

with session_column:
    active_session = st.session_state.active_recording
    if active_session:
        st.info(f"Recording: {active_session['meeting_id']}")
    elif st.session_state.last_recording_result:
        result = st.session_state.last_recording_result
        if result.get("status") == "completed":
            st.success(f"Completed: {result['meeting_id']}")
        elif result.get("status") == "too_short_to_analyze":
            st.warning(
                result.get("error", "Meeting was too short to analyze."),
                title="Meeting too short to analyze",
            )
        elif result.get("status") == "analysis_failed":
            st.warning(
                result.get("error", "Meeting analysis failed."),
                title="Analysis failed",
            )
        else:
            st.warning(f"Session status: {result.get('status', 'unknown').replace('_', ' ').capitalize()}")

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
        st.dataframe(table_rows, use_container_width=True, hide_index=True)

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
                st.dataframe(action_items, use_container_width=True, hide_index=True)
            else:
                st.caption("No action items captured.")

            with st.expander("Structured minutes and transcript references"):
                st.json(minutes)

        if selected["notes_path"]:
            notes_path = Path(selected["notes_path"])
            if notes_path.exists():
                with st.expander("Meeting notes", expanded=True):
                    st.markdown(notes_path.read_text(encoding="utf-8"))

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
