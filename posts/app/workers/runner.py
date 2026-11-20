"""Runs the workers forever in background threads, with heartbeats, pause switch and run-now triggers."""
import threading
import time
import traceback

from .. import db
from ..integrations.buffer import RateLimited
from . import discover, manual, monitor, publish, render

_last_limit_log: dict[str, float] = {}

WORKERS = {
    # name: (function, seconds between runs)
    "discover": (discover.run, 20 * 60),
    "render": (render.run, 3 * 60),
    "publish": (publish.run, 5 * 60),
    "monitor": (monitor.run, 10 * 60),
    "manual": (manual.run, 5 * 60),
}

_wake = {name: threading.Event() for name in WORKERS}


def trigger(name: str) -> bool:
    if name not in _wake:
        return False
    _wake[name].set()
    return True


def _loop(name: str, fn, every: int):
    time.sleep(5)
    while True:
        forced = _wake[name].is_set()
        _wake[name].clear()
        if forced or not db.settings().get("workers_paused"):
            db.q("""INSERT INTO heartbeats(worker, last_start, runs) VALUES (%s, now(), 1)
                    ON CONFLICT (worker) DO UPDATE SET last_start=now(), runs=heartbeats.runs+1""", (name,))
            try:
                busy = fn()
                db.q("UPDATE heartbeats SET last_ok=now() WHERE worker=%s", (name,))
            except RateLimited as e:
                busy = False
                # expected and self-healing: note it once an hour, not on every run
                if time.time() - _last_limit_log.get(name, 0) > 3600:
                    _last_limit_log[name] = time.time()
                    db.log(name, str(e), "warn")
            except Exception as e:
                busy = False
                db.q("UPDATE heartbeats SET last_error_at=now(), last_error=%s WHERE worker=%s", (str(e)[:1000], name))
                db.log(name, f"crashed: {e}\n{traceback.format_exc()[-1500:]}", "error")
            if name == "render" and busy:
                continue  # more to render: go again right away
        _wake[name].wait(every)


def start():
    for name, (fn, every) in WORKERS.items():
        threading.Thread(target=_loop, args=(name, fn, every), name=f"worker-{name}", daemon=True).start()
