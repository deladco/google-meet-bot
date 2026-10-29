import streamlit as st
from supabase import Client, create_client as _create_client

from remote_config import get_setting

MEETINGS_TABLE = "synced_meetings"
SESSION_KEYS = ("access_token", "refresh_token", "user_id", "supabase_user")


def _first(*names):
    return next((value for name in names if (value := get_setting(name))), None)


def _url():
    return _first("SUPABASE_URL", "NEXT_PUBLIC_SUPABASE_URL")


def _key():
    return _first(
        "SUPABASE_KEY",
        "SUPABASE_ANON_KEY",
        "NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY",
        "NEXT_PUBLIC_SUPABASE_ANON_KEY",
    )


def is_configured():
    return bool(_url() and _key())


def new_client() -> Client:
    return _create_client(_url(), _key())


create_client = new_client


def save_login(response):
    if not response.session or not response.user:
        raise ValueError("Session not available")
    st.session_state.access_token = response.session.access_token
    st.session_state.refresh_token = response.session.refresh_token
    st.session_state.user_id = response.user.id
    st.session_state.supabase_user = response.user.email


def client_for_user() -> Client:
    """Isolated client for this Streamlit session, with a validated, refreshed token."""
    token = st.session_state.get("access_token")
    refresh = st.session_state.get("refresh_token")
    if not token or not refresh:
        raise PermissionError("Not signed in")

    db = new_client()
    session = db.auth.set_session(token, refresh)
    user = db.auth.get_user(session.session.access_token).user
    if user is None:
        raise PermissionError("Invalid session")

    st.session_state.access_token = session.session.access_token
    st.session_state.refresh_token = session.session.refresh_token
    st.session_state.user_id = user.id
    return db


def sign_in(client, email, password):
    return client.auth.sign_in_with_password(
        {"email": email.strip(), "password": password}
    )


def sign_up(client, email, password):
    return client.auth.sign_up({"email": email.strip(), "password": password})


def sign_out():
    try:
        client_for_user().auth.sign_out()
    except Exception:
        pass
    for key in SESSION_KEYS:
        st.session_state.pop(key, None)


def upsert_meeting(client, row):
    return (
        client.table(MEETINGS_TABLE)
        .upsert(row, on_conflict="user_id,meeting_id")
        .execute()
    )


def list_meetings(client, limit=200):
    columns = "meeting_id,recorded_at,status,transcript,notes,minutes"
    try:
        query = client.table(MEETINGS_TABLE).select(columns + ",owner_email")
        return query.order("recorded_at", desc=True).limit(limit).execute().data
    except Exception:
        # owner_email column not added yet (supabase_migration_persist.sql)
        query = client.table(MEETINGS_TABLE).select(columns)
        return query.order("recorded_at", desc=True).limit(limit).execute().data


def save_meeting_from_history(client, meeting, owner_email=None):
    """Persist one local history row (and its files) to Supabase so it survives redeploys."""
    import json
    from pathlib import Path

    def read(path):
        path = Path(path) if path else None
        return path.read_text(encoding="utf-8") if path and path.is_file() else None

    row = {
        "meeting_id": meeting["meeting_id"],
        "recorded_at": meeting["timestamp"],
        "status": meeting["status"],
        "transcript": read(meeting["transcript_path"]) or "",
        "notes": read(meeting["notes_path"]),
        "minutes": json.loads(meeting["minutes_json"]) if meeting["minutes_json"] else None,
    }
    try:
        return upsert_meeting(client, {**row, "owner_email": owner_email})
    except Exception:
        return upsert_meeting(client, row)
