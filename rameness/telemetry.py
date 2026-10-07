"""Optional metrics push to VictoriaMetrics (or anything that accepts Prometheus text import).

Off unless ``RAMENESS_TELEMETRY_URL`` or ``telemetry_url`` in config is set, e.g.
``http://127.0.0.1:8428``. Pushes run on a background thread, so a slow or absent metrics
server never delays a decision.
"""

from __future__ import annotations

import json
import os
import queue
import re
import threading
import time
import urllib.request

_q: queue.Queue = queue.Queue(maxsize=10000)
_started = False
_lock = threading.Lock()
LABELS: dict = {}              # added to every sample, e.g. {"run": ..., "harness": "rameness"}
try:                            # RAMENESS_TELEMETRY_LABELS='{"run": "r1"}' tags a whole process's samples
    LABELS.update(json.loads(os.environ.get("RAMENESS_TELEMETRY_LABELS") or "{}"))
except json.JSONDecodeError:
    pass


def url() -> str | None:
    return os.environ.get("RAMENESS_TELEMETRY_URL") or None


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def _line(name: str, value: float, labels: dict, ts: float) -> str:
    lab = ",".join(f'{re.sub(r"[^a-zA-Z0-9_]", "_", k)}="{_esc(v)}"' for k, v in sorted(labels.items()) if v is not None)
    return f"{name}{{{lab}}} {float(value)} {int(ts * 1000)}"


def _worker(target: str) -> None:
    while True:
        batch = [_q.get()]
        while not _q.empty() and len(batch) < 500:
            batch.append(_q.get_nowait())
        try:
            req = urllib.request.Request(f"{target}/api/v1/import/prometheus",
                                         data=("\n".join(batch) + "\n").encode(), method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except Exception:
            pass


def emit(samples: list[tuple[str, float | None, dict]], target: str | None = None) -> None:
    global _started
    target = target or url()
    if not target:
        return
    with _lock:
        if not _started:
            threading.Thread(target=_worker, args=(target,), daemon=True).start()
            _started = True
    now = time.time()
    for name, value, labels in samples:
        if value is not None:
            try:
                _q.put_nowait(_line(name, value, {**LABELS, **labels}, now))
            except queue.Full:
                return


def flush(timeout: float = 3.0) -> None:
    """Give queued samples a moment to leave before the process exits."""
    t = time.time()
    while not _q.empty() and time.time() - t < timeout:
        time.sleep(0.05)
    time.sleep(0.2)
