"""Automatic booking for one time slot.

Modes (env RESERVE_MODE): off / dry (stop just before the final 申込 button) / live.
Credentials: RESERVE_ID / RESERVE_PW (GitHub Secrets -> Lambda env). Never logged.
"""
import json
import os
import re
from datetime import datetime, timedelta

from playwright.sync_api import sync_playwright

import monitor as m

FORM = {  # values typed into the 詳細申請 form
    "利用人数": os.environ.get("RESERVE_PEOPLE", "2"),
    "使用目的": os.environ.get("RESERVE_PURPOSE", "テニス"),
    "申請項目": os.environ.get("RESERVE_EVENT", "テニス練習"),
}
KNOWN_FIELDS = set(FORM)
MIN_DAYS_AHEAD = int(os.environ.get("RESERVE_MIN_DAYS", "10"))
# Web booking is possible up to 7 days before, and free cancellation also ends 7 days before
# (same weekday). Booking only slots >= 10 days ahead leaves >= 3 days to decide.
FREE_CANCEL_DAYS = 7
CANCEL_RULE = re.compile(r"キャンセルは、?ご利用日の1週間前")  # must appear on the confirm page
BOOKINGS_KEY = "bookings.json"


# ---------- choosing a slot ----------

def slot_date(slot):
    mt = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日", slot[0])
    return datetime(int(mt.group(1)), int(mt.group(2)), int(mt.group(3)), tzinfo=m.JST) if mt else None


def slot_start_hour(slot):
    mt = re.match(r"(\d{1,2}):", slot[2])
    return int(mt.group(1)) if mt else None


def priority(slot):
    """1 Sun night, 2 Sun day, 3 Sat night, 4 Sat 9-11, 5 others."""
    d, h = slot_date(slot), slot_start_hour(slot)
    if d is None or h is None:
        return 9
    sun, sat = d.weekday() == 6, d.weekday() == 5
    if sun and h >= 17:
        return 1
    if sun and 11 <= h < 17:
        return 2
    if sat and h >= 17:
        return 3
    if sat and h == 9:
        return 4
    return 5


def week_key(d):
    y, w, _ = d.isocalendar()  # Mon-Sun weeks: a Saturday and its Sunday share a key
    return f"{y}-W{w:02d}"


def load_bookings():
    if not m.STATE_BUCKET:
        return []
    import boto3
    try:
        return json.loads(boto3.client("s3").get_object(Bucket=m.STATE_BUCKET, Key=BOOKINGS_KEY)["Body"].read())
    except Exception as e:
        if "NoSuchKey" in str(e) or "NoSuchKey" in type(e).__name__:
            return []
        raise


def save_bookings(rows):
    if not m.STATE_BUCKET:
        return
    import boto3
    boto3.client("s3").put_object(Bucket=m.STATE_BUCKET, Key=BOOKINGS_KEY,
                                  Body=json.dumps(rows, ensure_ascii=False).encode())


def pick(slots, bookings, now=None):
    """Best bookable slot: >= 7 days ahead, time known, one per Sat/Sun week."""
    now = now or datetime.now(m.JST)
    limit = (now + timedelta(days=MIN_DAYS_AHEAD)).date()
    taken_weeks = {b["week"] for b in bookings}
    ok = []
    for s in slots:
        d = slot_date(s)
        if d is None or s[2] == "時間帯未取得" or not s[1]:
            continue
        if d.date() < limit or week_key(d) in taken_weeks:
            continue
        ok.append(s)
    ok.sort(key=lambda s: (priority(s), slot_date(s), slot_start_hour(s)))
    return ok[0] if ok else None


# ---------- browser steps ----------

class Stop(Exception):
    """Stopped on purpose (unknown form, login failure ...). Message is safe to show the user."""


