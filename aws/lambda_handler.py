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
LOCK_SECONDS = 170          # a run never lasts longer than the 180 s Lambda timeout


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


def handler(event, context):
    event = event or {}
    manual = bool(event.get("test_mail") or event.get("test_notify") or event.get("force"))
    if event.get("scheduled") and not manual:
        # round to the nearest minute: the scheduler may fire a few seconds late
        now = datetime.now(JST) + timedelta(seconds=20)
        if not due(now):
            return {"skipped": "not due"}
    if event.get("test_mail"):
        os.environ["TEST_MAIL"] = "true"
    else:
        os.environ.pop("TEST_MAIL", None)
    if not event.get("test_notify") and not acquire_lock():
        print("skip: previous run still going")
        return {"skipped": "locked"}

    import monitor  # reads settings from Lambda environment variables

    monitor.TEST_NOTIFY = bool(event.get("test_notify"))
    monitor.ACTIVE_ROOM = None
    try:
        return monitor.main()
    except Exception as e:
        print("error:", type(e).__name__, "at", monitor.STAGE)
        raise RuntimeError(f"check failed at {monitor.STAGE}: {type(e).__name__}") from None
    finally:
        if not event.get("test_notify"):
            try:
                release_lock()
            except Exception:
                pass
