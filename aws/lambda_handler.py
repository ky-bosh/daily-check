import os
import sys

sys.path.insert(0, "/app")


def handler(event, context):
    event = event or {}
    if event.get("test_mail"):
        os.environ["TEST_MAIL"] = "true"
    else:
        os.environ.pop("TEST_MAIL", None)
    import monitor  # reads settings from Lambda environment variables

    monitor.TEST_NOTIFY = bool(event.get("test_notify"))
    monitor.ACTIVE_ROOM = None

    try:
        return monitor.main()
    except Exception as e:
        print("error:", type(e).__name__, "at", monitor.STAGE)
        raise RuntimeError(f"check failed at {monitor.STAGE}: {type(e).__name__}") from None
