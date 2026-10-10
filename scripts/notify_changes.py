#!/usr/bin/env python3
"""Email when an event is added or edited in the sheet (deleted events are ignored).

The last-seen state of every event is kept in storage/events_snapshot.json.
An event is identified by its name + start date:
 - changing the start time, end, or location counts as an UPDATE
 - changing the name or start date counts as a NEW event
The first run only records what is already in the sheet (no emails).
Changes to events that have already ended are recorded but not emailed.
Set DRY_RUN=1 to print instead of send.
"""
import json, os, re, smtplib, urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

from send_reminders import build_ics, calendar_link, parse_events

SNAPSHOT_FILE = "storage/events_snapshot.json"
FMT = "%A, %B %-d, %Y at %-I:%M %p"


def details(e):
    return {"name": e["name"], "start": e["whenStart"].isoformat(),
            "end": e["whenEnd"].isoformat(), "location": e["location"]}


def event_key(e, taken):
    base = f"{e['name']}|{e['whenStart'].strftime('%Y-%m-%d')}"
    key, n = base, 2
    while key in taken:  # two rows with the same name and date
        key, n = f"{base}#{n}", n + 1
    return key


def load_snapshot():
    """Return the saved events dict, or None if there is no usable snapshot yet."""
    if not os.path.exists(SNAPSHOT_FILE):
        return None
    with open(SNAPSHOT_FILE) as f:
        raw = f.read().strip()
    data = json.loads(raw) if raw else {}
    return data["events"] if isinstance(data.get("events"), dict) else None


def save_snapshot(events):
    os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
    with open(SNAPSHOT_FILE, "w") as f:
        json.dump({"events": events}, f, indent=2)


def pretty(iso):
    return datetime.fromisoformat(iso).strftime(FMT)


def what_changed(old, e):
    out = []
    if old["start"] != e["whenStart"].isoformat():
        out.append(f"START: was {pretty(old['start'])}, now {e['whenStart'].strftime(FMT)}")
    if old["end"] != e["whenEnd"].isoformat():
        out.append(f"END: was {pretty(old['end'])}, now {e['whenEnd'].strftime(FMT)}")
    if old["location"] != e["location"]:
        out.append(f"WHERE: was {old['location'] or '(blank)'}, now {e['location'] or '(blank)'}")
    return out


def starts_in(left):
    hours = left.total_seconds() / 3600
    if hours >= 48:
        return f"about {round(hours / 24)} days"
    if hours >= 1:
        n = round(hours)
        return f"about {n} hour{'s' if n != 1 else ''}"
    n = max(1, round(hours * 60))
    return f"about {n} minute{'s' if n != 1 else ''}"


def build_change_email(e, old, now, site_url):
    lines = ["WE JUST MADE NEW PLANS" if old is None else "OUR PLANS JUST CHANGED", "",
             f"WHAT: {e['name']}",
             f"WHEN: {e['whenStart'].strftime(FMT)} until {e['whenEnd'].strftime(FMT)}"]
    if e["location"]:
        lines.append(f"WHERE: {e['location']}")
    if old is not None:
        lines += ["", "WHAT CHANGED:"] + what_changed(old, e)
    left = e["whenStart"] - now
    if left > timedelta(0):
        lines += ["", f"Starts in {starts_in(left)} (WOOHOO)"]
    lines += ["", f"Link to a Google Calendar invite: {calendar_link(e)}"]
    if site_url:
        lines += ["", f"To see the rest of our wonderful plans: {site_url}"]
    prefix = "NEW PLANS" if old is None else "UPDATED PLANS"
    subject = f"{prefix}: {e['name']} ({e['whenStart'].strftime('%a %b %-d, %-I:%M %p')})"
    return subject, "\n".join(lines)


def main():
    tz = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))
    site_url = os.environ.get("SITE_URL", "")
    dry = os.environ.get("DRY_RUN") == "1"
    now = datetime.now(tz)

    with urllib.request.urlopen(os.environ["SHEET_CSV_URL"], timeout=30) as r:
        text = r.read().decode("utf-8-sig")
    current = {}
    for e in parse_events(text, tz):
        current[event_key(e, current)] = e

    snap = load_snapshot()
    if snap is None:  # first run: remember what is already there, send nothing
        if not current:
            print("No events read and no snapshot yet; nothing recorded.")
        else:
            print(f"No snapshot yet: recording {len(current)} existing event(s) without sending email.")
            if not dry:
                save_snapshot({k: details(e) for k, e in current.items()})
        return
    if not current and snap:  # an empty read is more likely a hiccup than everything being deleted
        print("Sheet returned no events; leaving the snapshot alone.")
        return

    changes = [(k, e, snap.get(k)) for k, e in current.items() if snap.get(k) != details(e)]
    due = [c for c in changes if c[1]["whenEnd"] > now]
    for k, e, _ in changes:
        if e["whenEnd"] <= now:
            snap[k] = details(e)  # finished events: record quietly
    print(f"{len(changes)} new/updated event(s), {len(due)} email(s) to send.")

    if due and not dry:
        recipients = [x.strip() for x in os.environ["REMINDER_EMAILS"].split(",") if x.strip()]
        user = os.environ["GMAIL_ADDRESS"].strip()
        password = "".join(os.environ["GMAIL_APP_PASSWORD"].split())
        smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        try:
            smtp.login(user, password)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as err:
            raise SystemExit(f"Gmail login failed ({type(err).__name__}). Check GMAIL_ADDRESS and GMAIL_APP_PASSWORD.")
    for k, e, old in due:
        subject, body = build_change_email(e, old, now, site_url)
        if dry:
            print(f"[dry run] {subject}\n{body}\n")
            continue
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = user, ", ".join(recipients), subject
        msg.set_content(body)
        fname = (re.sub(r"\W+", "-", e["name"]).strip("-") or "event") + ".ics"
        msg.add_attachment(build_ics(e).encode("utf-8"), maintype="text", subtype="calendar",
                           filename=fname, params={"method": "PUBLISH"})
        smtp.send_message(msg)
        snap[k] = details(e)
        save_snapshot(snap)  # save after each send so a later failure can't cause repeats
        print(f"Sent: {subject}")
    if not dry:
        for k in [k for k in snap if k not in current]:
            del snap[k]  # deleted events are dropped quietly
        save_snapshot(snap)


if __name__ == "__main__":
    main()