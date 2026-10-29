from remote_config import get_setting

MEETINGS_TABLE = "meetings"


def is_configured():
    return bool(get_setting("SUPABASE_URL") and get_setting("SUPABASE_ANON_KEY"))


def create_client():
    from supabase import create_client as _create_client

    return _create_client(get_setting("SUPABASE_URL"), get_setting("SUPABASE_ANON_KEY"))


def sign_in(client, email, password):
    response = client.auth.sign_in_with_password(
        {"email": email.strip(), "password": password}
    )
    return response.user


def sign_up(client, email, password):
    response = client.auth.sign_up({"email": email.strip(), "password": password})
    return response.user, response.session is not None


def sign_out(client):
    try:
        client.auth.sign_out()
    except Exception:
        pass


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
