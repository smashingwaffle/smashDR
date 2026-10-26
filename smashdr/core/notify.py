"""
notify.py - push alerts to your phone through ntfy (https://ntfy.sh, free).

Install the ntfy app (iPhone / Android), subscribe to your topic name, put the
same name in Settings > Phone alerts. Anyone who knows the topic can read it,
so smashDR suggests a long random one.

    send(topic, "LAPD emergency", "TG 1 radio 16078 EMERGENCY", priority="urgent")

Runs in a background thread; never blocks or raises.
"""

import secrets
import threading
import urllib.request

SERVER = "https://ntfy.sh"


def random_topic() -> str:
    return "smashdr-" + secrets.token_urlsafe(12).replace("_", "").replace("-", "")[:16].lower()


def send(topic: str, title: str, message: str, priority: str = "high", tags: str = "warning",
         click: str = None, server: str = SERVER):
    topic = (topic or "").strip().strip("/")
    if not topic:
        return

    def run():
        try:
            req = urllib.request.Request(f"{server.rstrip('/')}/{topic}", data=message.encode("utf-8"),
                                         method="POST")
            # header values must be latin-1; keep titles plain
            req.add_header("Title", title.encode("ascii", "replace").decode("ascii"))
            req.add_header("Priority", priority)
            req.add_header("Tags", tags)
            if click:
                req.add_header("Click", click)
            urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:                      # alerts are best effort
            print("[ntfy] could not send:", e)

    threading.Thread(target=run, name="ntfy", daemon=True).start()
