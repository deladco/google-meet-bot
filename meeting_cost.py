"""User-entered meeting cost estimates; no inferred salaries or exchange rates."""
import copy
import math
from decimal import ROUND_HALF_UP, Decimal

CURRENCIES = ("GBP", "USD", "EUR", "UAH", "CAD", "AUD")


def estimate(duration_minutes, participants, hourly_rate, currency):
    if type(participants) is not int or participants < 1:
        raise ValueError("Enter at least one participant.")
    if not math.isfinite(duration_minutes) or duration_minutes <= 0:
        raise ValueError("Enter a duration greater than zero.")
    if not math.isfinite(hourly_rate) or hourly_rate < 0:
        raise ValueError("Hourly rate must be zero or greater.")
    if currency not in CURRENCIES:
        raise ValueError("Choose a supported currency.")
    total = (Decimal(str(duration_minutes)) / 60 * participants * Decimal(str(hourly_rate))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP,
    )
    return {"duration_minutes": float(duration_minutes), "participants": participants,
            "hourly_rate": float(hourly_rate), "currency": currency, "estimated_cost": float(total)}


def saved_estimate(meeting):
    cost = (meeting.get("minutes") or {}).get("cost_estimate")
    if not isinstance(cost, dict):
        return None
    try:
        return estimate(cost["duration_minutes"], cost["participants"], cost["hourly_rate"], cost["currency"])
    except (KeyError, TypeError, ValueError, ArithmeticError):
        return None


def totals(meetings):
    groups = {}
    for meeting in meetings:
        cost = saved_estimate(meeting)
        if cost is None:
            continue
        group = groups.setdefault(cost["currency"], {"total": Decimal(0), "meetings": 0})
        group["total"] += Decimal(str(cost["estimated_cost"]))
        group["meetings"] += 1
    return {currency: {"total": float(group["total"]), "meetings": group["meetings"]} for currency, group in sorted(groups.items())}


def save(client, meeting, cost, user_id):
    if meeting.get("source") != "cloud" or not meeting.get("minutes") or not user_id:
        raise ValueError("Save estimates on your cloud meeting after its minutes have been generated.")
    validated = estimate(cost["duration_minutes"], cost["participants"], cost["hourly_rate"], cost["currency"])
    minutes = copy.deepcopy(meeting["minutes"])
    minutes["cost_estimate"] = validated
    rows = client.table("synced_meetings").update({"minutes": minutes}).eq(
        "meeting_id", meeting["id"],
    ).eq("user_id", user_id).execute().data
    if not rows:
        raise PermissionError("The estimate could not be saved. Refresh and check that this meeting belongs to your account.")
    meeting["minutes"] = minutes