def login(page):
    m.stage("login")
    page.goto(m.BASE, wait_until="load")
    page.wait_for_timeout(1500)
    if page.locator("#userID").count() == 0:
        m.postback(page, "login")
    uid, pw = os.environ.get("RESERVE_ID", ""), os.environ.get("RESERVE_PW", "")
    if not uid or not pw:
        raise Stop("IDまたはパスワードがLambdaに渡っていません（GitHub Secrets の RESERVE_ID / RESERVE_PW を確認し、deploy-aws を実行し直してください）")
    page.fill("#userID", uid)
    page.fill("#passWord", pw)
    m.postback(page, "nextlogin")
    page.wait_for_timeout(1500)
    if page.locator("#passWord").count() and page.locator("#passWord").is_visible():
        body = page.inner_text("body")
        hints = [x.strip() for x in body.splitlines()
                 if re.search(r"エラー|正しく|誤り|違い|ロック|無効|期限|入力してください|できません", x) and len(x.strip()) < 80]
        diag = (f"\n画面：{page.url.split('/')[-1]}\nID文字数：{len(uid)} / パスワード文字数：{len(pw)}"
                f"\n画面のメッセージ：{' / '.join(hints[:5]) or '（なし）'}")
        raise Stop("ログインできませんでした" + diag)


def goto_month_with(page, ymd):
    for _ in range(m.MAX_PAGES):
        if page.locator(f"input[name=checkdate][value^='{ymd}']").count():
            return
        m.postback(page, "period", "next")
    raise Stop("予約したい日がカレンダーに見つかりませんでした")


def select_day(page, ymd, room):
    m.stage("book-select-day")
    ok = page.evaluate(
        """([ymd, room]) => {
        for (const tr of document.querySelectorAll('table.calendar tr')) {
            const name = tr.querySelector('td.shisetsu');
            if (!name || !name.innerText.includes(room)) continue;
            const i = [...tr.querySelectorAll('input[name=checkdate]')].find(i => i.value.startsWith(ymd));
            if (!i) return false;
            if (!i.checked) i.nextElementSibling.click();
            return i.checked;
        }
        return false;
    }""",
        [ymd, room],
    )
    if not ok:
        raise Stop("予約したい日を選べませんでした（すでに埋まった可能性）")


def select_time(page, court_no, time_label):
    m.stage("book-select-time")
    ok = page.evaluate(
        """([n, t]) => {
        for (const tb of document.querySelectorAll('table.calendar')) {
            const heads = [...tb.querySelectorAll('thead th')]
                .filter(th => !th.classList.contains('shisetsu') && !th.classList.contains('teiin'))
                .map(th => th.innerText.replace(/\\s+/g, ''));
            const k = heads.indexOf(t);
            if (k < 0) continue;
            const tr = tb.querySelectorAll('tbody tr')[n - 1];
            if (!tr) continue;
            const td = [...tr.querySelectorAll('td')].slice(2)[k];
            const i = td && td.querySelector('input[name=checktime]');
            if (!i) return false;
            if (!i.checked) (i.nextElementSibling || i).click();
            return i.checked;
        }
        return false;
    }""",
        [court_no, time_label],
    )
    if not ok:
        raise Stop("予約したい時間帯を選べませんでした（すでに埋まった可能性）")


def fill_form(page):
    """Fill the 詳細申請 form. Stop if it asks for something we do not know."""
    m.stage("book-form")
    info = page.evaluate(
        """(labels) => {
        const req = [];
        // headings that carry a 必須 badge
        for (const el of document.querySelectorAll('*')) {
            if (el.children.length > 3) continue;
            const t = (el.innerText || '').trim();
            if (/必須$/.test(t) && t.length < 30) req.push(t.replace(/\\s*必須$/, '').trim());
        }
        const mark = (label, key) => {
            const head = [...document.querySelectorAll('*')].find(e =>
                e.children.length <= 3 && (e.innerText || '').trim().replace(/\\s*必須$/, '').trim() === label);
            if (!head) return false;
            let box = head;
            for (let i = 0; i < 6 && box; i++) {
                box = box.parentElement;
                if (box && box.querySelector('input:not([type=hidden]), textarea, select, label, button, a.btn')) break;
            }
            if (!box) return false;
            box.setAttribute('data-auto', key);
            return true;
        };
        return {req: [...new Set(req)], found: labels.map(l => [l, mark(l, l)])};
    }""",
        list(KNOWN_FIELDS),
    )
    unknown = [r for r in info["req"] if r and r not in KNOWN_FIELDS]
    missing = [l for l, f in info["found"] if not f]
    if unknown or missing:
        raise Stop(f"申込画面に想定外の記入欄がありました（未対応: {unknown or '-'} / 見つからない: {missing or '-'}）")
    page.locator("[data-auto='利用人数'] input:not([type=hidden])").first.fill(FORM["利用人数"])
    page.locator(f"[data-auto='使用目的'] :text-is('{FORM['使用目的']}')").first.click()
    page.locator("[data-auto='申請項目'] input:not([type=hidden]), [data-auto='申請項目'] textarea").first.fill(FORM["申請項目"])


