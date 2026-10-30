import csv
import hashlib
import hmac
import io
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

import auth_db
from ai_agent import load_transcript
from remote_config import (
    CLOUD_OLLAMA_HOST,
    LOCAL_OLLAMA_HOST,
    get_ollama_client,
    get_ollama_host,
    get_setting,
)
from meeting_history import MeetingHistory
from remote_workflow import process_uploaded_recording
import meeting_bot
import rag
from tab_recorder import tab_recorder
from transcript_quality import LEARNED_FILE, vocabulary

PROJECT_DIR = Path(__file__).resolve().parent
VOCABULARY_FILE = PROJECT_DIR / "vocabulary.txt"
LANGUAGES = {"Auto-detect": None, "English": "en", "Ukrainian": "uk", "Russian": "ru"}
LOGO_FILE = PROJECT_DIR / "assets" / "logo.png"
MEETINGS_CACHE_SECONDS = 45

load_dotenv(dotenv_path=PROJECT_DIR / ".env")
st.set_page_config(
    page_title="Meet AI!",
    page_icon=str(Path(__file__).resolve().parent / "assets" / "favicon.png"),
    layout="wide",
)


# ---------------------------------------------------------------- access control


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
    intro_column, form_column = st.columns([3, 2], gap="large")
    with intro_column:
        landing_intro()
    with form_column:
        if not expected:
            st.error(
                "Access is locked: no password is configured. Set `Password` in .env "
                "or in the Streamlit app secrets."
            )
        else:
            with st.form("login"):
                entered = st.text_input("Password", type="password")
                submitted = st.form_submit_button("Unlock", type="primary")
            if submitted:
                if hmac.compare_digest(entered.encode(), expected.encode()):
                    st.session_state["authenticated"] = True
                    st.rerun()
                st.error("Incorrect password.")
    landing_details()
    st.stop()


LANDING_FEATURES = [
    (":material/smart_toy:", "Joins for you", "Send the bot to a Google Meet, Zoom or Teams call, or record and upload audio yourself."),
    (":material/translate:", "English, Ukrainian, Russian", "Speech is transcribed in the language spoken, even when people switch mid-meeting."),
    (":material/groups:", "Knows who spoke", "Every line is attributed to a speaker, so the notes read like a real conversation."),
    (":material/task_alt:", "Summary and action items", "Get decisions, tasks with owners and deadlines, and open questions in minutes."),
    (":material/chat:", "Ask AI", "Ask \"What did we decide about the launch?\" and get answers with sources from your own meetings."),
    (":material/lock:", "Private by default", "Your meetings are visible only to your account, and recording needs explicit consent."),
]
LANDING_STEPS = [
    ("1. Add a meeting", "Paste a meeting link for the bot, or upload a recording."),
    ("2. Let it work", "It transcribes, identifies speakers and writes the summary."),
    ("3. Follow up", "Review decisions and tasks, or just ask the AI."),
]


def landing_intro():
    logo_column, name_column = st.columns([1, 5], vertical_alignment="center")
    logo_column.image(str(LOGO_FILE), width=110)
    name_column.markdown("# Meet AI!")
    st.markdown("### Turn every meeting into clear notes, decisions and tasks.")
    st.write(
        "An AI assistant that joins your calls, writes down what was said in "
        "English, Ukrainian or Russian, and answers questions about it later."
    )


def landing_details():
    st.divider()
    st.subheader("What it does")
    for row_start in range(0, len(LANDING_FEATURES), 3):
        for column, (icon, title, text) in zip(st.columns(3), LANDING_FEATURES[row_start : row_start + 3]):
            with column.container(border=True, height=190):
                st.markdown(f"#### {icon} {title}")
                st.caption(text)
    st.subheader("How it works")
    for column, (title, text) in zip(st.columns(3), LANDING_STEPS):
        column.markdown(f"**{title}**")
        column.caption(text)
    st.caption("Create a free account above to try it. Recording requires the consent of everyone in the meeting.")


