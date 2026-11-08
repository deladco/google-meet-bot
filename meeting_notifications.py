"""Private, deduplicated in-app notifications and upcoming-meeting alerts."""
import hashlib
import logging
from datetime import UTC, datetime, timedelta

import job_retry

TABLE = "meeting_notifications"
ALERT_WINDOW = timedelta(minutes=15)


def event_key(value):
    return hashlib.sha256(value.encode()).hexdigest()


def job_notification(table, job):
    if table not in {"bot_jobs", "audio_jobs"} or not job.get("user_id"):
        return None
    status = job.get("status")
    if status != "done" and status not in job_retry.FAILURE_STATUSES:
        return None
    completed = status == "done"
    message = job.get("message") or ""
    if not completed:
        _, preserved = job_retry.description(table, status)
        message = " ".join(filter(None, [message, preserved]))
    return {
        "user_id": job["user_id"],
        "event_key": event_key(f"{table}:{job['id']}:{status}:{job.get('updated_at', 'legacy')}"),
        "kind": "completed" if completed else "failed",
        "title": "Meeting processing finished" if completed else "Meeting needs attention",
        "body": message or "Your transcript and minutes are ready. Open the meeting to read them.",
        "job_table": table, "job_id": job["id"], "meeting_id": job.get("meeting_id"),
        "created_at": job.get("updated_at") or datetime.now(UTC).isoformat(),
    }


def store(client, items):
    if items:
        # DO NOTHING on conflict preserves read_at, unlike a normal replacing upsert.
        client.table(TABLE).upsert(items, on_conflict="user_id,event_key", ignore_duplicates=True).execute()


def safe_publish_job(client, table, job):
    item = job_notification(table, job)
    if item:
        try:
            store(client, [item])
        except Exception:
            logging.getLogger(__name__).debug("Notification could not be saved; the dashboard will backfill recent jobs", exc_info=True)


def sync_recent_jobs(client, user_id):
    """Recover missed notifications using the user's RLS-scoped job rows."""
    items = []
    for table in ("bot_jobs", "audio_jobs"):
        rows = client.table(table).select("id,user_id,status,message,meeting_id,updated_at").eq(
            "user_id", user_id,
        ).in_("status", sorted({"done"} | job_retry.FAILURE_STATUSES)).order("updated_at", desc=True).limit(20).execute().data
        items.extend(item for row in rows if (item := job_notification(table, row)))
    store(client, items)


def upcoming_events(events, now=None):
    now = now or datetime.now(UTC)
    return [event for event in events or [] if (
        not event.get("all_day") and event.get("link") and event.get("key")
        and isinstance(event.get("start"), datetime) and event["start"].tzinfo is not None
        and now <= event["start"] <= now + ALERT_WINDOW
    )]


def worker_alerts(user_id, events, availability, handled=(), now=None):
    now = now or datetime.now(UTC)
    if availability["available"]:
        return []
    reason = "busy" if availability["online"] else "offline"
    explanation = "All workers were busy" if reason == "busy" else "No worker was online"
    return [{
        "user_id": user_id,
        "event_key": event_key(f"worker:{event['key']}:{event['start'].isoformat()}:{reason}"),
        "kind": "worker_unavailable", "title": "Worker unavailable for an upcoming meeting",
        "body": (
            f"{explanation} at {now.strftime('%H:%M UTC')}. "
            f"{str(event.get('title') or 'Meeting')[:300]} starts "
            f"{event['start'].astimezone(UTC).strftime('%d %b at %H:%M UTC')}. "
            "Start or free a worker so the bot can join on time."
        ),
        "job_table": None, "job_id": None, "meeting_id": None, "created_at": now.isoformat(),
    } for event in upcoming_events(events, now) if event["key"] not in handled]


def sync_worker_alerts(client, user_id, agenda, availability, now=None):
    if not (agenda.get("feed") or {}).get("enabled"):
        return
    events = upcoming_events(agenda.get("events"), now)
    if not events or availability["available"]:
        return
    rows = client.table("bot_jobs").select("event_key,status").eq("user_id", user_id).in_(
        "event_key", [event["key"] for event in events],
    ).in_("status", ["joining", "recording", "processing", "transcribing", "summarizing", "done"]).execute().data
    store(client, worker_alerts(user_id, events, availability, {row["event_key"] for row in rows}, now))


def inbox(client, user_id):
    rows = client.table(TABLE).select("*").eq("user_id", user_id).order("created_at", desc=True).limit(30).execute().data
    count = client.table(TABLE).select("id", count="exact", head=True).eq("user_id", user_id).is_("read_at", "null").execute().count
    return rows, count or 0


def mark_read(client, user_id, notification_id=None):
    query = client.table(TABLE).update({"read_at": datetime.now(UTC).isoformat()}).eq("user_id", user_id).is_("read_at", "null")
    if notification_id:
        query = query.eq("id", notification_id)
    query.execute()
