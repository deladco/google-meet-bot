import streamlit as st
from supabase import Client, create_client as _create_client

from remote_config import get_setting

MEETINGS_TABLE = "meetings"
SESSION_KEYS = ("access_token", "refresh_token", "user_id", "supabase_user")


def _key():
    return get_setting("SUPABASE_KEY") or get_setting("SUPABASE_ANON_KEY")


def is_configured():
    return bool(get_setting("SUPABASE_URL") and _key())


def new_client() -> Client:
    return _create_client(get_setting("SUPABASE_URL"), _key())


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
    return (
        client.table(MEETINGS_TABLE)
        .select("meeting_id,recorded_at,status,transcript,notes,minutes")
        .order("recorded_at", desc=True)
        .limit(limit)
        .execute()
        .data
    )
