import json
from datetime import datetime, timezone
from uuid import uuid4

import streamlit as st

from auth_db import MEETINGS_TABLE, client_for_user, new_client, save_login, sign_out

st.set_page_config(page_title="AI Meeting Assistant", page_icon="🎙️", layout="wide")


def login_screen():
    st.title("AI Meeting Assistant")
    st.caption("Private meeting history and AI notes")

    login_tab, signup_tab = st.tabs(["Sign in", "Create account"])

    with login_tab:
        with st.form("login"):
            email = st.text_input("Email")
            password = st.text_input("Password", type="password")
            submitted = st.form_submit_button("Sign in", width="stretch")

        if submitted:
            try:
                response = new_client().auth.sign_in_with_password(
                    {"email": email.strip(), "password": password}
                )
                save_login(response)
            except Exception:
                st.error("Sign-in failed. Check your credentials and confirm your email.")
            else:
                st.rerun()

    with signup_tab:
        with st.form("signup"):
            email = st.text_input("Email address", key="signup_email")
            password = st.text_input("Choose password", type="password", key="signup_password")
            submitted = st.form_submit_button("Create account", width="stretch")

        if submitted:
            try:
                new_client().auth.sign_up({"email": email.strip(), "password": password})
            except Exception:
                st.error("Account registration could not be completed.")
            else:
                st.success("Check your email for a verification link, then return to sign in.")


def load_jsonl(data):
    entries = []
    for line in data.decode("utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if not isinstance(record, dict):
            raise ValueError("Invalid transcript entry")
        text = str(record.get("text", "")).strip()
        if text:
            entries.append(
                f"[{record.get('timestamp', '')}] "
                f"{record.get('speaker', 'Unknown')}: {text}"
            )
    return "\n".join(entries)


def upload_meeting(db):
    st.subheader("Add meeting")

    with st.form("upload_meeting"):
        title = st.text_input("Meeting title")
        transcript_file = st.file_uploader("Transcript (.jsonl or .txt)", type=["jsonl", "txt"])
        notes_file = st.file_uploader("AI meeting notes (.md)", type=["md"])
        submit = st.form_submit_button("Save meeting", width="stretch")

    if not submit:
        return
    if not title.strip():
        st.error("Enter a meeting title.")
        return
    if transcript_file is None:
        st.error("Upload a transcript.")
        return

    try:
        raw = transcript_file.getvalue()
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Transcript exceeds the 2 MB limit.")

        if transcript_file.name.lower().endswith(".jsonl"):
            transcript = load_jsonl(raw)
        else:
            transcript = raw.decode("utf-8")

        notes = notes_file.getvalue().decode("utf-8") if notes_file else ""
        if len(notes.encode("utf-8")) > 500_000:
            raise ValueError("Notes file too large.")
        if not transcript.strip():
            raise ValueError("Transcript is empty.")

        # user_id defaults to auth.uid() in the database and is enforced by RLS.
        db.table(MEETINGS_TABLE).insert(
            {
                "meeting_id": uuid4().hex,
                "title": title.strip(),
                "transcript": transcript,
                "notes": notes,
                "status": "uploaded",
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        ).execute()
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
        st.error(f"Invalid upload: {error}")
    except Exception:
        st.error("Unable to save meeting.")
    else:
        st.success("Meeting saved securely.")
        st.rerun()


def meeting_history(db):
    st.subheader("My meeting history")

    search = st.text_input(
        "Search meetings", placeholder="Search titles or meeting notes..."
    ).lower().strip()

    result = (
        db.table(MEETINGS_TABLE)
        .select("id,title,meeting_id,recorded_at,transcript,notes")
        .order("recorded_at", desc=True)
        .limit(100)
        .execute()
    )
    meetings = result.data or []

    if search:
        meetings = [
            m for m in meetings
            if search in ((m["title"] or "") + " " + (m["notes"] or "")).lower()
        ]

    st.metric("Meetings shown", len(meetings))
    if not meetings:
        st.info("No matching meetings found.")
        return

    options = {
        f"{m['title'] or m['meeting_id']} | {(m['recorded_at'] or '')[:10]} | {m['id'][:8]}": m
        for m in meetings
    }
    meeting = options[st.selectbox("Select a meeting", options=list(options.keys()))]

    st.markdown(f"### {meeting['title'] or meeting['meeting_id']}")
    notes_tab, transcript_tab = st.tabs(["AI Notes", "Transcript"])

    with notes_tab:
        st.markdown(meeting["notes"] or "No AI notes saved.")
        st.download_button(
            "Download notes",
            data=meeting["notes"] or "",
            file_name="meeting_notes.md",
            mime="text/markdown",
        )

    with transcript_tab:
        st.text_area("Full transcript", value=meeting["transcript"] or "", height=400)
        st.download_button(
            "Download transcript",
            data=meeting["transcript"] or "",
            file_name="meeting_transcript.txt",
            mime="text/plain",
        )

    st.divider()
    confirm = st.checkbox("I want to permanently delete this meeting.")
    if st.button("Delete selected meeting", disabled=not confirm):
        db.table(MEETINGS_TABLE).delete().eq("id", meeting["id"]).execute()
        st.success("Meeting deleted.")
        st.rerun()


def main():
    if not st.session_state.get("access_token"):
        login_screen()
        return

    try:
        db = client_for_user()
    except Exception:
        st.session_state.clear()
        st.warning("Session expired. Please sign in.")
        login_screen()
        return

    st.title("🎙️ AI Meeting Assistant")

    with st.sidebar:
        st.success("Signed in")
        if st.button("Log out"):
            sign_out()
            st.session_state.clear()
            st.rerun()

    dashboard, upload = st.tabs(["My Meetings", "Upload Meeting"])
    with dashboard:
        meeting_history(db)
    with upload:
        upload_meeting(db)


if __name__ == "__main__":
    main()
