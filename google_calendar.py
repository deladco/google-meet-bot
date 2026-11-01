"""Google Calendar (read-only) sign-in and event reading for calendar auto-join.

Needs GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET (a Google Cloud "Web application" OAuth client) in the
app secrets and in the worker's .env. GOOGLE_REDIRECT_URI must match the client's authorised redirect URI.
"""
import hashlib
import hmac
import time
from datetime import datetime, timezone
from urllib.parse import urlencode

import httpx

from remote_config import get_setting

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
EVENTS_URL = "https://www.googleapis.com/calendar/v3/calendars/primary/events"
SCOPE = "https://www.googleapis.com/auth/calendar.readonly"
STATE_MAX_AGE = 900


def client_id():
    return get_setting("GOOGLE_CLIENT_ID")


def client_secret():
    return get_setting("GOOGLE_CLIENT_SECRET")


def redirect_uri():
    return (get_setting("GOOGLE_REDIRECT_URI") or "https://ai-chief.streamlit.app").strip()


def is_configured():
    return bool(client_id() and client_secret())


def _sign(payload):
    return hmac.new(client_secret().encode(), payload.encode(), hashlib.sha256).hexdigest()[:32]


def make_state(user_id):
    payload = f"{user_id}.{int(time.time())}"
    return f"{payload}.{_sign(payload)}"


def state_belongs_to(state, user_id):
    """True when the state was issued by this app, recently, for this user."""
    try:
        owner, issued, signature = (state or "").rsplit(".", 2)
        payload = f"{owner}.{issued}"
        return (
            hmac.compare_digest(signature, _sign(payload))
            and owner == str(user_id)
            and time.time() - int(issued) <= STATE_MAX_AGE
        )
    except (ValueError, TypeError):
        return False


def authorization_url(user_id):
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id(),
            "redirect_uri": redirect_uri(),
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": make_state(user_id),
        }
    )


def _token_request(data):
    response = httpx.post(TOKEN_URL, data={"client_id": client_id(), "client_secret": client_secret(), **data}, timeout=20)
    payload = response.json()
    if response.status_code != 200:
        raise ValueError(payload.get("error_description") or payload.get("error") or "Google sign-in failed.")
    return payload


def exchange_code(code):
    """Trades the one-time code for a long-lived refresh token."""
    payload = _token_request({"code": code, "grant_type": "authorization_code", "redirect_uri": redirect_uri()})
    if not payload.get("refresh_token"):
        raise ValueError("Google did not return offline access. Remove the app in your Google account and connect again.")
    return payload["refresh_token"]


def access_token(refresh_token):
    return _token_request({"refresh_token": refresh_token, "grant_type": "refresh_token"})["access_token"]


def _links_text(item):
    parts = [item.get("hangoutLink"), item.get("location"), item.get("description")]
    for entry in (item.get("conferenceData") or {}).get("entryPoints", []):
        parts.append(entry.get("uri"))
    return parts


def fetch_events(refresh_token, start, end, link_finder):
    """Timed events starting in [start, end) that carry a Meet/Zoom/Teams link. link_finder(texts) -> url|None."""
    response = httpx.get(
        EVENTS_URL,
        headers={"Authorization": f"Bearer {access_token(refresh_token)}"},
        params={
            "timeMin": start.isoformat(),
            "timeMax": end.isoformat(),
            "singleEvents": "true",
            "orderBy": "startTime",
            "maxResults": 100,
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise ValueError(f"Google Calendar returned an error ({response.status_code}).")
    events = []
    for item in response.json().get("items", []):
        begins = (item.get("start") or {}).get("dateTime")
        if item.get("status") == "cancelled" or not begins:
            continue
        link = link_finder(_links_text(item))
        if not link:
            continue
        when = datetime.fromisoformat(begins).astimezone(timezone.utc)
        events.append(
            {
                "key": f"{item.get('id')}|{when.isoformat()}",
                "title": item.get("summary") or "Meeting",
                "start": when,
                "link": link,
            }
        )
    return events
