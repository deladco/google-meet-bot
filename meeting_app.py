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
import minutes_store
import rag
from tab_recorder import tab_recorder
from transcript_quality import LEARNED_FILE, vocabulary

PROJECT_DIR = Path(__file__).resolve().parent
VOCABULARY_FILE = PROJECT_DIR / "vocabulary.txt"
LANGUAGES = {"Auto-detect": None, "English": "en", "Ukrainian": "uk", "Russian": "ru"}
LOGO_FILE = PROJECT_DIR / "assets" / "logo_neu.svg"
MEETINGS_CACHE_SECONDS = 45

load_dotenv(dotenv_path=PROJECT_DIR / ".env")
st.set_page_config(
    page_title="Daryna's AI Meetings Assistant",
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


BOT_STEPS = ["queued", "joining", "recording", "transcribing", "summarizing", "done"]


def render_bot_status():
    """Shows the signed-in user's own bot job. Every query goes through their RLS-scoped client."""
    try:
        db = auth_db.client_for_user()
        bot_id = st.session_state.get("active_bot_id")
        job = meeting_bot.get_job(db, bot_id) if bot_id else meeting_bot.latest_job(db)
        if job is None or (not bot_id and not meeting_bot.is_active(job["status"])):
            return False
        st.session_state["active_bot_id"] = job["id"]
        code = job["status"]
        step = {"stop_requested": "recording", "processing": "transcribing"}.get(code, code)
        if code in meeting_bot.FAILED:
            st.error(f"{meeting_bot.describe_status(code)}. {job.get('message') or ''}".strip())
            return False
        if step in BOT_STEPS:
            st.progress(BOT_STEPS.index(step) / (len(BOT_STEPS) - 1), text=meeting_bot.describe_status(code))
        else:
            st.write(f"**Bot status:** {meeting_bot.describe_status(code)}")
        if code == "queued":
            st.caption(
                "The bot worker (`python bot_worker.py`) must be running on a computer with "
                "access to the meeting's audio to pick this up."
            )
        if job.get("message"):
            st.caption(job["message"])
        if code in meeting_bot.FINISHED:
            return False
        if code in meeting_bot.STOPPABLE and st.button(
            "Make the bot leave now", key=f"leave_{job['id']}", width="stretch"
        ):
            meeting_bot.leave_meeting(db, job["id"])
            st.rerun(scope="fragment")
        return True
    except Exception as error:
        st.error(f"Bot error: {error}")
        return False


def bot_status_panel():
    """Polls the job every 2 s without rerunning the dashboard; the whole page refreshes once when it ends."""
    was_active = st.session_state.get("bot_panel_active", False)

    @st.fragment(run_every="2s" if was_active else None)
    def panel():
        active = render_bot_status()
        if active != st.session_state.get("bot_panel_active", False):
            st.session_state["bot_panel_active"] = active
            if not active and st.session_state.get("active_bot_id"):
                refresh_meetings()
            st.rerun()

    panel()
    if not st.session_state.get("bot_panel_active", False) and st.session_state.get("active_bot_id"):
        job = meeting_bot.get_job(auth_db.client_for_user(), st.session_state["active_bot_id"])
        if job and job["status"] in meeting_bot.FINISHED:
            st.success("Finished. Open **My Meetings** to read the notes and transcript.")


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
    bot_mode = audio_source == "Send AI bot to a meeting"
    bot_url = ""
    if bot_mode:
        bot_url = st.text_input(
            "Meeting link",
            placeholder="Paste your Google Meet, Zoom or Teams link",
            key="bot_meeting_link",
            label_visibility="collapsed",
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
        bot_url = bot_url.strip()
        with st.expander("Bot options"):
            bot_name = st.text_input("Bot name", value="AI Assistant")
        link_ok = bot_url.lower().startswith(("http://", "https://")) and any(
            host in bot_url.lower() for host in ("meet.google.", "zoom.", "teams.")
        )
        if bot_url and not link_ok:
            st.warning("That doesn't look like a Google Meet, Zoom or Teams link.")
        if not bot_url:
            st.caption("Paste a meeting link above to enable this button.")
        if st.button(
            "Send bot to meeting",
            type="primary",
            icon=":material/smart_toy:",
            disabled=not bot_url,
            width="stretch",
            key="send_bot",
        ):
            if not consent_confirmed:
                st.error("Tick the consent box above first, so everyone in the meeting is covered.")
            elif not link_ok:
                st.error("Paste a full Google Meet, Zoom or Teams link first.")
            else:
                try:
                    st.session_state["active_bot_id"] = meeting_bot.join_meeting(
                        auth_db.client_for_user(), bot_url, bot_name, viewer_email
                    )
                except Exception as error:
                    st.error(f"Could not queue the bot: {error}")
        bot_status_panel()
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


NO_PROJECT = "No project"
UNASSIGNED = "Unassigned"


def load_projects():
    if "projects" not in st.session_state:
        try:
            st.session_state["projects"] = auth_db.get_projects(auth_db.client_for_user())
        except Exception:
            st.session_state["projects"] = []
    return st.session_state["projects"]


def store_projects(names):
    try:
        auth_db.save_projects(auth_db.client_for_user(), names)
        st.session_state["projects"] = names
        st.session_state.pop("task_error", None)
    except Exception as error:
        st.session_state["task_error"] = f"Could not save projects: {error}"


def delete_project(name):
    meetings = load_meetings()
    changed = minutes_store.remove_project(meetings, name)
    try:
        for meeting in changed:
            if meeting["source"] == "cloud":
                minutes_store.save_minutes(
                    auth_db.client_for_user(), meeting, meeting["minutes"], user_id=st.session_state.get("user_id")
                )
    except Exception as error:
        st.session_state["task_error"] = f"Could not update tasks: {error}"
        return
    store_projects([n for n in load_projects() if n.casefold() != name.casefold()])


def rename_project(old, new):
    new = minutes_store.clean_project(new)
    names = load_projects()
    if not new or new == old:
        return False
    meetings = load_meetings()
    taken = next((n for n in names if n.casefold() == new.casefold() and n.casefold() != old.casefold()), None)
    if taken:
        new = taken  # merging into the existing project keeps its spelling
    changed = minutes_store.rename_project(meetings, old, new)
    try:
        for meeting in changed:
            if meeting["source"] == "cloud":
                minutes_store.save_minutes(
                    auth_db.client_for_user(), meeting, meeting["minutes"], user_id=st.session_state.get("user_id")
                )
    except Exception as error:
        st.session_state["task_error"] = f"Could not update tasks: {error}"
        return False
    store_projects(minutes_store.renamed_list(names, old, new))
    return not st.session_state.get("task_error")


def add_project_form(key):
    with st.form(key, clear_on_submit=True, border=False):
        name_column, button_column = st.columns([3, 1], vertical_alignment="bottom")
        name = name_column.text_input("New project", placeholder="New project name", label_visibility="collapsed")
        submitted = button_column.form_submit_button("Add project", icon=":material/add:", width="stretch")
    if submitted:
        current = load_projects()
        updated = minutes_store.add_project(current, name)
        if updated == current:
            st.warning("Enter a project name that doesn't exist yet.")
        else:
            store_projects(updated)
            st.rerun()


def open_task_board(project):
    st.session_state["pending_project_filter"] = [project]
    st.switch_page(PAGE_ACTIONS)


def projects_section(meetings):
    rows = minutes_store.task_rows(meetings)
    names = minutes_store.all_projects(load_projects(), rows)
    stats = minutes_store.project_stats(rows, names)
    with st.container(border=True, key="projects"):
        st.subheader("Projects", anchor=False)
        st.caption("Group your tasks by project. Move a task into a project from the Task board.")
        if st.session_state.get("task_error"):
            st.error(st.session_state["task_error"])
        entries = [(name, stats[name]) for name in names]
        if stats[""]["open"] + stats[""]["done"]:
            entries.append((UNASSIGNED, stats[""]))
        if not entries:
            st.info("No projects yet. Add one below, then assign tasks to it.")
        for start in range(0, len(entries), 3):
            columns = st.columns(3)
            for column, (name, counts) in zip(columns, entries[start : start + 3]):
                with column.container(border=True):
                    st.markdown(f"**{name}**")
                    st.caption(f"{counts['open']} open · {counts['done']} done")
                    if st.button("Open tasks", key=f"open_project_{name}", type="tertiary", icon=":material/arrow_forward:"):
                        open_task_board(name)
        add_project_form("add_project_dashboard")
        if names:
            with st.expander("Manage projects"):
                for name in names:
                    name_column, rename_column, delete_column = st.columns([3, 1, 1], vertical_alignment="bottom")
                    new_name = name_column.text_input(
                        "Project name", value=name, key=f"rename_input_{name}", label_visibility="collapsed"
                    )
                    if rename_column.button(
                        "Rename", key=f"rename_project_{name}", disabled=not new_name.strip() or new_name.strip() == name
                    ):
                        if rename_project(name, new_name):
                            st.rerun()
                    if delete_column.button("Delete", key=f"delete_project_{name}", icon=":material/delete:"):
                        delete_project(name)
                        st.rerun()
                st.caption(
                    "Renaming updates all of the project's tasks. Using the name of another project merges them. "
                    "Deleting a project keeps its tasks; they become unassigned."
                )


def dashboard_page():
    meetings = load_meetings()
    actions = [a for m in meetings for a in minutes_list(m, "action_items")]
    hours = sum(meeting_duration_hours(m) for m in meetings)

    st.title("Daryna's AI Meetings Assistant")
    with st.container(border=True, key="new_meeting"):
        st.subheader("Start a new meeting", anchor=False)
        st.caption("Paste a meeting link and the AI joins, records and writes your notes. Or record or upload audio.")
        new_meeting_section()

    metric_columns = st.columns(3)
    metric_columns[0].metric("Meetings", len(meetings), border=True)
    metric_columns[1].metric("Action items", len(actions), border=True)
    metric_columns[2].metric("Hours transcribed", f"{hours:.1f}", border=True)

    projects_section(meetings)

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


def sources_caption(item):
    lines = minutes_store.source_text(item)
    if lines:
        st.caption(":material/schedule: " + "  \n".join(lines))


def persist_minutes(meeting, minutes, message):
    try:
        minutes_store.save_minutes(auth_db.client_for_user(), meeting, minutes, user_id=st.session_state.get("user_id"))
    except Exception as error:
        st.error(f"Could not save: {error}")
        return False
    st.toast(message, icon=":material/check_circle:")
    return True


def show_minutes(meeting):
    minutes = meeting["minutes"]
    cloud = meeting["source"] == "cloud"
    approved = minutes_store.is_approved(minutes)
    info = minutes_store.review(minutes)
    if approved:
        st.success(
            f"Approved{' by ' + info['approved_by'] if info.get('approved_by') else ''} · {when_full(info.get('approved_at'))}",
            icon=":material/verified:",
        )
    else:
        st.info(
            "AI draft. Check the summary, decisions and tasks against their sources, edit what is wrong, "
            "then approve.",
            icon=":material/edit_note:",
        )
    if cloud:
        edit_column, approve_column = st.columns(2)
        if edit_column.button("Edit notes", icon=":material/edit:", width="stretch", key=f"edit_btn_{meeting['id']}"):
            st.session_state[f"editing_{meeting['id']}"] = True
            st.rerun()
        if not approved and approve_column.button(
            "Approve notes", icon=":material/verified:", type="primary", width="stretch", key=f"approve_{meeting['id']}"
        ):
            minutes_store.approve(minutes, by=viewer_email)
            if persist_minutes(meeting, minutes, "Notes approved"):
                st.rerun()
        if approved and approve_column.button("Reopen as draft", width="stretch", key=f"reopen_{meeting['id']}"):
            minutes["review"] = {"status": "draft", "edited": info.get("edited", False)}
            if persist_minutes(meeting, minutes, "Reopened as draft"):
                st.rerun()
    else:
        st.caption("Editing and approval need the cloud copy of this meeting (sign in and process it again).")

    st.subheader("Summary")
    st.write(minutes.get("summary") or "No summary available.")
    for line in minutes_store.source_text({"transcript_refs": minutes.get("summary_refs")}, limit=1):
        st.caption(f":material/schedule: {line}")

    decisions = minutes_list(meeting, "decisions")
    st.subheader(f"Decisions ({len(decisions)})")
    if decisions:
        for decision in decisions:
            with st.container(border=True):
                st.markdown(decision.get("decision", ""))
                sources_caption(decision)
    else:
        st.caption("No decisions were recorded.")

    items = minutes_list(meeting, "action_items")
    st.subheader(f"Action items ({len(items)})")
    if items:
        for item in items:
            with st.container(border=True):
                done = item.get("status") == "done"
                st.markdown(f"~~{item.get('task', '')}~~" if done else f"**{item.get('task', '')}**")
                due = minutes_store.parse_due(item.get("due"))
                st.caption(
                    f"Owner: {item.get('owner') or 'Not specified'} · "
                    f"Deadline: {item.get('deadline') or 'Not specified'}"
                    + (f" · Due {due.strftime('%d %b %Y')}" if due else "")
                    + (" · Done" if done else "")
                )
                sources_caption(item)
        st.page_link(PAGE_ACTIONS, label="Track these on the task board", icon=":material/checklist:")
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


def edit_minutes_form(meeting):
    minutes = meeting["minutes"]
    mid = meeting["id"]
    st.subheader("Edit notes")
    st.caption("Changes are saved to your account and the notes are regenerated. You approve them afterwards.")
    with st.form(f"edit_form_{mid}"):
        summary = st.text_area("Summary", value=minutes.get("summary") or "", height=180)
        decisions_text = st.text_area(
            "Decisions (one per line)",
            value="\n".join(str(d.get("decision", "")) for d in minutes_list(meeting, "decisions")),
            height=140,
        )
        st.markdown("**Action items**")
        rows = []
        for index, item in enumerate(minutes_list(meeting, "action_items")):
            with st.container(border=True):
                task = st.text_input("Task", value=item.get("task", ""), key=f"et_{mid}_{index}")
                owner_column, deadline_column = st.columns(2)
                owner = owner_column.text_input("Owner", value=item.get("owner", ""), key=f"eo_{mid}_{index}")
                deadline = deadline_column.text_input(
                    "Deadline", value=item.get("deadline", ""), key=f"ed_{mid}_{index}", placeholder="e.g. 2026-11-05"
                )
                remove = st.checkbox("Remove this item", key=f"er_{mid}_{index}")
            rows.append({"id": item.get("id"), "task": task, "owner": owner, "deadline": deadline, "remove": remove})
        with st.container(border=True):
            new_task = st.text_input("Add an action item", key=f"nt_{mid}", placeholder="Optional")
            new_owner_column, new_deadline_column = st.columns(2)
            new_owner = new_owner_column.text_input("Owner", key=f"no_{mid}")
            new_deadline = new_deadline_column.text_input("Deadline", key=f"nd_{mid}")
        rows.append({"task": new_task, "owner": new_owner, "deadline": new_deadline})
        save_column, cancel_column = st.columns(2)
        save = save_column.form_submit_button("Save changes", type="primary", width="stretch")
        cancel = cancel_column.form_submit_button("Cancel", width="stretch")
    if cancel:
        st.session_state.pop(f"editing_{mid}", None)
        st.rerun()
    if save:
        updated = minutes_store.apply_edits(minutes, summary, decisions_text, rows)
        if persist_minutes(meeting, updated, "Notes saved as draft"):
            for key in [k for k in st.session_state if str(k).endswith(f"_{mid}") or f"_{mid}_" in str(k)]:
                if key.startswith(("et_", "eo_", "ed_", "er_", "nt_", "no_", "nd_")):
                    st.session_state.pop(key, None)
            st.session_state.pop(f"editing_{mid}", None)
            st.rerun()


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
        minutes_store.ensure_ids(minutes, meeting["id"])
        if st.session_state.get(f"editing_{meeting['id']}") and meeting["source"] == "cloud":
            edit_minutes_form(meeting)
        else:
            show_minutes(meeting)

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


TASK_VIEWS = ["Open", "Done", "All"]
DUE_FILTERS = {
    "Any time": None,
    "Overdue": {"overdue"},
    "Due today": {"today"},
    "Next 7 days": {"today", "week"},
    "No due date": {"none"},
}


def update_task(meeting_id, task_id, widget_key, field):
    meeting = next((m for m in load_meetings() if m["id"] == meeting_id), None)
    if meeting is None or meeting["source"] != "cloud":
        return
    value = st.session_state.get(widget_key)
    if field == "status":
        value = "done" if value else "open"
    elif field == "project" and value == NO_PROJECT:
        value = ""
    if not minutes_store.set_task(meeting["minutes"], task_id, **{field: value}):
        return
    try:
        minutes_store.save_minutes(
            auth_db.client_for_user(), meeting, meeting["minutes"], user_id=st.session_state.get("user_id")
        )
        st.session_state.pop("task_error", None)
    except Exception as error:
        st.session_state["task_error"] = f"Could not save the change: {error}"


def due_label(row, today):
    bucket = minutes_store.due_bucket(row, today)
    if not row["due"]:
        return "No due date"
    text = row["due"].strftime("%d %b")
    if bucket == "overdue":
        return f":red[Overdue · {text}]"
    if bucket == "today":
        return ":orange[Due today]"
    return f"Due {text}"


def task_card(row, today, project_names):
    meeting = row["meeting"]
    editable = meeting["source"] == "cloud"
    base = f"{meeting['id']}_{row['id']}"
    with st.container(border=True):
        st.checkbox(
            row["task"],
            value=row["done"],
            key=f"done_{base}",
            disabled=not editable,
            on_change=update_task,
            args=(meeting["id"], row["id"], f"done_{base}", "status"),
        )
        project_tag = f"{row['project']} · " if row["project"] else ""
        st.caption(f"{project_tag}{row['owner']} · {due_label(row, today)} · {meeting_title(meeting)}")
        options = [NO_PROJECT, *project_names]
        project_key = f"project_{base}_{row['project']}"
        st.selectbox(
            "Project",
            options,
            index=options.index(row["project"]) if row["project"] in options else 0,
            key=project_key,
            disabled=not editable,
            on_change=update_task,
            args=(meeting["id"], row["id"], project_key, "project"),
        )
        with st.expander("Details"):
            owner_column, due_column = st.columns(2)
            owner_column.text_input(
                "Owner",
                value=row["owner"] if row["owner"] != "Not specified" else "",
                key=f"owner_{base}",
                disabled=not editable,
                on_change=update_task,
                args=(meeting["id"], row["id"], f"owner_{base}", "owner"),
            )
            due_column.date_input(
                "Due date",
                value=row["due"],
                key=f"due_{base}",
                disabled=not editable,
                format="YYYY-MM-DD",
                on_change=update_task,
                args=(meeting["id"], row["id"], f"due_{base}", "due"),
            )
            if row["deadline"] != "Not specified":
                st.caption(f"Said in the meeting: {row['deadline']}")
            sources_caption({"transcript_refs": row["refs"]})
            if st.button("Open meeting", key=f"goto_{base}", icon=":material/arrow_forward:", type="tertiary"):
                open_meeting(meeting["id"])
            if not editable:
                st.caption("Read-only: this meeting is not saved to your account.")


def actions_page():
    st.title("Task board")
    st.caption("Tasks from your meeting notes. Tick them off, set due dates, and jump back to where they came from.")
    meetings = load_meetings()
    rows = minutes_store.task_rows(meetings)
    if not rows:
        st.info(
            "No action items yet. Tasks assigned in your meetings are collected here "
            "after a meeting's summary is generated."
        )
        if not meetings:
            first_meeting_help()
        return
    if st.session_state.get("task_error"):
        st.error(st.session_state["task_error"])

    today = datetime.now().date()
    buckets = [minutes_store.due_bucket(r, today) for r in rows]
    metric_columns = st.columns(4)
    metric_columns[0].metric("Open", sum(not r["done"] for r in rows), border=True)
    metric_columns[1].metric("Overdue", buckets.count("overdue"), border=True)
    metric_columns[2].metric("Next 7 days", buckets.count("today") + buckets.count("week"), border=True)
    metric_columns[3].metric("Done", sum(r["done"] for r in rows), border=True)

    view = st.segmented_control("Show", TASK_VIEWS, default="Open", label_visibility="collapsed") or "Open"
    project_names = minutes_store.all_projects(load_projects(), rows)
    project_options = [*project_names, UNASSIGNED]
    if "pending_project_filter" in st.session_state:
        st.session_state["task_projects"] = st.session_state.pop("pending_project_filter")
    if "task_projects" in st.session_state:
        st.session_state["task_projects"] = [p for p in st.session_state["task_projects"] if p in project_options]
    chosen_projects = st.multiselect(
        "Project", project_options, key="task_projects", placeholder="All projects", label_visibility="collapsed"
    )
    needle = st.text_input(
        "Search tasks", label_visibility="collapsed", placeholder="Search tasks or meetings", icon=":material/search:"
    ).strip().casefold()
    with st.expander("Filter and sort", icon=":material/tune:"):
        due_filter = st.selectbox("Due", list(DUE_FILTERS))
        owners = sorted({r["owner"] for r in rows})
        chosen_owners = st.multiselect("Owner", owners, placeholder="All owners")
        meeting_titles = sorted({meeting_title(r["meeting"]) for r in rows})
        chosen_meetings = st.multiselect("Meeting", meeting_titles, placeholder="All meetings")
        sort_by = st.selectbox("Sort by", ["Due date", "Newest meeting", "Owner", "Project"])
    with st.expander("New project", icon=":material/create_new_folder:"):
        add_project_form("add_project_board")

    allowed = DUE_FILTERS[due_filter]
    shown = [
        r
        for r in rows
        if (view == "All" or (view == "Done") == r["done"])
        and (allowed is None or minutes_store.due_bucket({**r, "done": False}, today) in allowed)
        and (not chosen_projects or (r["project"] or UNASSIGNED) in chosen_projects)
        and (not chosen_owners or r["owner"] in chosen_owners)
        and (not chosen_meetings or meeting_title(r["meeting"]) in chosen_meetings)
        and (not needle or needle in r["task"].casefold() or needle in meeting_title(r["meeting"]).casefold())
    ]
    newest_first = sorted(
        shown,
        key=lambda r: parse_when(r["meeting"]["recorded_at"]) or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    if sort_by == "Due date":
        shown = sorted(newest_first, key=lambda r: (r["due"] is None, r["due"] or datetime.max.date()))
    elif sort_by == "Owner":
        shown = sorted(newest_first, key=lambda r: r["owner"].casefold())
    elif sort_by == "Project":
        shown = sorted(newest_first, key=lambda r: (not r["project"], r["project"].casefold()))
    else:
        shown = newest_first

    if not shown:
        st.info("Nothing here. Change the filters, or switch the view to All.")
    st.caption(f"{len(shown)} of {len(rows)} tasks")
    for row in shown:
        task_card(row, today, project_names)

    export = [
        {
            "Task": r["task"],
            "Project": r["project"],
            "Status": "Done" if r["done"] else "Open",
            "Owner": r["owner"],
            "Due": r["due"].isoformat() if r["due"] else "",
            "Said in meeting": r["deadline"],
            "Meeting": meeting_title(r["meeting"]),
            "Date": when_full(r["meeting"]["recorded_at"]),
        }
        for r in shown
    ]
    if export:
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=list(export[0]))
        writer.writeheader()
        writer.writerows(export)
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
PAGE_ACTIONS = st.Page(actions_page, title="Task board", icon=":material/task_alt:", url_path="actions")
PAGE_SETTINGS = st.Page(settings_page, title="Settings", icon=":material/settings:", url_path="settings")

HOVER_CSS = """
<style>
:root {
  --bg: #23262E; --raise: 6px 6px 14px rgba(10,11,15,.75), -6px -6px 14px rgba(58,63,77,.45);
  --raise-sm: 3px 3px 8px rgba(10,11,15,.7), -3px -3px 8px rgba(58,63,77,.4);
  --inset: inset 3px 3px 7px rgba(10,11,15,.75), inset -3px -3px 7px rgba(58,63,77,.4);
  --accent: #D9A400;
}
.stApp, [data-testid="stAppViewContainer"], [data-testid="stHeader"] { background: var(--bg) !important; }
.block-container { max-width: 1100px; padding-top: 5rem; }
h1, h2, h3 { font-weight: 700 !important; letter-spacing: -.02em; color: #F2F2F5; }
h1 { font-size: 2.2rem !important; }
hr { border: none !important; height: 2px; background: var(--bg); box-shadow: var(--inset); border-radius: 2px; }

button, summary, [role="tab"], [data-baseweb="select"] > div, [data-baseweb="input"] {
  transition: box-shadow .18s ease, background-color .18s ease, color .18s ease;
}
/* raised cards */
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stExpander"], [data-testid="stMetric"],
[data-testid="stForm"], [data-testid="stAlert"] {
  background: var(--bg) !important; border: none !important; border-radius: 20px !important;
  box-shadow: var(--raise); padding: 1rem 1.1rem;
}
[data-testid="stExpander"] details { border: none !important; }
[data-testid="stExpander"] summary:hover { background: rgba(255,255,255,.06); border-radius: 14px; }
[data-testid="stImage"] img { border-radius: 14px; }

/* pill buttons */
.stButton > button, .stDownloadButton > button, .stFormSubmitButton > button,
[data-testid="stBaseButton-pills"], [data-testid="stBaseButton-segmented_control"] {
  background: var(--bg); color: #E6E8F2; border: none; border-radius: 999px;
  box-shadow: var(--raise-sm); padding: .45rem 1.4rem;
}
.stButton > button:hover, .stDownloadButton > button:hover, .stFormSubmitButton > button:hover {
  background: #2A2E38; color: #D9A400; box-shadow: var(--raise);
}
.stButton > button:active, .stDownloadButton > button:active, .stFormSubmitButton > button:active {
  box-shadow: var(--inset);
}
.stButton > button:focus-visible, .stFormSubmitButton > button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: #E0AB10; color: #14100A; font-weight: 600;
}
.stButton > button[kind="primary"]:hover, .stFormSubmitButton > button[kind="primary"]:hover {
  background: #E8B923; color: #14100A;
}
.stButton > button[kind="tertiary"] { box-shadow: none; background: transparent; }
.stButton > button[kind="tertiary"]:hover { box-shadow: var(--raise-sm); }
.stButton > button:disabled, .stFormSubmitButton > button:disabled { opacity: 1; cursor: not-allowed; color: #7C8090; box-shadow: var(--raise-sm); }
.stButton > button[kind="primary"]:disabled, .stFormSubmitButton > button[kind="primary"]:disabled {
  background: #4A3E10; color: #A8955A; box-shadow: var(--raise-sm); border: 1px dashed rgba(255,227,71,.35);
}

/* inset inputs */
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="textarea"], [data-baseweb="select"] > div,
[data-testid="stChatInput"] > div {
  background: var(--bg) !important; border: none !important; border-radius: 999px !important; box-shadow: var(--inset);
}
[data-baseweb="textarea"] { border-radius: 20px !important; }
[data-baseweb="input"] input, [data-baseweb="base-input"] input, [data-baseweb="textarea"] textarea { background: transparent !important; }
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within, [data-baseweb="select"] > div:focus-within {
  outline: 2px solid var(--accent); outline-offset: 2px;
}

/* tabs as soft segmented pills */
[data-baseweb="tab-list"] { gap: .5rem; background: transparent; }
[role="tab"] { border-radius: 999px; padding: .4rem 1.1rem; box-shadow: var(--raise-sm); background: var(--bg); }
[role="tab"][aria-selected="true"] { box-shadow: var(--inset); color: #D9A400; }
[role="tab"]:hover { color: #D9A400; }
[data-baseweb="tab-highlight"], [data-baseweb="tab-border"] { display: none; }

/* top navigation links */
[data-testid="stSidebarNavLink"], [data-testid="stTopNavLink"] { border-radius: 999px; }
[data-testid="stSidebarNavLink"]:hover { background: rgba(217,164,0,.14); }
[data-testid="stTopNavLink"], [data-testid="stTopNavSection"] a {
  font-size: 1.15rem !important; font-weight: 600; padding: .8rem 1.75rem !important; min-height: 3.1rem;
  border-radius: 999px; background: var(--bg); box-shadow: var(--raise-sm); color: #E6E8F2;
}
[data-testid="stTopNavLink"] span, [data-testid="stTopNavLink"] p { font-size: 1.15rem !important; }
[data-testid="stTopNavLink"] [data-testid="stIconMaterial"] { font-size: 1.5rem !important; }
[data-testid="stTopNavLink"]:hover { color: #D9A400; box-shadow: var(--raise); }
[data-testid="stTopNavLink"][aria-current="page"], [data-testid="stTopNavLink"][aria-selected="true"] { box-shadow: var(--inset); color: #D9A400; }
[data-testid="stTopNavLinkContainer"] { gap: 1rem; }
[data-testid="stTopNavLink"] {
  transition: box-shadow .35s cubic-bezier(.4,0,.2,1), color .35s ease, background-color .35s ease, transform .35s cubic-bezier(.4,0,.2,1);
  box-shadow: 4px 4px 12px rgba(10,11,15,.55), -4px -4px 12px rgba(58,63,77,.3);
}
[data-testid="stTopNavLink"]:hover { transform: translateY(-1px); }
[data-testid="stHeader"] { min-height: 5.5rem; }
[data-testid="stHeaderLogo"], [data-testid="stLogo"] { height: 4.5rem !important; max-height: none !important; width: auto; }
[data-baseweb="slider"] [role="slider"] { box-shadow: var(--raise-sm); background: #D9A400 !important; }
[data-testid="stProgress"] > div > div { background: var(--bg); box-shadow: var(--inset); border-radius: 999px; }

.st-key-new_meeting { padding: 1.5rem 1.75rem; }
.st-key-new_meeting [data-testid="stTextInput"] input {
  font-size: 1.15rem; min-height: 3.4rem; padding-left: 1rem;
}
.st-key-new_meeting .stButton > button[kind="primary"] {
  min-height: 3.4rem; font-size: 1.1rem; font-weight: 600;
}
/* neon yellow details */
:root { --neon: #FFE347; --neon-glow: 0 0 0 2px rgba(255,227,71,.55), 0 0 14px rgba(255,227,71,.45), 0 0 34px rgba(255,227,71,.18); }
/* raised blocks: thick bottom edge + faint neon rim */
[data-testid="stVerticalBlockBorderWrapper"], [data-testid="stExpander"], [data-testid="stMetric"], [data-testid="stForm"] {
  box-shadow: var(--raise), 0 5px 0 #14161C, inset 0 1px 0 rgba(255,227,71,.14) !important;
}
[data-testid="stMetric"] { border-bottom: 2px solid rgba(255,227,71,.35) !important; }
[data-testid="stMetricValue"] { color: var(--neon); text-shadow: 0 0 14px rgba(255,227,71,.45); }
.st-key-new_meeting { box-shadow: var(--raise), var(--neon-glow) !important; }
/* neon focus / glowing pill outlines */
[data-baseweb="input"]:focus-within, [data-baseweb="textarea"]:focus-within, [data-baseweb="select"] > div:focus-within,
[data-testid="stChatInput"] > div:focus-within { outline: none !important; box-shadow: var(--inset), var(--neon-glow) !important; }
.stButton > button:focus-visible, .stFormSubmitButton > button:focus-visible { outline: none !important; box-shadow: var(--neon-glow) !important; }
.stButton > button[kind="primary"], .stFormSubmitButton > button[kind="primary"] {
  background: #F2C400; box-shadow: var(--raise-sm), 0 0 16px rgba(255,227,71,.4);
}
[data-baseweb="input"], [data-baseweb="base-input"], [data-baseweb="select"] > div {
  border: 1px solid rgba(255,227,71,.12) !important;
}
/* glowing nav + tabs */
[data-testid="stTopNavLink"][aria-current="page"], [data-testid="stTopNavLink"][aria-selected="true"],
[role="tab"][aria-selected="true"] { color: var(--neon) !important; box-shadow: var(--inset), 0 0 0 1.5px rgba(255,227,71,.6), 0 0 16px rgba(255,227,71,.35) !important; }
[data-testid="stTopNavLink"]:hover, [role="tab"]:hover { color: var(--neon) !important; }
/* toggles, checkboxes, sliders, progress */
[data-baseweb="checkbox"] > div:first-child { box-shadow: var(--inset); border-radius: 8px; border-color: transparent !important; }
[data-baseweb="checkbox"] input:checked + div, [data-testid="stCheckbox"] [aria-checked="true"] { box-shadow: 0 0 10px rgba(255,227,71,.6); }
[data-baseweb="slider"] [role="slider"] { background: var(--neon) !important; box-shadow: var(--raise-sm), 0 0 14px rgba(255,227,71,.7) !important; }
[data-testid="stProgress"] [role="progressbar"] > div > div { background: #F2C400 !important; box-shadow: 0 0 12px rgba(255,227,71,.6); }
/* alerts as tinted tiles */
[data-testid="stAlert"] { border-left: 4px solid var(--neon) !important; }
/* dividers glow softly, scrollbar styled */
hr { background: rgba(255,227,71,.3) !important; box-shadow: none !important; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: var(--bg); box-shadow: var(--inset); border-radius: 999px; }
::-webkit-scrollbar-thumb { background: #3A3F4D; border-radius: 999px; border: 2px solid var(--bg); }
::-webkit-scrollbar-thumb:hover { background: #D9A400; }
::selection { background: rgba(255,227,71,.35); }
/* flat pastel-style tiles (dark-adapted from the reference) */
:root {
  --tile-blue: #2A344D;
  --tile-yellow: #3E3921;
  --tile-mix: #323650;
  --tile-ink: #292D37;
}
[data-testid="stMetric"] { background: var(--tile-blue) !important; }
[data-testid="stColumn"]:nth-child(even) [data-testid="stMetric"] { background: var(--tile-yellow) !important; }
[data-testid="stColumn"]:nth-child(3n) [data-testid="stMetric"] { background: var(--tile-mix) !important; }
[data-testid="stVerticalBlockBorderWrapper"] { background: var(--tile-mix) !important; }
[data-testid="stVerticalBlockBorderWrapper"] [data-testid="stVerticalBlockBorderWrapper"] { background: var(--tile-ink) !important; }
[data-testid="stExpander"] { background: var(--tile-blue) !important; }
[data-testid="stForm"] { background: var(--tile-yellow) !important; }
[data-testid="stAlert"] { background: var(--tile-mix) !important; }
.st-key-new_meeting { background: #3E3C2E !important; }
[data-testid="stMetricLabel"] { color: #AEB8D6; }
[data-testid="stMetricValue"] { color: #FFE347; }
/* unified button system: every button kind shares the same pill look */
[data-testid^="stBaseButton-"]:not([data-testid="stBaseButton-borderlessIcon"]):not([data-testid="stBaseButton-minimal"]):not([data-testid="stBaseButton-headerNoPadding"]):not([data-testid="stBaseButton-tertiary"]),
[data-testid^="stBaseLinkButton-"], [data-testid="stPopoverButton"] {
  background: var(--bg); color: #E6E8F2; border: none !important; border-radius: 999px !important;
  box-shadow: var(--raise-sm); font-weight: 500;
  transition: box-shadow .3s cubic-bezier(.4,0,.2,1), background .3s ease, color .3s ease, transform .3s ease;
}
[data-testid^="stBaseButton-"]:not([data-testid="stBaseButton-borderlessIcon"]):not([data-testid="stBaseButton-minimal"]):not([data-testid="stBaseButton-headerNoPadding"]):not([data-testid="stBaseButton-tertiary"]):hover:not(:disabled),
[data-testid^="stBaseLinkButton-"]:hover, [data-testid="stPopoverButton"]:hover {
  background: #2A2E38; color: var(--neon); box-shadow: var(--raise), 0 0 12px rgba(255,227,71,.18); transform: translateY(-1px);
}
[data-testid^="stBaseButton-"]:active:not(:disabled), [data-testid^="stBaseLinkButton-"]:active { box-shadow: var(--inset) !important; transform: none; }
[data-testid="stBaseButton-primary"], [data-testid="stBaseButton-primaryFormSubmit"], [data-testid^="stBaseLinkButton-primary"] {
  background: #F2C400 !important; color: #14100A !important; font-weight: 600;
  box-shadow: var(--raise-sm), 0 0 16px rgba(255,227,71,.4);
}
[data-testid="stBaseButton-primary"]:hover:not(:disabled), [data-testid="stBaseButton-primaryFormSubmit"]:hover:not(:disabled) {
  background: #FFD92E !important; color: #14100A !important; box-shadow: var(--raise), 0 0 22px rgba(255,227,71,.55);
}
[data-testid="stBaseButton-primary"]:disabled, [data-testid="stBaseButton-primaryFormSubmit"]:disabled {
  background: #4A3E10 !important; color: #A8955A !important;
  border: 1px dashed rgba(255,227,71,.35) !important; box-shadow: var(--raise-sm); opacity: 1; cursor: not-allowed;
}
[data-testid^="stBaseButton-"]:disabled:not([data-testid*="primary"]) { color: #7C8090 !important; opacity: 1; cursor: not-allowed; }
/* tertiary "Open / View all / Back" links: ghost pill, glow on hover */
[data-testid="stBaseButton-tertiary"] {
  background: transparent; color: #C9CEE0; border-radius: 999px !important; box-shadow: none; transition: all .3s ease;
}
[data-testid="stBaseButton-tertiary"]:hover:not(:disabled) { background: var(--bg); color: var(--neon); box-shadow: var(--raise-sm); }
/* pills & segmented selection */
[data-testid="stBaseButton-pillsActive"], [data-testid="stBaseButton-segmented_controlActive"] {
  background: var(--bg) !important; color: var(--neon) !important; box-shadow: var(--inset), 0 0 0 1.5px rgba(255,227,71,.6), 0 0 14px rgba(255,227,71,.3) !important;
}
[data-testid="stBaseButton-pillsActive"], [data-testid="stBaseButton-segmented_controlActive"],
[data-testid="stBaseButton-pills"], [data-testid="stBaseButton-segmented_control"] { border-radius: 999px !important; border: none !important; }
/* file uploader, audio input, chat send */
[data-testid="stFileUploaderDropzone"] {
  background: var(--bg) !important; border: 1px dashed rgba(255,227,71,.35) !important; border-radius: 20px !important; box-shadow: var(--inset);
}
[data-testid="stFileUploaderDropzone"]:hover { box-shadow: var(--inset), 0 0 14px rgba(255,227,71,.25); }
[data-testid="stAudioInput"] { background: var(--bg) !important; border: none !important; border-radius: 999px !important; box-shadow: var(--inset); }
[data-testid="stAudioInputActionButton"], [data-testid="stChatInputSubmitButton"] {
  background: var(--bg) !important; border-radius: 999px !important; box-shadow: var(--raise-sm); color: var(--neon) !important;
}
[data-testid="stChatInputSubmitButton"]:hover:not(:disabled), [data-testid="stAudioInputActionButton"]:hover { box-shadow: var(--raise), 0 0 12px rgba(255,227,71,.4); }
[data-testid^="stBaseButton-"]:focus-visible, [data-testid^="stBaseLinkButton-"]:focus-visible { outline: none !important; box-shadow: var(--neon-glow) !important; }
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
navigation = st.navigation([PAGE_DASHBOARD, PAGE_MEETINGS, PAGE_CHAT, PAGE_ACTIONS, PAGE_SETTINGS], position="top")
navigation.run()
st.divider()
footer = "Created by Daryna Ladanovska · Version 1.1"
if st.session_state.get("supabase_user"):
    footer = f"Signed in as {st.session_state['supabase_user']} · {footer}"
st.caption(footer)
