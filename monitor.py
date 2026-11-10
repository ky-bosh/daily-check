"""Availability checker.

Settings come from the TARGET secret: "URL|CATEGORY|FACILITY|ROOM".
Mail settings: GMAIL_USER, GMAIL_APP_PASSWORD, MAIL_TO.
Logs show counts only.
"""
import json
import os
import smtplib
import sys
from datetime import datetime, timezone, timedelta
from email.mime.text import MIMEText
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE, CATEGORY, FACILITY, ROOM = (s.strip() for s in os.environ["TARGET"].split("|"))
STATE_FILE = Path(os.environ.get("STATE_FILE", ".state/state.json"))
STATE_BUCKET = os.environ.get("STATE_BUCKET")  # set on AWS: state is kept in S3
STATE_KEY = "state.json"
CHROMIUM_ARGS = os.environ.get("CHROMIUM_ARGS", "").split()
MAX_PAGES = 3
JST = timezone(timedelta(hours=9))
YOUBI = "月火水木金土日"


def postback(page, target, arg=""):
    with page.expect_navigation(wait_until="load"):
        page.evaluate(f"__doPostBack('{target}','{arg}')")


STAGE = "start"


def stage(name):
    global STAGE
    STAGE = name


def open_calendar(page):
    stage("open")
    page.goto(BASE, wait_until="load")
    stage("category")
    btn = page.locator(f"input[type=button][value='{CATEGORY}'], button:text-is('{CATEGORY}')").first
    btn.wait_for(state="visible")
    with page.expect_navigation(wait_until="load"):
        btn.click()
    stage("facility")
    page.locator("label", has_text=FACILITY).first.click()
    postback(page, "next")
    stage("calendar")
    page.evaluate(
        """() => {
        ['checkYobi6','checkYobi7','checkYobi8'].forEach(id => document.getElementById(id).checked = true);
        document.getElementById('radioPeriod1month').checked = true;
    }"""
    )
    with page.expect_navigation(wait_until="load"):
        page.click("#btnHyoji")


TEST_NOTIFY = False  # set by the Lambda handler for an end-to-end test
ACTIVE_ROOM = None


def read_days(page):
    """Return [(yyyymmdd, value-rest, mark)] for the watched room.
    In test-notify mode, switch once to another room of the same facility that has openings."""
    global ACTIVE_ROOM
    rows = page.evaluate(
        """() => {
        const out = [];
        for (const tr of document.querySelectorAll('table.calendar tr')) {
            const name = tr.querySelector('td.shisetsu');
            if (!name) continue;
            const cells = [...tr.querySelectorAll('input[name=checkdate]')].map(i =>
                [i.value.slice(0, 8), i.value.slice(8), i.nextElementSibling.innerText.trim()]);
            if (cells.length) out.push([name.innerText.trim(), cells]);
        }
        return out;
    }"""
    )
    if ACTIVE_ROOM is None:
        ACTIVE_ROOM = ROOM
        if TEST_NOTIFY:
            for name, cells in rows:
                if ROOM not in name and any(m in ("○", "△") for _, _, m in cells):
                    ACTIVE_ROOM = name
                    break
    for name, cells in rows:
        if ACTIVE_ROOM in name:
            return cells
    return []


def read_slots(page):
    return page.evaluate(
        """() => {
        const out = [], seen = new Set();
        for (const tb of document.querySelectorAll('table.calendar')) {
            const head = tb.querySelector('thead th.shisetsu');
            if (!head) continue;
            const times = [...tb.querySelectorAll('thead th')]
                .filter(th => !th.classList.contains('shisetsu') && !th.classList.contains('teiin'))
                .map(th => th.innerText.replace(/\\s+/g, ''));
            let n = 0;
            for (const tr of tb.querySelectorAll('tbody tr')) {
                n++;
                const tds = [...tr.querySelectorAll('td')].slice(2);
                tds.forEach((td, k) => {
                    const mark = td.innerText.trim();
                    const key = head.innerText + n + times[k];
                    if ((mark === '○' || mark === '△') && !seen.has(key)) {
                        seen.add(key);
                        out.push([head.innerText.trim(), `${n}番目`, times[k], mark]);
                    }
                });
            }
        }
        return out;
    }"""
    )


def fmt_day(ymd):
    d = datetime.strptime(ymd, "%Y%m%d")
    return f"{d.month}/{d.day}({YOUBI[d.weekday()]})"


def collect():
    """Retry once when the browser itself crashes (seen on Lambda)."""
    try:
        return _collect()
    except Exception as e:
        if type(e).__name__ not in ("TargetClosedError",):
            raise
        print("browser crashed, retrying once at", STAGE)
        global ACTIVE_ROOM
        ACTIVE_ROOM = None
        return _collect()


