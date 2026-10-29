import hashlib
import hmac
import json
import os
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import auth_db
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
import meeting_bot
from tab_recorder import tab_recorder

st.set_page_config(page_title="AI Assistant", layout="wide")


def require_password():
    expected = next(
        (
            value
            for name in ("APP_PASSWORD", "Password", "PASSWORD")
            if (value := get_setting(name))
        ),
        None,
    )
    if st.session_state.get("authenticated"):
        return
    st.title("AI Assistant")
    if not expected:
        st.error(
            "Access is locked: no password is configured. Set `Password` in .env "
            "or in the Streamlit app secrets."
        )
        st.stop()
    with st.form("login"):
        entered = st.text_input("Password", type="password")
        submitted = st.form_submit_button("Unlock", type="primary")
    if submitted:
        if hmac.compare_digest(entered.encode(), expected.encode()):
            st.session_state["authenticated"] = True
            st.rerun()
        st.error("Incorrect password.")
    st.stop()


def require_login():
    if st.session_state.get("supabase_user"):
        return
    st.title("AI Assistant")
    client = auth_db.new_client()
    sign_in_tab, sign_up_tab = st.tabs(["Sign in", "Create account"])
    with sign_in_tab, st.form("supabase_sign_in"):
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        if st.form_submit_button("Sign in", type="primary"):
            try:
                auth_db.save_login(auth_db.sign_in(client, email, password))
            except Exception as error:
                st.error(f"Sign in failed: {error}")
            else:
                st.rerun()
    with sign_up_tab, st.form("supabase_sign_up"):
        new_email = st.text_input("Email", key="signup_email")
        new_password = st.text_input("Password (min 6 characters)", type="password", key="signup_password")
        if st.form_submit_button("Create account"):
            try:
                response = auth_db.sign_up(client, new_email, new_password)
            except Exception as error:
                st.error(f"Sign up failed: {error}")
            else:
                if response.session:
                    auth_db.save_login(response)
                    st.rerun()
                st.success("Account created. Confirm the email we sent, then sign in.")
    st.stop()


if auth_db.is_configured():
    require_login()
else:
    require_password()
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
    if st.session_state.get("supabase_user"):
        st.caption(f"Signed in as {st.session_state['supabase_user']}")
    if st.button("Sign out" if st.session_state.get("supabase_user") else "Lock app"):
        auth_db.sign_out()
        st.session_state.pop("authenticated", None)
        st.rerun()
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
    options=[
        "Send AI bot to a meeting",
        "Browser microphone",
        "Tabs / system audio + microphone",
        "Upload file",
    ],
    default="Browser microphone",
    key="audio_source",
)
consent_confirmed = st.checkbox(
    "I have consent to record and transcribe this meeting."
)


def current_viewer():
    """Returns (owner_id, owner_email, can_see_all) for the signed-in Supabase user."""
    if not auth_db.is_configured():
        return None, None, True
    email = st.session_state.get("supabase_user")
    admin_email = (get_setting("ADMIN_EMAIL") or "").strip().casefold()
    is_admin = bool(admin_email) and (email or "").casefold() == admin_email
    return st.session_state.get("user_id"), email, is_admin


viewer_id, viewer_email, viewer_sees_all = current_viewer()
scope_owner_id = None if viewer_sees_all else viewer_id

if auth_db.is_configured():
    with st.sidebar:
        total_on_server = len(MeetingHistory().list_meetings(limit=100000))
        if viewer_sees_all:
            st.caption(
                f"Admin view: all users' meetings. Saved copies are in the Cloud tab "
                f"({total_on_server} on this server's temporary disk)."
            )
        else:
            st.caption(
                f"Showing only your meetings. {total_on_server} on server in total. "
                "If you are the admin, set ADMIN_EMAIL to your sign-in email "
                f"(signed in as {viewer_email})."
            )


def process_audio(audio_bytes, filename):
    with st.spinner("Transcribing, diarizing, and generating meeting minutes..."):
        result = process_uploaded_recording(
            audio_bytes,
            filename,
            consent_confirmed=consent_confirmed,
            owner_id=viewer_id,
            owner_email=viewer_email,
        )
    save_to_cloud(result)
    return result


def save_to_cloud(result):
    """Copy the finished meeting to Supabase; the server disk is wiped on redeploy."""
    if not (auth_db.is_configured() and st.session_state.get("supabase_user")):
        return
    try:
        saved = next(
            (
                meeting
                for meeting in MeetingHistory().list_meetings(limit=50)
                if meeting["meeting_id"] == result.get("meeting_id")
            ),
            None,
        )
        if saved:
            auth_db.save_meeting_from_history(
                auth_db.client_for_user(), saved, owner_email=viewer_email
            )
            result["saved_to_cloud"] = True
    except Exception as error:
        st.warning(f"Could not save this meeting to the cloud: {error}")


