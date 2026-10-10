#!/usr/bin/env python3
"""Email a reminder when an event is within its "Reminder (Hours Before)" window.

Sheet columns: Event, Date, Time, Location, Reminder (Hours Before)
 - Reminder blank or not a number -> 24 hours. 0 or negative -> no reminder.
Sent reminders are logged in sent_reminders.json so nothing is emailed twice.
Set DRY_RUN=1 to print instead of send.
"""
import csv, io, json, os, re, smtplib, urllib.request
from datetime import datetime, timedelta
from email.message import EmailMessage
from zoneinfo import ZoneInfo

STATE_FILE = "sent_reminders.json"
DEFAULT_HOURS = 24.0
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


def parse_date(s):
    """Return (year, month, day) or None. Slash dates are US month/day/year."""
    m = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s)
    if m:
        return int(m[1]), int(m[2]), int(m[3])
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})", s)
    if m:
        y = int(m[3])
        return (2000 + y if len(m[3]) == 2 else y), int(m[1]), int(m[2])
    m = re.search(r"([A-Za-z]{3})[a-z]*\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s+(\d{4})", s)
    if m and m[1].lower() in MONTHS:
        return int(m[3]), MONTHS.index(m[1].lower()) + 1, int(m[2])
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3})[a-z]*\.?,?\s+(\d{4})", s)
    if m and m[2].lower() in MONTHS:
        return int(m[3]), MONTHS.index(m[2].lower()) + 1, int(m[1])
    return None


def parse_time(s):
    """Return (hour, minute). Handles 18:30, 18:30:00, 6:30 PM, 6 pm. Blank = midnight."""
    m = re.match(r"^(\d{1,2})(?::(\d{2}))?(?::\d{2})?\s*([ap])?\.?m?\.?$", s, re.I)
    if not m:
        return 0, 0
    h, ap = int(m[1]), (m[3] or "").lower()
    if ap == "p" and h < 12:
        h += 12
    if ap == "a" and h == 12:
        h = 0
    return h, int(m[2] or 0)


def parse_events(text, tz):
    events = []
    for row in list(csv.reader(io.StringIO(text)))[1:]:
        row = [c.strip() for c in row] + [""] * 7
        name, startDate, startTime, endDate, endTime, location, rem = row[:7]
        startD = parse_date(startDate)
        endD = parse_date(endDate)
        if not name or not startD or not endD:
            continue
        try:
            hours = float(rem) if rem else DEFAULT_HOURS
        except ValueError:
            hours = DEFAULT_HOURS
        if hours <= 0:
            continue
        startH, startMi = parse_time(startTime)
        endH, endMi = parse_time(endTime)
        try:
            whenStart = datetime(startD[0], startD[1], startD[2], startH, startMi, tzinfo=tz)
            whenEnd = datetime(endD[0], endD[1], endD[2], endH, endMi, tzinfo=tz)
        except ValueError:
            continue
        events.append({"name": name, "location": location, "whenStart": whenStart, "whenEnd": whenEnd, "hours": hours})
    return events


def due_reminders(events, now, sent):
    out = []
    for e in events:
        key = f"{e['name']}|{e['whenStart'].isoformat()}||{e['whenEnd'].isoformat()}|{e['hours']:g}"
        if e["whenStart"] - timedelta(hours=e["hours"]) <= now < e["whenStart"] and key not in sent:
            out.append((key, e))
    return out


def build_email(e, now, site_url):
    left = e["whenStart"] - now
    if left >= timedelta(hours=1):
        n = round(left.total_seconds() / 3600)
        soon = f"about {n} hour{'s' if n != 1 else ''}"
    else:
        n = max(1, round(left.total_seconds() / 60))
        soon = f"about {n} minute{'s' if n != 1 else ''}"
    lines = ["WE'RE GONNA DO SOMETHING TOGETHER VERY SOON", "", f'WHAT: {e["name"]}', f'WHEN: {e["whenStart"].strftime("%A, %B %-d, %Y at %-I:%M %p")} until {e["whenEnd"].strftime("%A, %B %-d, %Y at %-I:%M %p")}']
    if e["location"]:
        lines.append(f"WHERE: {e['location']}")
    lines += ["", f"Starts in {soon} (WOOHOO)"]
    if site_url:
        lines += ["", f"To see the rest of our wonderful plans: {site_url}"]
    subject = f"UPCOMING PLANS: {e['name']} ({e['whenStart'].strftime('%a %b %-d, %-I:%M %p')})"
    return subject, "\n".join(lines)


def main():
    tz = ZoneInfo(os.environ.get("TIMEZONE", "America/New_York"))
    site_url = os.environ.get("SITE_URL", "")
    dry = os.environ.get("DRY_RUN") == "1"
    now = datetime.now(tz)

    with urllib.request.urlopen(os.environ["SHEET_CSV_URL"], timeout=30) as r:
        text = r.read().decode("utf-8-sig")
    events = parse_events(text, tz)

    sent = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            raw = f.read().strip()
        sent = json.loads(raw) if raw else {}  # an empty file counts as "nothing sent yet"
    cutoff = (now - timedelta(days=30)).isoformat()
    pruned = {k: v for k, v in sent.items() if v >= cutoff}
    changed = pruned != sent
    sent = pruned

    due = due_reminders(events, now, sent)
    print(f"{len(events)} events read, {len(due)} reminder(s) due.")
    if due and not dry:
        recipients = [x.strip() for x in os.environ["REMINDER_EMAILS"].split(",") if x.strip()]
        # Secrets often pick up stray spaces or a trailing newline when pasted; Gmail
        # drops the connection if they reach the login step, so clean them first.
        user = os.environ["GMAIL_ADDRESS"].strip()
        password = "".join(os.environ["GMAIL_APP_PASSWORD"].split())
        smtp = smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30)
        try:
            smtp.login(user, password)
        except (smtplib.SMTPAuthenticationError, smtplib.SMTPServerDisconnected) as err:
            raise SystemExit(
                f"Gmail login failed ({type(err).__name__}). Check GMAIL_ADDRESS and "
                "GMAIL_APP_PASSWORD, and that 2-Step Verification is on for that account."
            )
    for key, e in due:
        subject, body = build_email(e, now, site_url)
        if dry:
            print(f"[dry run] {subject}\n{body}\n")
            continue
        msg = EmailMessage()
        msg["From"], msg["To"], msg["Subject"] = user, ", ".join(recipients), subject
        msg.set_content(body)
        smtp.send_message(msg)
        sent[key] = e["whenStart"].isoformat()
        changed = True
        print(f"Sent: {subject}")
        with open(STATE_FILE, "w") as f:  # save after each send so a later failure can't cause repeats
            json.dump(sent, f, indent=2)
    if changed and not dry:
        with open(STATE_FILE, "w") as f:
            json.dump(sent, f, indent=2)


if __name__ == "__main__":
    main()