def _collect():
    with sync_playwright() as p:
        browser = p.chromium.launch(args=CHROMIUM_ARGS)
        page = browser.new_page(locale="ja-JP")
        page.set_default_timeout(30000)

        open_calendar(page)
        pages = []
        for n in range(MAX_PAGES):
            stage(f"read-page{n + 1}")
            days = read_days(page)
            pages.append(days)
            marks = [m for _, _, m in days]
            if not days or "抽選" in marks or all(m == "－" for m in marks[-3:]):
                break
            if n + 1 < MAX_PAGES:  # do not page past the last one we read
                stage(f"next-page{n + 1}")
                postback(page, "period", "next")

        total = sum(len(d) for d in pages)
        print(f"checked: {total} days / {len(pages)} pages")
        if total == 0:
            raise RuntimeError("no data read (page layout may have changed)")

        slots = []
        for idx, days in enumerate(pages):
            avail = [(ymd, rest) for ymd, rest, m in days if m in ("○", "△")]
            if not avail:
                continue
            # fresh session each time (the site keeps screen state per session).
            # Clear cookies instead of opening a new context: safer for single-process Chromium.
            page.context.clear_cookies()
            open_calendar(page)
            stage(f"slots-goto-page{idx + 1}")
            for _ in range(idx):
                postback(page, "period", "next")
            stage("slots-select")
            for ymd, rest in avail:
                page.evaluate(
                    """(v) => {
                    const i = [...document.querySelectorAll('input[name=checkdate]')]
                      .find(i => i.value === v);
                    if (i && !i.checked) i.nextElementSibling.click();
                }""",
                    ymd + rest,
                )
            stage("slots")
            try:
                postback(page, "next")
                got = read_slots(page)
            except Exception:
                got = []
            if got:
                slots.extend(got)
            else:
                slots.extend([(fmt_day(y), "", "時間帯未取得", "○/△") for y, _ in avail])
        browser.close()
    return slots


def send(subject, body):
    user = os.environ["GMAIL_USER"]
    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = user
    msg["To"] = os.environ["MAIL_TO"]
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, os.environ["GMAIL_APP_PASSWORD"])
        s.send_message(msg)


def line(slot):
    d, c, t, m = slot
    return f"・{d} {t} {c} {m}".replace("  ", " ")


def send_new(new):
    body = (
        f"{FACILITY} {ROOM}に新しい空きが出ました（土日祝）。\n\n"
        + "\n".join(line(s) for s in new)
        + f"\n\n予約はこちら（ログインして申込）：\n{BASE}\n\n"
        "※空きは先着順です。通知後に埋まっている場合があります。\n"
        "※△は「施設に問合せ」の枠です。"
    )
    first = new[0]
    subject = f"【空き通知】{FACILITY} {ROOM} {first[0]} {first[2]}" + (f" ほか{len(new)-1}件" if len(new) > 1 else "")
    send(subject, body)


def send_test(now):
    body = (
        f"空き監視のテストメールです（{datetime.now(JST):%Y-%m-%d %H:%M}）。\n"
        "このメールが届いていれば、空きが出たときの通知も届きます。\n\n"
        f"現在の空き（{FACILITY} {ROOM}・土日祝）：{len(now)}件\n"
        + ("\n".join(line(s) for s in now) if now else "なし")
    )
    send("【空き通知】テスト送信", body)


def load_state():
    if STATE_BUCKET:
        import boto3
        try:
            body = boto3.client("s3").get_object(Bucket=STATE_BUCKET, Key=STATE_KEY)["Body"].read()
            return json.loads(body)
        except Exception as e:
            if "NoSuchKey" in type(e).__name__ or "NoSuchKey" in str(e):
                return []
            raise
    return json.loads(STATE_FILE.read_text("utf-8")) if STATE_FILE.exists() else []


def save_state(now):
    data = json.dumps(now, ensure_ascii=False)
    if STATE_BUCKET:
        import boto3
        boto3.client("s3").put_object(Bucket=STATE_BUCKET, Key=STATE_KEY, Body=data.encode("utf-8"))
        return
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(data, "utf-8")


def run_test_notify():
    """End-to-end test on another room that has openings. Does not touch the saved state."""
    global ACTIVE_ROOM
    ACTIVE_ROOM = None
    slots = [list(s) for s in collect()]
    print(f"test-notify: switched={ACTIVE_ROOM != ROOM} open: {len(slots)}")
    if not slots:
        return {"test_notify": True, "open": 0, "sent": False}
    body = (
        "【テスト】通知メールの動作確認です。本番の監視対象ではなく、"
        f"同じ施設で空きのある「{ACTIVE_ROOM}」で、本番と同じ処理を通しました。\n\n"
        + "\n".join(line(x) for x in slots[:15])
        + (f"\n…ほか{len(slots)-15}件" if len(slots) > 15 else "")
        + f"\n\n予約サイト：\n{BASE}"
    )
    first = slots[0]
    send(f"【テスト】【空き通知】{FACILITY} {ACTIVE_ROOM} {first[0]} {first[2]} ほか", body)
    print("test notify sent")
    return {"test_notify": True, "open": len(slots), "sent": True}


def main():
    if TEST_NOTIFY:
        return run_test_notify()
    hour = datetime.now(JST).hour
    if hour < 6 and os.environ.get("TEST_MAIL") != "true":
        print("skip: night hours")
        return {"skipped": True}
    now = [list(s) for s in collect()]
    prev = load_state()
    new = [s for s in now if s not in prev]
    print(f"{datetime.now(JST):%Y-%m-%d %H:%M} open: {len(now)} / new: {len(new)}")
    if new:
        send_new(new)
        print("notified")
    if os.environ.get("TEST_MAIL") == "true":
        send_test(now)
        print("test mail sent")
    save_state(now)
    return {"open": len(now), "new": len(new)}


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print("error:", type(e).__name__, "at", STAGE, file=sys.stderr)
        sys.exit(1)