def click_button(page, text):
    btn = page.locator(f"a:text-is('{text}'), input[type=button][value='{text}'], input[type=submit][value='{text}'], button:text-is('{text}')").first
    if btn.count() == 0:
        btn = page.get_by_text(text, exact=True).first
    with page.expect_navigation(wait_until="load"):
        btn.click()


def clean_tmp():
    """Remove leftover Chromium profiles/shared memory files from earlier browsers in this container."""
    import glob
    import shutil
    for pth in glob.glob("/tmp/playwright*") + glob.glob("/tmp/.org.chromium.*") + glob.glob("/tmp/.com.google.*"):
        shutil.rmtree(pth, ignore_errors=True) if os.path.isdir(pth) else os.remove(pth)


def book(slot, facility, room, mode):
    """Returns dict(result=..., detail=page text)."""
    try:
        clean_tmp()
    except Exception:
        pass
    ymd = slot_date(slot).strftime("%Y%m%d")
    court = int(re.match(r"(\d+)", slot[1]).group(1))
    saved = (m.FACILITY, m.ROOM)
    m.FACILITY, m.ROOM = facility, room
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(args=m.CHROMIUM_ARGS)
            page = browser.new_page(locale="ja-JP")
            page.set_default_timeout(30000)
            page.on("dialog", lambda d: d.accept())  # e.g. 「同じ申請内容にしますか？」
            login(page)
            m.open_calendar(page)
            goto_month_with(page, ymd)
            select_day(page, ymd, room)
            m.stage("book-to-times")
            m.postback(page, "next")
            select_time(page, court, slot[2])
            m.stage("book-to-form")
            m.postback(page, "next")
            if "詳細申請" not in page.inner_text("body"):
                raise Stop("申込画面（詳細申請）に進めませんでした")
            fill_form(page)
            m.stage("book-confirm")
            click_button(page, "確定")
            body = page.inner_text("body")
            if "申込" not in body:
                raise Stop("内容確認の画面に進めませんでした")
            confirm = summarize(body)
            if mode == "live" and not CANCEL_RULE.search(body.replace("１", "1")):
                browser.close()
                raise Stop("内容確認画面のキャンセル規定が想定（利用日の1週間前まで無料）と違うため、申込ボタンを押さずに止めました。\n\n画面の表示：\n" + confirm)
            if mode != "live":
                browser.close()
                return {"result": "dry", "detail": confirm}
            m.stage("book-submit")
            click_button(page, "申込")
            body = page.inner_text("body")
            browser.close()
            if "完了" not in body:
                raise Stop("申込ボタンを押しましたが、完了画面を確認できませんでした。予約状況をサイトでご確認ください")
            return {"result": "booked", "detail": summarize(body)}
    finally:
        m.FACILITY, m.ROOM = saved


def summarize(body):
    lines = [x.strip() for x in body.splitlines() if x.strip()]
    keep = [x for x in lines if re.search(r"施設|利用日|時間帯|予約番号|受付番号|申込番号|料金|金額|支払|期限|人数|目的|行事|キャンセル|\d{4}/\d{1,2}/\d{1,2}|～", x)]
    return "\n".join(keep[:40])


def cancel_deadline(slot):
    return slot_date(slot) - timedelta(days=FREE_CANCEL_DAYS)