def require_login():
    if st.session_state.get("supabase_user"):
        return
    client = auth_db.new_client()
    intro_column, form_column = st.columns([3, 2], gap="large")
    with intro_column:
        landing_intro()
    with form_column:
        sign_in_tab, sign_up_tab = st.tabs(["Sign in", "Create account"])
        _login_forms(client, sign_in_tab, sign_up_tab)
    landing_details()
    st.stop()


def _login_forms(client, sign_in_tab, sign_up_tab):
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


if auth_db.is_configured():
    require_login()
else:
    require_password()


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
history = MeetingHistory()


# ---------------------------------------------------------------- meeting data


def parse_when(value):
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone()


def when_label(value):
    moment = parse_when(value)
    if moment is None:
        return str(value)[:16]
    today = datetime.now().astimezone().date()
    if moment.date() == today:
        return "Today"
    if moment.date() == today - timedelta(days=1):
        return "Yesterday"
    return moment.strftime("%d %b")


def when_full(value):
    moment = parse_when(value)
    return moment.strftime("%d %b %Y, %H:%M") if moment else str(value)


def read_text(path):
    path = Path(path) if path else None
    try:
        return path.read_text(encoding="utf-8") if path and path.is_file() else ""
    except OSError:
        return ""


def parse_json(value):
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value) if value else None
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def parse_transcript(text):
    entries = []
    for line in (text or "").splitlines():
        if line.strip():
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def normalize_cloud(row):
    return {
        "id": row["meeting_id"],
        "recorded_at": row.get("recorded_at") or "",
        "status": row.get("status") or "",
        "notes": row.get("notes") or "",
        "transcript": row.get("transcript") or "",
        "minutes": parse_json(row.get("minutes")),
        "owner_email": row.get("owner_email"),
        "error": None,
        "source": "cloud",
    }


def normalize_local(meeting):
    return {
        "id": meeting["meeting_id"],
        "recorded_at": meeting["timestamp"],
        "status": meeting["status"],
        "notes": read_text(meeting["notes_path"]),
        "transcript": read_text(meeting["transcript_path"]),
        "minutes": parse_json(meeting["minutes_json"]),
        "owner_email": meeting.get("owner_email"),
        "error": meeting.get("error"),
        "source": "local",
    }


def load_meetings(force=False):
    """Cloud meetings plus any local-only ones, newest first, cached briefly per session."""
    cached = st.session_state.get("meetings_cache")
    if cached and not force and time.time() - cached[0] < MEETINGS_CACHE_SECONDS:
        return cached[1]
    items, seen = [], set()
    if auth_db.is_configured() and st.session_state.get("supabase_user"):
        try:
            rows = auth_db.list_meetings(auth_db.client_for_user())
        except PermissionError:
            auth_db.sign_out()
            st.rerun()
        except Exception as error:
            st.warning(f"Could not load cloud meetings: {error}")
            rows = []
        for row in rows:
            items.append(normalize_cloud(row))
            seen.add(row["meeting_id"])
    for meeting in history.list_meetings(limit=500, owner_id=scope_owner_id):
        if meeting["meeting_id"] not in seen:
            items.append(normalize_local(meeting))
    items.sort(
        key=lambda item: (parse_when(item["recorded_at"]) or datetime.min.replace(tzinfo=timezone.utc)),
        reverse=True,
    )
    st.session_state["meetings_cache"] = (time.time(), items)
    return items


def refresh_meetings():
    st.session_state.pop("meetings_cache", None)


def minutes_list(meeting, key):
    minutes = meeting["minutes"] or {}
    value = minutes.get(key)
    return value if isinstance(value, list) else []


def meeting_title(meeting):
    topics = minutes_list(meeting, "discussion_topics")
    if topics and topics[0].get("topic"):
        return str(topics[0]["topic"])
    return f"Meeting · {when_full(meeting['recorded_at'])}"


def meeting_duration_hours(meeting):
    ends = [float(e["end"]) for e in parse_transcript(meeting["transcript"]) if isinstance(e.get("end"), (int, float))]
    return max(ends) / 3600 if ends else 0.0


