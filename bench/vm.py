"""Push samples to VictoriaMetrics (Prometheus text import, explicit millisecond timestamps).

Everything the benchmarks measure lands here, so Grafana can chart it next to the node and GPU
metrics VictoriaMetrics already scrapes.
"""

from __future__ import annotations

import os
import re
import time
import urllib.request

VM_URL = os.environ.get("BENCH_VM_URL", "http://127.0.0.1:8428")


def _esc(v) -> str:
    return str(v).replace("\\", "\\\\").replace("\n", " ").replace('"', '\\"')


def line(name: str, value: float, labels: dict, ts: float | None = None) -> str:
    lab = ",".join(f'{re.sub(r"[^a-zA-Z0-9_]", "_", k)}="{_esc(v)}"' for k, v in sorted(labels.items()) if v is not None)
    return f"{name}{{{lab}}} {float(value)} {int((ts or time.time()) * 1000)}"


def push(samples: list[tuple[str, float, dict]], ts: float | None = None, url: str = VM_URL) -> bool:
    """samples: (metric name, value, labels). Returns False (never raises) if VM is unreachable."""
    body = "\n".join(line(n, v, lab, ts) for n, v, lab in samples if v is not None) + "\n"
    try:
        req = urllib.request.Request(f"{url}/api/v1/import/prometheus", data=body.encode(), method="POST")
        urllib.request.urlopen(req, timeout=5).read()
        return True
    except Exception:
        return False
