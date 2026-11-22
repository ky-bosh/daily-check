import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone

sys.path.insert(0, "/app")

JST = timezone(timedelta(hours=9))
# Cadence (the scheduler fires every minute; this decides which minutes really run)
PEAK_HOURS = range(9, 21)   # 09:00-20:59 on Sat/Sun/holidays -> every 2 min
PEAK_EVERY = 2
NORMAL_EVERY = 5            # otherwise 06:00-23:55 -> every 5 min
LOCK_KEY = "lock.json"
LOCK_SECONDS = 290          # a run never lasts longer than the 300 s Lambda timeout


def is_peak_day(d):
    if d.weekday() >= 5:
        return True
    try:
        import jpholiday
        return jpholiday.is_holiday(d)
    except Exception:
        return False


def due(now):
    if now.hour < 6:
        return False
    if is_peak_day(now.date()) and now.hour in PEAK_HOURS:
        return now.minute % PEAK_EVERY == 0
    return now.minute % NORMAL_EVERY == 0


def _s3():
    import boto3
    return boto3.client("s3"), os.environ.get("STATE_BUCKET")


def acquire_lock():
    """Skip if another run is still going (prevents double notifications)."""
    s3, bucket = _s3()
    if not bucket:
        return True
    try:
        until = json.loads(s3.get_object(Bucket=bucket, Key=LOCK_KEY)["Body"].read()).get("until", 0)
        if until > time.time():
            return False
    except Exception:
        pass
    s3.put_object(Bucket=bucket, Key=LOCK_KEY, Body=json.dumps({"until": time.time() + LOCK_SECONDS}).encode())
    return True


def release_lock():
    s3, bucket = _s3()
    if bucket:
        s3.put_object(Bucket=bucket, Key=LOCK_KEY, Body=b'{"until": 0}')


FAIL_KEY = "failures.json"
FAIL_ALERT_AT = 3   # consecutive failed checks before an alert mail


def _read_fail():
    s3, bucket = _s3()
    if not bucket:
        return None
    try:
        return json.loads(s3.get_object(Bucket=bucket, Key=FAIL_KEY)["Body"].read())
    except Exception:
        return {"count": 0, "first": "", "alerted": False}


def _write_fail(state):
    s3, bucket = _s3()
    if bucket:
        s3.put_object(Bucket=bucket, Key=FAIL_KEY, Body=json.dumps(state, ensure_ascii=False).encode())


def record_result(monitor, ok, stage=""):
    """Count consecutive failures; mail once when it reaches FAIL_ALERT_AT, and once on recovery."""
    try:
        st = _read_fail()
        if st is None:
            return
        now = datetime.now(JST).strftime("%m/%d %H:%M")
        if ok:
            if st.get("alerted"):
                monitor.send("【空き監視】復旧しました",
                             f"監視が正常に戻りました（{now}）。\n失敗が続いていた期間：{st.get('first')} 〜 {now}\n"
                             "この間に出た空きは見逃している可能性があります。")
            if st.get("count"):
                _write_fail({"count": 0, "first": "", "alerted": False})
            return
        st["count"] = st.get("count", 0) + 1
        st["first"] = st.get("first") or now
        st["last_stage"] = stage
        if st["count"] >= FAIL_ALERT_AT and not st.get("alerted"):
            monitor.send("【空き監視】失敗が続いています",
                         f"空きの確認が{st['count']}回続けて失敗しています（{st['first']} 〜 {now}）。\n"
                         f"止まった段階：{stage}\n\nこの間は空きを見逃している可能性があります。\n"
                         "サイトの不調・メンテナンス、または画面の変更が考えられます。\n"
                         "復旧したら「復旧しました」のメールが届きます。")
            st["alerted"] = True
        _write_fail(st)
    except Exception as e:
        print("fail-alert: error", type(e).__name__)


def handler(event, context):
    event = event or {}
    manual = bool(event.get("test_mail") or event.get("test_notify") or event.get("test_calendar") or event.get("force"))
    if event.get("scheduled") and not manual:
        # round to the nearest minute: the scheduler may fire a few seconds late
        now = datetime.now(JST) + timedelta(seconds=20)
        if not due(now):
            return {"skipped": "not due"}
    if event.get("test_mail"):
        os.environ["TEST_MAIL"] = "true"
    else:
        os.environ.pop("TEST_MAIL", None)
    if not (event.get("test_notify") or event.get("test_reserve") or event.get("test_calendar")) and not acquire_lock():
        print("skip: previous run still going")
        return {"skipped": "locked"}

    import monitor  # reads settings from Lambda environment variables

    monitor.TEST_NOTIFY = bool(event.get("test_notify"))
    monitor.ACTIVE_ROOM = None
    try:
        if event.get("test_calendar"):
            import calendar_hook
            slot = ["2026年12月31日(木)", "1番目", "9:00～11:00", "○"]
            ok = calendar_hook.add(slot, monitor.FACILITY, monitor.ROOM, "動作確認用のテスト予定です。確認後に削除します。", "【テスト】")
            return {"test_calendar": True, "added": ok}
        if event.get("test_reserve"):
            return monitor.run_test_reserve(event["facility"], event.get("room", monitor.ROOM))
        result = monitor.main()
        if not (result or {}).get("skipped"):
            record_result(monitor, True)
        return result
    except Exception as e:
        print("error:", type(e).__name__, "at", monitor.STAGE)
        if not (event.get("test_notify") or event.get("test_reserve") or event.get("test_calendar")):
            record_result(monitor, False, monitor.STAGE)
        raise RuntimeError(f"check failed at {monitor.STAGE}: {type(e).__name__}") from None
    finally:
        if not (event.get("test_notify") or event.get("test_reserve")):
            try:
                release_lock()
            except Exception:
                pass
