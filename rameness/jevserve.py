"""Set up and run JEV's local decision servers: Laya and Kev.

One server per machine is shared by the manager and every fleet worker, so the model is in
memory once. ``rameness up`` starts the configured one if it isn't running.

* **laya**: the ``laya`` package in rameness's own environment (``pip install "laya[serve]"``),
  which also enables the in-process ``laya-local`` backend. Serves the ``typed-decisions``
  checkpoint on ``127.0.0.1:8000``.
* **kev**: Jared Palmer's Kev, cloned into ``~/.rameness/jev/kev`` with its own pinned Python and
  PyTorch (it needs Python 3.12-3.13 and ``torch<2.9``, which must not leak into rameness's
  environment). Serves ``jaredpalmer/kev-4b`` on ``127.0.0.1:8008``.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import user_home
from .jev import SystemOneJev, laya_running

KEV_REPO = "https://github.com/jaredpalmer/kev.git"


def root() -> Path:
    return user_home() / "jev"


def url(name: str, jc: dict) -> str:
    return jc.get(f"{name}_url") or SystemOneJev.PRESETS[name]["url"]


def port(name: str, jc: dict) -> int:
    return int(url(name, jc).split(":")[2].split("/")[0])


def installed(name: str) -> bool:
    if name == "laya":
        return shutil.which("laya-serve", path=str(Path(sys.executable).parent)) is not None
    return (root() / "kev" / "src" / ".venv" / "bin" / "python").exists()


def setup(name: str, out=print) -> None:
    """Install a local decision model. Downloads code now and weights on first start."""
    if name == "laya":
        out('installing Laya into rameness\'s environment: pip install "laya[serve]" (pulls PyTorch)')
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "laya[serve]"], check=True)
        return
    if name != "kev":
        raise ValueError(f"unknown decision model {name!r}: laya or kev")
    src, tools = root() / "kev" / "src", root() / "kev" / "tools"
    if not (src / ".git").exists():
        out(f"cloning Kev into {src}")
        src.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", "-q", "--depth", "1", KEV_REPO, str(src)], check=True)
    uv = shutil.which("uv")
    if not uv:                                     # a private uv, so Kev can fetch the Python it pins
        subprocess.run([sys.executable, "-m", "venv", str(tools)], check=True)
        subprocess.run([str(tools / "bin" / "python"), "-m", "pip", "install", "-q", "uv"], check=True)
        uv = str(tools / "bin" / "uv")
    out("installing Kev's dependencies (its own Python 3.13 + PyTorch; a few GB)")
    subprocess.run([uv, "sync", "-q", "--extra", "serve", "--python", "3.13"], cwd=src, check=True)


def _cpu_bf16() -> bool:
    try:
        flags = Path("/proc/cpuinfo").read_text()
    except OSError:
        return False
    return "avx512_bf16" in flags or "amx_bf16" in flags


def command(name: str, jc: dict) -> tuple[list[str], dict, Path | None]:
    env = dict(os.environ)
    device = jc.get("serve_device")               # None: GPU if one is visible, else CPU
    if name == "laya":
        env.update(LAYA_HOST="127.0.0.1", LAYA_PORT=str(port(name, jc)), LAYA_PRELOAD="1",
                   LAYA_MODELS=jc.get("laya_model") or "typed-decisions")
        if device:
            env["LAYA_DEVICE"] = device
        return [str(Path(sys.executable).parent / "laya-serve")], env, None
    src = root() / "kev" / "src"
    if device == "cpu":
        env["CUDA_VISIBLE_DEVICES"] = ""
    dtype = jc.get("kev_dtype") or ("bf16" if _cpu_bf16() else None)
    if dtype and "KEV_DTYPE" not in env:
        # Kev serves fp32 on CPU by default: 4B parameters take ~17 GB of RAM. bf16 halves that and, on CPUs
        # with native bf16 (AVX512-BF16 / AMX), measured 2.3x faster with the same choices (probabilities
        # within 0.003). GPUs already get bf16 from Kev itself.
        env["KEV_DTYPE"] = dtype
    return ([str(src / ".venv" / "bin" / "python"), "-m", "kev.serve", "--run",
             jc.get("kev_checkpoint") or "jaredpalmer/kev-4b", "--port", str(port(name, jc))], env, src)


def _pidfile(name: str) -> Path:
    return root() / f"{name}.pid"


def _pid(name: str) -> int | None:
    try:
        pid = int(_pidfile(name).read_text())
        os.kill(pid, 0)
        return pid
    except (OSError, ValueError):
        return None


def up(name: str, jc: dict, wait: float = 900, out=print) -> str:
    """Start ``name`` in the background (no-op if it already answers). Waits until it answers:
    the first start downloads weights (Laya ~2 GB, Kev-4B ~9 GB)."""
    if laya_running(url(name, jc)):
        return f"{name} already answering at {url(name, jc)}"
    if not installed(name):
        raise RuntimeError(f"{name} is not installed: run `rameness jev setup {name}`")
    cmd, env, cwd = command(name, jc)
    root().mkdir(parents=True, exist_ok=True)
    log = root() / f"{name}.log"
    with log.open("ab") as fh:
        p = subprocess.Popen(cmd, env=env, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
    _pidfile(name).write_text(str(p.pid))
    out(f"starting {name} (log: {log})")
    t0 = time.time()
    while time.time() - t0 < wait:
        if p.poll() is not None:
            tail = log.read_text(errors="replace")[-1500:]
            raise RuntimeError(f"{name} exited with code {p.returncode}:\n{tail}")
        if laya_running(url(name, jc), timeout=5):
            return f"{name} answering at {url(name, jc)} after {time.time() - t0:.0f}s"
        time.sleep(3)
    return f"{name} still loading after {wait:.0f}s; it keeps starting in the background (log: {log})"


def down(name: str) -> str:
    pid = _pid(name)
    if not pid:
        return f"{name}: not started by rameness"
    os.killpg(pid, signal.SIGTERM)
    for _ in range(40):                            # a busy server can sit on SIGTERM; don't leave two running
        time.sleep(0.5)
        try:
            os.kill(pid, 0)
        except OSError:
            break
    else:
        os.killpg(pid, signal.SIGKILL)
    _pidfile(name).unlink(missing_ok=True)
    return f"{name}: stopped"


def status(jc: dict) -> list[str]:
    from .jev import laya_importable, resolve_backend
    lines = []
    for name in ("kev", "laya"):
        state = "answering" if laya_running(url(name, jc)) else ("installed" if installed(name) else "not installed")
        lines.append(f"{name:9s} {state:14s} {url(name, jc)}")
    lines.append(f"laya-local {'available' if laya_importable() else 'not installed':14s} (in-process)")
    key = jc.get("typesafe_key_env", "TYPESAFE_API_KEY")
    lines.append(f"typesafe  {'key set' if os.environ.get(key) else 'no key':14s} ${key} (hosted subscription)")
    lines.append(f"decisions now use: {resolve_backend(jc)}")
    return lines


def ensure(jc: dict, out=print) -> None:
    """For `rameness up`: start the configured local server if it's installed and not running."""
    name = jc.get("serve")
    if name in ("kev", "laya") and installed(name) and not laya_running(url(name, jc)):
        try:
            out(up(name, jc, out=out))
        except Exception as e:                    # never block the fleet on the decision server
            out(f"could not start {name}: {e}")