def show_saved_transcript(result):
    transcript_path = Path(result.get("transcript_path", ""))
    if not transcript_path.is_file():
        return
    st.caption(f"Transcript saved: {transcript_path}")
    transcript_text = transcript_path.read_text(encoding="utf-8")
    if transcript_text.strip():
        with st.expander("Transcript", expanded=result["status"] != "completed"):
            try:
                for entry in load_transcript(transcript_path):
                    st.write(f"**{entry.get('speaker', 'Unknown')}**: {entry.get('text', '')}")
            except Exception:
                st.code(transcript_text)
        st.download_button(
            "Download transcript (.jsonl)",
            data=transcript_text,
            file_name=f"transcript_{result['meeting_id']}.jsonl",
            mime="application/jsonl",
            key=f"download_transcript_{result['meeting_id']}",
        )


def show_processing_result(result):
    show_processing_result_body(result)
    show_saved_transcript(result)


def show_processing_result_body(result):
    if result["status"] == "completed":
        st.success(f"Meeting processed: {result['meeting_id']}")
        st.caption(
            f"{result['action_item_count']} action items · "
            f"Speaker diarization: {result['diarization_status']}"
        )
        notes_path = Path(result["notes_path"])
        if notes_path.exists():
            st.download_button(
                "Download notes (.md)",
                data=notes_path.read_text(encoding="utf-8"),
                file_name=f"meeting_notes_{result['meeting_id']}.md",
                mime="text/markdown",
                key=f"download_completed_notes_{result['meeting_id']}",
            )
        minutes_path = Path(result["minutes_path"])
        if minutes_path.exists():
            st.download_button(
                "Download structured minutes (.json)",
                data=minutes_path.read_text(encoding="utf-8"),
                file_name=f"meeting_minutes_{result['meeting_id']}.json",
                mime="application/json",
                key=f"download_completed_minutes_{result['meeting_id']}",
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
    if not consent_confirmed:
        st.info("Confirm recording consent above to enable the microphone recorder.")
        st.session_state.pop("processed_browser_audio_hash", None)
    else:
        recorded_audio = st.audio_input(
            "Record from microphone",
            sample_rate=16000,
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
elif audio_source == "Send AI bot to a meeting":
    st.caption(
        "The AI assistant joins your Google Meet, Zoom or Microsoft Teams meeting as a "
        "participant named below, records it, and then transcribes it. Admit it from the "
        "lobby if asked. Everyone in the meeting will see the bot."
    )
    if not (meeting_bot.is_configured() and st.session_state.get("supabase_user")):
        st.info("Sign in with Supabase to send the bot to a meeting.")
    else:
        bot_url = st.text_input("Meeting link", placeholder="https://meet.google.com/abc-defg-hij")
        bot_name = st.text_input("Bot name", value="AI Assistant")
        if st.button(
            "Send bot to meeting",
            type="primary",
            disabled=not (consent_confirmed and bot_url.strip()),
        ):
            try:
                st.session_state["active_bot_id"] = meeting_bot.join_meeting(
                    auth_db.client_for_user(), bot_url, bot_name, viewer_email
                )
            except Exception as error:
                st.error(f"Could not queue the bot: {error}")
        if not consent_confirmed:
            st.info("Confirm recording consent above to send the bot.")

        bot_id = st.session_state.get("active_bot_id")
        if bot_id:
            try:
                db = auth_db.client_for_user()
                job = meeting_bot.get_job(db, bot_id)
                code = job["status"] if job else "cancelled"
                st.write(f"**Bot status:** {meeting_bot.describe_status(code)}")
                if code == "queued":
                    st.caption(
                        "The bot worker (`python bot_worker.py`) must be running on a computer "
                        "or server to pick this up."
                    )
                if job and job.get("message"):
                    st.caption(job["message"])
                if code in meeting_bot.FINISHED:
                    st.success("Finished. Open the Cloud tab to read the notes and transcript.")
                elif code not in meeting_bot.FAILED:
                    left_col, right_col = st.columns(2)
                    left_col.button("Refresh status")
                    if right_col.button("Make the bot leave now"):
                        meeting_bot.leave_meeting(db, bot_id)
                        st.rerun()
            except Exception as error:
                st.error(f"Bot error: {error}")
elif audio_source == "Tabs / system audio + microphone":
    st.caption(
        "Records the other participants together with your microphone. Click Start, then in the "
        "browser prompt either pick the Meet tab and tick **Share tab audio**, or pick "
        "**Entire screen** and tick **Share system audio** to capture audio from all tabs "
        "and apps (Chrome/Edge on Windows or ChromeOS; Mac can only share a single tab's audio). "
        "Allow the microphone, and click Stop when the meeting ends."
    )
    if not consent_confirmed:
        st.info("Confirm recording consent above to enable the recorder.")
    tab_audio = tab_recorder(enabled=consent_confirmed, key="meeting_tab_recorder")
    if tab_audio is not None and consent_confirmed:
        tab_bytes, tab_mime = tab_audio
        tab_hash = hashlib.sha256(tab_bytes).hexdigest()
        if tab_hash != st.session_state.get("processed_tab_audio_hash"):
            try:
                extension = "ogg" if "ogg" in tab_mime else "webm"
                result = process_audio(tab_bytes, f"tab-recording.{extension}")
                st.session_state["processed_tab_audio_hash"] = tab_hash
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
meetings = history.list_meetings(limit=500, owner_id=scope_owner_id)
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

list_tab, search_tab, ask_tab, cloud_tab = st.tabs(
    ["Meetings", "Search", "Ask", "Cloud"]
)

with cloud_tab:
    if not st.session_state.get("supabase_user"):
        st.info("Sign in with Supabase to see meetings synced from your computer.")
    else:
        st.caption(
            "New recordings are saved here automatically and survive redeploys. "
            "Run `python sync_meeting.py` to upload older local meetings."
        )
        try:
            cloud_meetings = auth_db.list_meetings(auth_db.client_for_user())
        except PermissionError:
            auth_db.sign_out()
            st.rerun()
        except Exception as error:
            st.error(f"Could not load cloud meetings: {error}")
            cloud_meetings = []
        if not cloud_meetings:
            st.info("No synced meetings yet.")
        for item in cloud_meetings:
            by = f" · {item['owner_email']}" if viewer_sees_all and item.get("owner_email") else ""
            with st.expander(f"{item['recorded_at']} · {item['meeting_id']} · {item['status']}{by}"):
                if item.get("notes"):
                    st.markdown(item["notes"])
                    st.download_button(
                        "Download notes (.md)",
                        data=item["notes"],
                        file_name=f"meeting_notes_{item['meeting_id']}.md",
                        mime="text/markdown",
                        key=f"cloud_notes_{item['meeting_id']}",
                    )
                if item.get("transcript"):
                    st.download_button(
                        "Download transcript (.jsonl)",
                        data=item["transcript"],
                        file_name=f"transcript_{item['meeting_id']}.jsonl",
                        mime="application/jsonl",
                        key=f"cloud_transcript_{item['meeting_id']}",
                    )

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
                **(
                    {"Recorded by": meeting["owner_email"] or "Unknown (before accounts)"}
                    if viewer_sees_all and auth_db.is_configured()
                    else {}
                ),
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
        if viewer_sees_all and auth_db.is_configured():
            left.caption(
                f"Recorded by: {selected['owner_email'] or 'Unknown (before accounts)'}"
            )
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

        selected_transcript_path = Path(selected["transcript_path"])
        if selected_transcript_path.is_file():
            raw_transcript = selected_transcript_path.read_text(encoding="utf-8")
            if raw_transcript.strip():
                try:
                    transcript_lines = [
                        f"[{entry.get('timestamp', '')}] "
                        f"{entry.get('speaker', 'Unknown')}: {entry.get('text', '')}"
                        for entry in load_transcript(selected_transcript_path)
                    ]
                    readable_transcript = "\n".join(transcript_lines)
                except Exception:
                    readable_transcript = raw_transcript
                transcript_columns = st.columns(2)
                transcript_columns[0].download_button(
                    "Download transcript (.txt)",
                    data=readable_transcript,
                    file_name=f"transcript_{selected['meeting_id']}.txt",
                    mime="text/plain",
                    key=f"download_transcript_txt_{selected['meeting_id']}",
                )
                transcript_columns[1].download_button(
                    "Download transcript (.jsonl)",
                    data=raw_transcript,
                    file_name=f"transcript_{selected['meeting_id']}.jsonl",
                    mime="application/jsonl",
                    key=f"download_transcript_jsonl_{selected['meeting_id']}",
                )
            else:
                st.caption("No speech was captured, so the transcript is empty.")
        else:
            st.caption("Transcript file is no longer available on the server.")

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
            results = history.search(query, limit=30, owner_id=scope_owner_id)
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
                answer = history.answer(question, limit=5, owner_id=scope_owner_id)
        except Exception as error:
            st.error(f"Could not answer from local history: {error}")
        else:
            st.markdown(answer)
