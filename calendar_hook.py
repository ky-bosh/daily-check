"""Send a booked slot to the user's Google Apps Script web app, which adds it to the "bosh" calendar.
Env: CALENDAR_HOOK_URL, CALENDAR_HOOK_TOKEN. Does nothing when not configured."""
import json
import os
import urllib.request



def add(slot, facility, room, detail):
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
        "title": f"テニス（{facility} {room}）",
        "location": facility,
        "description": f"{slot[1]}のコート\n自動予約。支払いは当日受付で\n\n{detail}",
        "cancel_deadline": reserve.cancel_deadline(slot).strftime("%Y-%m-%d"),
    }
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        ok = r.status == 200
    print("calendar:", "added" if ok else f"status {r.status}")
    return ok
