"""Calendar auto-join: reads a user's secret iCal feed and queues a bot job for upcoming meetings.

Works with any calendar that offers a private iCal (.ics) link: Google Calendar, Outlook, Apple.
The app stores the link in `calendar_feeds`; bot_worker.py calls `sync_feeds` about once a minute.
"""
import html
import ipaddress
import re
import socket
from datetime import date, datetime, timedelta, timezone
from urllib.parse import unquote, urlparse

import httpx

import meeting_bot

FEEDS_TABLE = "calendar_feeds"
MAX_FEED_BYTES = 5_000_000
JOIN_EARLY = timedelta(minutes=2)
JOIN_LATE = timedelta(minutes=10)
LINK_PATTERN = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)*(?:meet\.google\.com|zoom\.us|zoom\.com|teams\.microsoft\.com|teams\.live\.com|webinar-stars\.com)"
    r"/[^\s<>\"')\\]+",
    re.IGNORECASE,
)


def normalize_feed_url(url):
    url = (url or "").strip()
    if url.lower().startswith("webcal://"):
        url = "https://" + url[len("webcal://"):]
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("Paste the full calendar link (it should start with https:// or webcal://).")
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(parsed.hostname, 443)}
    except OSError:
        raise ValueError("Could not reach that calendar address.") from None
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
            raise ValueError("That address is not a public calendar link.")
    return url


def _download(url):
    for _ in range(4):
        with httpx.stream("GET", url, timeout=20, follow_redirects=False) as response:
            if response.status_code in (301, 302, 303, 307, 308):
                url = normalize_feed_url(str(response.url.join(response.headers.get("location", ""))))
                continue
            if response.status_code != 200:
                raise ValueError(f"The calendar link returned an error ({response.status_code}).")
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > MAX_FEED_BYTES:
                    raise ValueError("The calendar feed is too large.")
            return bytes(data)
    raise ValueError("The calendar link redirected too many times.")


def find_link(texts):
    """First supported meeting link found in any of the given strings (tolerates HTML, wrapped and scheme-less links)."""
    for text in texts:
        text = unquote(html.unescape(str(text or "")))
        for candidate in LINK_PATTERN.findall(text):
            candidate = candidate.rstrip(".,;")
            if not candidate.lower().startswith("http"):
                candidate = "https://" + candidate
            try:
                return meeting_bot.validate_meeting_url(candidate)
            except ValueError:
                continue
    return None


def meeting_link(event):
    return find_link(str(event.get(field) or "") for field in ("LOCATION", "URL", "X-GOOGLE-CONFERENCE", "DESCRIPTION"))


def _as_utc(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, date):
        return datetime(value.year, value.month, value.day, tzinfo=timezone.utc)
    return None


def fetch_events(url, start, end):
    """Events with a Meet/Zoom/Teams link that start between start and end (UTC datetimes)."""
    import icalendar
    import recurring_ical_events

    calendar = icalendar.Calendar.from_ical(_download(normalize_feed_url(url)))
    events = []
    for event in recurring_ical_events.of(calendar).between(start, end):
        if str(event.get("STATUS", "")).upper() == "CANCELLED":
            continue
        begins = _as_utc(event.get("DTSTART").dt) if event.get("DTSTART") else None
        if not begins:
            continue
        events.append(
            {
                "end": _as_utc(event.get("DTEND").dt) if event.get("DTEND") else None,
                "key": f"{event.get('UID', 'event')}|{begins.isoformat()}",
                "title": str(event.get("SUMMARY") or "Meeting"),
                "start": begins,
                "link": meeting_link(event),
                "all_day": not isinstance(event.get("DTSTART").dt, datetime),
            }
        )
    return sorted(events, key=lambda item: item["start"])


def feed_events(feed, start, end):
    """Events for a saved feed: Google Calendar when connected, otherwise the iCal link."""
    if feed.get("google_refresh_token"):
        import google_calendar

        return google_calendar.fetch_events(feed["google_refresh_token"], start, end, find_link)
    return fetch_events(feed["ics_url"], start, end)


def sync_feeds(db):
    """Queue a bot for every enabled feed's meeting that is starting now. Returns jobs queued."""
    now = datetime.now(timezone.utc)
    queued = 0
    feeds = db.table(FEEDS_TABLE).select("*").eq("enabled", True).execute().data
    for feed in feeds:
        try:
            events = feed_events(feed, now - JOIN_LATE, now + JOIN_EARLY)
        except Exception as error:
            print("Calendar sync failed for", feed.get("user_email"), "-", error)
            continue
        for event in events:
            if not event["link"] or event.get("all_day"):
                continue
            if not (event["start"] - JOIN_EARLY <= now <= event["start"] + JOIN_LATE):
                continue
            if event.get("end") and now >= event["end"]:
                continue  # the meeting is already over
            row = {
                "user_id": feed["user_id"],
                "user_email": feed.get("user_email"),
                "meeting_url": event["link"],
                "bot_name": feed.get("bot_name") or "AI Assistant",
                "event_key": event["key"],
                "message": f"Scheduled from your calendar: {event['title']}"[:200],
            }
            try:
                db.table(meeting_bot.JOBS_TABLE).insert(row).execute()
                queued += 1
                print("Queued calendar meeting:", event["title"])
            except Exception:
                # The unique (user_id, event_key) index means this one was already queued.
                pass
    return queued
