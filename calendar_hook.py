"""Send a booked slot to the user's Google Apps Script web app, which adds it to the "bosh" calendar.
Env: CALENDAR_HOOK_URL, CALENDAR_HOOK_TOKEN. Does nothing when not configured."""
import json
import os
import urllib.request



def add(slot, facility, room, detail, title_prefix=""):
    url, token = os.environ.get("CALENDAR_HOOK_URL"), os.environ.get("CALENDAR_HOOK_TOKEN")
    if not url or not token:
        print("calendar: not configured")
        return False
    import reserve
    d = reserve.slot_date(slot)
    start, end = [x.strip() for x in slot[2].replace("～", "~").split("~")]
    payload = {
        "token": token,
        "date": d.strftime("%Y-%m-%d"),
        "start": start,
        "end": end,
        "title": f"{title_prefix}テニス（{facility} {room}）",
        "location": facility,
        "description": f"{slot[1]}のコート\n自動予約。支払いは当日受付で\n\n{detail}",
        "cancel_deadline": reserve.cancel_deadline(slot).strftime("%Y-%m-%d"),
    }
    url = url.strip()
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            text = r.read().decode("utf-8", "replace").strip()
    except Exception as e:
        import re
        reason = re.sub(r"https?://\S+", "<url>", f"{type(e).__name__}: {getattr(e, 'reason', e)}")[:200]
        print("calendar: error", reason)
        return reason
    ok = text == "ok"
    print("calendar:", "added" if ok else f"answer={text[:40]}")
    return ok