def status_label(meeting):
    return (meeting["status"] or "unknown").replace("_", " ").capitalize()


def open_meeting(meeting_id):
    st.session_state["open_meeting"] = meeting_id
    st.switch_page(PAGE_MEETINGS)


# ---------------------------------------------------------------- recording flow


def process_audio(audio_bytes, filename, consent):
    language = LANGUAGES.get(st.session_state.get("transcription_language", "Auto-detect"))
    with st.status("Processing your recording", expanded=True) as status:
        def progress(message):
            status.update(label=message)
            st.write(f":material/check: {message}")

        result = process_uploaded_recording(
            audio_bytes,
            filename,
            consent_confirmed=consent,
            owner_id=viewer_id,
            owner_email=viewer_email,
            language=language,
            progress=progress,
        )
        if auth_db.is_configured() and st.session_state.get("supabase_user"):
            progress("Saving to your account")
        save_to_cloud(result)
        status.update(label="Done", state="complete", expanded=False)
    refresh_meetings()
    return result


def save_to_cloud(result):
    """Copy the finished meeting to Supabase; the server disk is wiped on redeploy."""
    if not (auth_db.is_configured() and st.session_state.get("supabase_user")):
        return
    try:
        saved = next(
            (
                meeting
                for meeting in history.list_meetings(limit=50)
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
    if result["status"] == "completed":
        st.success(f"Meeting processed: {result['meeting_id']}")
        st.caption(
            f"{result['action_item_count']} action items · "
            f"Speaker identification: {result['diarization_status'].replace('unavailable', 'not available')}"
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
    show_saved_transcript(result)


def new_meeting_section():
    audio_source = st.segmented_control(
        "Recording source",
        options=[
            "Send AI bot to a meeting",
            "Browser microphone",
            "Tabs / system audio + microphone",
            "Upload file",
        ],
        default="Send AI bot to a meeting",
        key="audio_source",
    )
    consent_confirmed = st.checkbox(
        "I have consent to record and transcribe this meeting."
    )

    if audio_source == "Browser microphone":
        st.caption(
            "Use the recorder’s Start/Stop controls. This captures your browser microphone, "
            "not Meet tab or system audio."
        )
        if not consent_confirmed:
            st.info("Confirm recording consent above to enable the microphone recorder.")
            st.session_state.pop("processed_browser_audio_hash", None)
            return
        recorded_audio = st.audio_input(
            "Record from microphone",
            sample_rate=16000,
            key="meeting_microphone_recording",
        )
        if recorded_audio is None:
            st.session_state.pop("processed_browser_audio_hash", None)
            return
        audio_bytes = recorded_audio.getvalue()
        audio_hash = hashlib.sha256(audio_bytes).hexdigest()
        if audio_hash != st.session_state.get("processed_browser_audio_hash"):
            try:
                result = process_audio(audio_bytes, "browser-recording.wav", consent_confirmed)
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
            return
        bot_url = st.text_input("Meeting link", placeholder="https://meet.google.com/abc-defg-hij")
        bot_name = st.text_input("Bot name", value="AI Assistant")
        if st.button(
            "Send bot to meeting",
            type="primary",
            icon=":material/smart_toy:",
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
                    refresh_meetings()
                    st.success("Finished. Open **My Meetings** to read the notes and transcript.")
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
                    result = process_audio(tab_bytes, f"tab-recording.{extension}", consent_confirmed)
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
                result = process_audio(uploaded_audio.getvalue(), uploaded_audio.name, consent_confirmed)
                show_processing_result(result)
            except Exception as error:
                st.error(f"Meeting processing failed: {error}")


# ---------------------------------------------------------------- Q&A


def answer_question(question):
    """Returns (answer, passages). Raises on failure; callers map errors to messages."""
    if auth_db.is_configured() and st.session_state.get("supabase_user"):
        cloud_db = auth_db.client_for_user()
        rag.index_missing(cloud_db)
        return rag.answer_question(cloud_db, question)
    return history.answer(question, limit=5, owner_id=scope_owner_id), []


def friendly_error(error):
    if "meeting_chunks" in str(error) or "search_meeting_chunks" in str(error):
        return (
            "The search index is not set up yet. Run supabase_migration_rag.sql "
            "in the Supabase SQL Editor."
        )
    return f"Could not answer: {error}"


def show_sources(passages):
    if not passages:
        return
    with st.expander(f"Sources ({len(passages)})"):
        for number, passage in enumerate(passages, start=1):
            st.markdown(
                f"**[{number}]** `{passage['meeting_id']}` · "
                f"{str(passage.get('recorded_at') or '')[:16]} · {passage['kind']}"
            )
            st.text(passage["content"][:600])


# ---------------------------------------------------------------- pages


def first_meeting_help_inline():
    st.info(
        "No meetings yet. Open **New meeting** above, choose a source (AI bot, microphone, "
        "or upload a file), tick the consent box and start. Your summary and action items "
        "will appear here when processing finishes."
    )


def dashboard_page():
    meetings = load_meetings()
    actions = [a for m in meetings for a in minutes_list(m, "action_items")]
    hours = sum(meeting_duration_hours(m) for m in meetings)

    st.title("Meet AI!")
    with st.expander("New meeting: send the bot, record or upload", icon=":material/add_circle:"):
        new_meeting_section()

    metric_columns = st.columns(3)
    metric_columns[0].metric("Meetings", len(meetings), border=True)
    metric_columns[1].metric("Action items", len(actions), border=True)
    metric_columns[2].metric("Hours transcribed", f"{hours:.1f}", border=True)

    recent_column, ask_column = st.columns([3, 2], gap="large")
    with recent_column:
        header, view_all = st.columns([3, 1], vertical_alignment="center")
        header.subheader("Recent meetings")
        if view_all.button("View all", key="view_all", type="tertiary"):
            st.switch_page(PAGE_MEETINGS)
        if not meetings:
            first_meeting_help_inline()
        for meeting in meetings[:3]:
            meeting_card(meeting, "recent")
    with ask_column:
        st.subheader("Ask AI")
        with st.container(border=True):
            quick_question = st.text_input(
                "Question",
                placeholder="What did we decide about the project launch?",
                label_visibility="collapsed",
                key="dashboard_question",
            )
            if st.button("Ask", icon=":material/arrow_forward:", disabled=not quick_question.strip()):
                st.session_state["pending_question"] = quick_question.strip()
                st.switch_page(PAGE_CHAT)
            st.caption("Answers grounded in your private transcripts")


DATE_FILTERS = {"Any time": None, "Last 7 days": 7, "Last 30 days": 30, "Last 90 days": 90}
SORT_OPTIONS = ["Newest first", "Oldest first", "Most action items", "Title A-Z"]


def meeting_card(meeting, key_prefix, show_open=True):
    """Compact, mobile-friendly summary of one meeting."""
    with st.container(border=True):
        st.markdown(f"**{meeting_title(meeting)}**")
        if meeting["minutes"]:
            detail = (
                f"{len(minutes_list(meeting, 'decisions'))} decisions · "
                f"{len(minutes_list(meeting, 'action_items'))} actions"
            )
        else:
            detail = status_label(meeting)
        st.caption(f"{when_label(meeting['recorded_at'])} · {detail}")
        if show_open and st.button("Open", key=f"{key_prefix}_{meeting['id']}", type="tertiary", icon=":material/arrow_forward:"):
            open_meeting(meeting["id"])


def first_meeting_help():
    with st.container(border=True):
        st.markdown("#### Add your first meeting")
        st.markdown(
            "1. Go to the **Dashboard** and open **New meeting**.\n"
            "2. Pick a source: send the bot to a Google Meet, Zoom or Teams link, record with your "
            "microphone, or upload an audio or video file.\n"
            "3. Tick the consent box and start. A summary, decisions and action items appear here "
            "when processing finishes."
        )
        if st.button("Go to Dashboard", icon=":material/add_circle:", type="primary"):
            st.switch_page(PAGE_DASHBOARD)


def meetings_page():
    meetings = load_meetings()
    selected_id = st.session_state.get("open_meeting")
    selected = next((m for m in meetings if m["id"] == selected_id), None)
    if selected:
        if st.button("Back to all meetings", icon=":material/arrow_back:", type="tertiary"):
            st.session_state.pop("open_meeting", None)
            st.rerun()
        show_meeting(selected)
        return

    st.title("My Meetings")
    if not meetings:
        st.info("No meetings yet.")
        first_meeting_help()
        return

    query = st.text_input(
        "Search",
        placeholder="Search by title, topic, person or any word said",
        label_visibility="collapsed",
        icon=":material/search:",
    )
    with st.expander("Filter and sort", icon=":material/tune:"):
        statuses = sorted({m["status"] for m in meetings})
        status_filter = st.selectbox(
            "Status",
            ["All", *statuses],
            format_func=lambda s: "All" if s == "All" else s.replace("_", " ").capitalize(),
        )
        date_filter = st.selectbox("Date", list(DATE_FILTERS))
        sort_by = st.selectbox("Sort by", SORT_OPTIONS)
    if st.button("Refresh", icon=":material/refresh:", type="tertiary"):
        refresh_meetings()
        st.rerun()

    needle = query.strip().casefold()
    days = DATE_FILTERS[date_filter]
    cutoff = datetime.now(timezone.utc) - timedelta(days=days) if days else None
    filtered = [
        m
        for m in meetings
        if (status_filter == "All" or m["status"] == status_filter)
        and (cutoff is None or (parse_when(m["recorded_at"]) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff)
        and (
            not needle
            or needle in meeting_title(m).casefold()
            or needle in m["id"].casefold()
            or needle in m["notes"].casefold()
            or needle in m["transcript"].casefold()
        )
    ]
    if sort_by == "Oldest first":
        filtered.reverse()
    elif sort_by == "Most action items":
        filtered.sort(key=lambda m: len(minutes_list(m, "action_items")), reverse=True)
    elif sort_by == "Title A-Z":
        filtered.sort(key=lambda m: meeting_title(m).casefold())

    if not filtered:
        st.info("No meetings match. Try a different word, or set Date to Any time and Status to All.")
        return
    st.caption(f"{len(filtered)} of {len(meetings)} meetings")
    for meeting in filtered:
        meeting_card(meeting, "list")


def show_meeting(meeting):
    st.header(meeting_title(meeting))
    st.caption(
        f"{when_full(meeting['recorded_at'])} · {status_label(meeting)} · "
        f"{'Saved in cloud' if meeting['source'] == 'cloud' else 'This server only'}"
        + (f" · Recorded by {meeting['owner_email']}" if viewer_sees_all and meeting["owner_email"] else "")
    )
    if meeting["error"]:
        st.warning(meeting["error"])

    minutes = meeting["minutes"]
    if not minutes:
        st.info("A summary has not been generated for this meeting yet.")
    else:
        st.subheader("Summary")
        st.write(minutes.get("summary") or "No summary available.")

        decisions = minutes_list(meeting, "decisions")
        st.subheader(f"Decisions ({len(decisions)})")
        if decisions:
            for decision in decisions:
                st.markdown(f"- {decision.get('decision', '')}")
        else:
            st.caption("No decisions were recorded.")

        items = minutes_list(meeting, "action_items")
        st.subheader(f"Action items ({len(items)})")
        if items:
            for item in items:
                with st.container(border=True):
                    st.markdown(f"**{item.get('task', '')}**")
                    st.caption(
                        f"Owner: {item.get('owner') or 'Not specified'} · "
                        f"Deadline: {item.get('deadline') or 'Not specified'}"
                    )
        else:
            st.caption("No action items were captured.")

        topics = minutes_list(meeting, "discussion_topics")
        if topics:
            st.subheader("Discussion topics")
            for topic in topics:
                with st.expander(str(topic.get("topic", "Topic"))):
                    st.write(topic.get("details", ""))
        questions = minutes_list(meeting, "unresolved_questions")
        if questions:
            st.subheader("Open questions")
            for question in questions:
                st.markdown(f"- {question.get('question', '')}")

    entries = parse_transcript(meeting["transcript"])
    with st.expander(f"Transcript ({len(entries)} lines)" if entries else "Transcript", icon=":material/notes:"):
        if not entries:
            st.info("No transcript available (no speech was captured, or the file is gone from this server).")
        else:
            speakers = sorted({str(e.get("speaker", "Unknown")) for e in entries})
            if len(speakers) > 1:
                chosen = st.multiselect("Show speakers", speakers, default=speakers, key=f"speakers_{meeting['id']}")
            else:
                chosen = speakers
            with st.container(height=480):
                for entry in entries:
                    speaker = str(entry.get("speaker", "Unknown"))
                    if speaker not in chosen:
                        continue
                    start = entry.get("start")
                    stamp = f"{int(start) // 60:02d}:{int(start) % 60:02d}" if isinstance(start, (int, float)) else ""
                    st.markdown(f"`{stamp}` **{speaker}** {entry.get('text', '')}")

    with st.expander("Downloads", icon=":material/download:"):
        if meeting["notes"]:
            st.download_button(
                "Notes (.md)",
                data=meeting["notes"],
                file_name=f"meeting_notes_{meeting['id']}.md",
                mime="text/markdown",
                key=f"notes_{meeting['id']}",
                width="stretch",
            )
        if minutes:
            st.download_button(
                "Structured minutes (.json)",
                data=json.dumps(minutes, indent=2, ensure_ascii=False),
                file_name=f"meeting_minutes_{meeting['id']}.json",
                mime="application/json",
                key=f"minutes_{meeting['id']}",
                width="stretch",
            )
        if entries:
            readable = "\n".join(
                f"[{e.get('timestamp', '')}] {e.get('speaker', 'Unknown')}: {e.get('text', '')}" for e in entries
            )
            st.download_button(
                "Transcript (.txt)",
                data=readable,
                file_name=f"transcript_{meeting['id']}.txt",
                mime="text/plain",
                key=f"txt_{meeting['id']}",
                width="stretch",
            )
            st.download_button(
                "Transcript (.jsonl)",
                data=meeting["transcript"],
                file_name=f"transcript_{meeting['id']}.jsonl",
                mime="application/jsonl",
                key=f"jsonl_{meeting['id']}",
                width="stretch",
            )


def chat_page():
    st.title("Ask AI")
    st.caption("Ask a question in plain words. Answers come from your own meetings and show where they were found.")
    messages = st.session_state.setdefault("chat_messages", [])
    if not load_meetings():
        st.info("There is nothing to ask about yet. Add a meeting first, then come back here.")
        first_meeting_help()
        return

    pending = st.session_state.pop("pending_question", None)
    typed = st.chat_input("Ask about decisions, tasks, deadlines or open questions")
    question = pending or typed

    if not messages and not question:
        suggestion = st.pills(
            "Try asking",
            [
                "What decisions did we make?",
                "What action items are open and who owns them?",
                "What deadlines were discussed?",
            ],
            key="chat_suggestion",
        )
        question = suggestion

    for message in messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            show_sources(message.get("passages"))

    if question:
        messages.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)
        with st.chat_message("assistant"):
            try:
                with st.spinner("Searching your meetings..."):
                    answer, passages = answer_question(question)
            except Exception as error:
                answer, passages = friendly_error(error), []
                st.error(answer)
            else:
                st.markdown(answer)
                show_sources(passages)
        messages.append({"role": "assistant", "content": answer, "passages": passages})
    if messages and st.button("Clear conversation", icon=":material/delete:", type="tertiary"):
        st.session_state["chat_messages"] = []
        st.rerun()


def actions_page():
    st.title("Action Items")
    meetings = load_meetings()
    rows = []
    for meeting in meetings:
        for item in minutes_list(meeting, "action_items"):
            rows.append(
                {
                    "Task": item.get("task", ""),
                    "Owner": item.get("owner", "Not specified"),
                    "Deadline": item.get("deadline", "Not specified"),
                    "Meeting": meeting_title(meeting),
                    "Date": when_full(meeting["recorded_at"]),
                }
            )
    if not rows:
        st.info(
            "No action items yet. Tasks assigned in your meetings are collected here "
            "after a meeting's summary is generated."
        )
        if not meetings:
            first_meeting_help()
        return

    unassigned = sum(r["Owner"].strip().casefold() in {"", "not specified"} for r in rows)
    with_deadline = sum(r["Deadline"].strip().casefold() not in {"", "not specified"} for r in rows)
    metric_columns = st.columns(3)
    metric_columns[0].metric("Total", len(rows), border=True)
    metric_columns[1].metric("No owner", unassigned, border=True)
    metric_columns[2].metric("Has deadline", with_deadline, border=True)

    needle = st.text_input(
        "Search tasks", label_visibility="collapsed", placeholder="Search tasks", icon=":material/search:"
    ).strip().casefold()
    owners = sorted({r["Owner"] for r in rows})
    chosen_owners = st.multiselect("Owner", owners, placeholder="All owners")
    shown = [
        r
        for r in rows
        if (not needle or needle in r["Task"].casefold() or needle in r["Meeting"].casefold())
        and (not chosen_owners or r["Owner"] in chosen_owners)
    ]
    for row in shown:
        with st.container(border=True):
            st.markdown(f"**{row['Task']}**")
            st.caption(f"Owner: {row['Owner']} · Deadline: {row['Deadline']} · {row['Meeting']} ({row['Date']})")

    buffer = io.StringIO()
    writer = csv.DictWriter(buffer, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(shown)
    st.download_button(
        "Export CSV",
        data=buffer.getvalue(),
        file_name="action_items.csv",
        mime="text/csv",
        icon=":material/download:",
    )


def ollama_target_label(host):
    if host.rstrip("/") == CLOUD_OLLAMA_HOST.rstrip("/"):
        return "Ollama Cloud"
    if host.rstrip("/") == LOCAL_OLLAMA_HOST.rstrip("/"):
        return "Local Ollama"
    return "Custom Ollama endpoint"


def settings_page():
    st.title("Settings")

    st.subheader("Account")
    with st.container(border=True):
        if st.session_state.get("supabase_user"):
            st.write(f"Signed in as **{st.session_state['supabase_user']}**")
            if auth_db.is_configured() and viewer_sees_all:
                st.caption("Admin view: you see all users' meetings.")
            elif auth_db.is_configured():
                st.caption(
                    "You see only your own meetings. To get the admin view, set ADMIN_EMAIL "
                    f"to your sign-in email ({viewer_email})."
                )
        else:
            st.write("Password-protected session.")
        if st.button("Sign out" if st.session_state.get("supabase_user") else "Lock app", icon=":material/logout:"):
            auth_db.sign_out()
            st.session_state.pop("authenticated", None)
            st.rerun()

    st.subheader("Transcription")
    with st.container(border=True):
        st.session_state["transcription_language"] = st.selectbox(
            "Language for uploads and in-browser recordings",
            list(LANGUAGES),
            index=list(LANGUAGES).index(st.session_state.get("transcription_language", "Auto-detect")),
            help="Auto-detect picks the language for each segment, so mixed English, Ukrainian and Russian works. "
            "Pick one language only if the meeting is entirely in it. The AI bot uses WHISPER_LANGUAGE on its host.",
        )
        st.caption(
            f"Model: {os.getenv('WHISPER_MODEL') or 'automatic (large-v3-turbo on GPU, small on CPU)'} · "
            f"Audio cleanup: {'off' if os.getenv('AUDIO_CLEANUP') == '0' else 'on'} · "
            f"Spelling correction: {'off' if os.getenv('TRANSCRIPT_LLM_CORRECTION') == '0' else 'on'} · "
            f"Vocabulary learning: {'off' if os.getenv('VOCABULARY_LEARNING') == '0' else 'on'}"
        )

    st.subheader("Vocabulary")
    with st.container(border=True):
        st.caption(
            "Names and terms that should be spelled exactly (one per line). Used by the transcriber and the "
            "correction pass. Stored on the machine that runs the transcription."
        )
        current = VOCABULARY_FILE.read_text(encoding="utf-8") if VOCABULARY_FILE.is_file() else ""
        edited = st.text_area("Your terms", value=current, height=200, label_visibility="collapsed")
        if st.button("Save vocabulary", icon=":material/save:", disabled=edited == current):
            VOCABULARY_FILE.write_text(edited.rstrip() + "\n", encoding="utf-8")
            st.toast("Vocabulary saved.")
            st.rerun()
        learned = [t for t in vocabulary() if t not in current.splitlines()]
        if learned:
            with st.expander(f"Auto-learned terms ({len(learned)})"):
                st.write(", ".join(learned))
                st.caption(f"Managed in {LEARNED_FILE.name}; terms are added after they appear in two meetings.")

    st.subheader("AI service connection")
    with st.container(border=True):
        ollama_host = get_ollama_host()
        api_key_configured = bool(get_setting("OLLAMA_API_KEY"))
        st.caption(
            f"Endpoint: {ollama_target_label(ollama_host)} · "
            f"API key: {'configured' if api_key_configured else 'missing'}"
        )
        if st.button("Test connection", disabled=not api_key_configured, key="test_ollama_connection"):
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


PAGE_DASHBOARD = st.Page(dashboard_page, title="Dashboard", icon=":material/dashboard:", url_path="dashboard", default=True)
PAGE_MEETINGS = st.Page(meetings_page, title="My Meetings", icon=":material/event_note:", url_path="meetings")
PAGE_CHAT = st.Page(chat_page, title="Ask AI", icon=":material/chat:", url_path="chat")
PAGE_ACTIONS = st.Page(actions_page, title="Action Items", icon=":material/task_alt:", url_path="actions")
PAGE_SETTINGS = st.Page(settings_page, title="Settings", icon=":material/settings:", url_path="settings")

HOVER_CSS = """
<style>
button, summary, [role="tab"], [data-baseweb="select"] > div, [data-testid="stSidebarNavLink"],
label:has(input[type="checkbox"]) { transition: background-color .15s ease, border-color .15s ease, color .15s ease; }
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
  background-color: #2B2F45; border-color: #7C7CF0; color: #FFFFFF;
}
.stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {
  background-color: #9494F5; border-color: #9494F5;
}
.stButton > button[kind="tertiary"]:hover { background-color: rgba(124,124,240,.16); color: #B8B8FF; }
[data-testid="stExpander"] summary:hover { background-color: #262A3E; }
[role="tab"]:hover { background-color: rgba(124,124,240,.14); color: #B8B8FF; }
[data-baseweb="select"] > div:hover, [data-baseweb="input"]:hover { border-color: #7C7CF0; background-color: #262A3E; }
[data-testid="stSidebarNavLink"]:hover { background-color: rgba(124,124,240,.18); }
label:has(input[type="checkbox"]):hover { color: #B8B8FF; }
[data-testid="stBaseButton-pills"]:hover, [data-testid="stBaseButton-segmented_control"]:hover {
  background-color: #2B2F45; border-color: #7C7CF0;
}
@media (max-width: 640px) {
  .block-container { padding: 3.5rem 1rem 5rem; }
  .stDownloadButton > button, .stFormSubmitButton > button { width: 100%; }
  [data-testid="stMetricValue"] { font-size: 1.5rem; }
  [data-testid="stChatInput"] { padding-bottom: .5rem; }
  h1 { font-size: 1.7rem !important; }
}
</style>
"""
st.markdown(HOVER_CSS, unsafe_allow_html=True)

st.logo(str(LOGO_FILE), size="large")
navigation = st.navigation([PAGE_DASHBOARD, PAGE_MEETINGS, PAGE_CHAT, PAGE_ACTIONS, PAGE_SETTINGS])
with st.sidebar:
    st.caption(f"Signed in as {st.session_state['supabase_user']}" if st.session_state.get("supabase_user") else "")
    st.caption("Created by Daryna Ladanovska · Version 1.1")
navigation.run()
