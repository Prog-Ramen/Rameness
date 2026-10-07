"""Run the Minecraft task across harnesses and models, score each build, push metrics.

    python -m bench.minecraft.run --harnesses rameness,pi,dsh,claude-code --models qwen,occamy,claude

Each run: a fresh working directory, a recording proxy in front of the model (bench.llmproxy),
the harness in headless mode with its tools auto-approved, a wall-clock limit, then the scorer
(bench.minecraft.score). Local-model runs execute as the unprivileged `bench` user, so a model
with a shell can only write inside its own run directory. Runs on the same model server go one at
a time (so speeds are comparable); different servers run in parallel.
"""

from __future__ import annotations

import argparse
import json
import shutil
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from .. import loki, vm
from ..llmproxy import Proxy

TASK = (Path(__file__).parent / "task.md").read_text()
TASK_DIR: Path | None = None        # --task NAME: bench/tasks/NAME (task.md, seed/, score.py); None = Minecraft
SCORER = ["/root/bench/venv/bin/python", "-m", "bench.minecraft.score"]
REPO = Path(__file__).resolve().parents[2]

MODELS = {
    "qwen": {"upstream": "http://127.0.0.1:8033", "id": "qwen3.8-27b", "ctx": 150000, "server": "local-8033",
             "label": "Qwen3.8-27B IQ3_S", "reasoning_budget": 5000},   # unbounded, it thinks through the
                                                                     # whole game in one reply and never acts
    "flashnext": {"upstream": "http://127.0.0.1:8033", "id": "qwen3.8-flash-next-iq3_s-gsq-rco", "ctx": 262144,
                  "server": "local-8033-cmp170hx", "label": "qwen3.8-flash-next-iq3_s-gsq-rco", "reasoning_budget": 5000},
    "exl3": {"upstream": "http://127.0.0.1:5000", "id": "Qwen3.8-Flash-Next-exl3-3.05bpw", "ctx": 196608,
             "server": "local-5000-tabbyapi-cmp170hx", "label": "qwen3.8-flash-next-exl3-3.05bpw", "reasoning_budget": 5000,
             "vision": True,    # TabbyAPI + ExLlamaV3 + MTP: no /props, so the proxy states window and vision
             "props": {"default_generation_settings": {"n_ctx": 196608}, "total_slots": 1, "modalities": {"vision": True}}},
    "exl3-405": {"upstream": "http://127.0.0.1:5000", "id": "Qwen3.8-Flash-Next-exl3-4.05bpw", "ctx": 196608,
                 "server": "local-5000-tabbyapi-cmp170hx", "label": "qwen3.8-flash-next-exl3-4.05bpw", "reasoning_budget": 5000,
                 "vision": True,    # 96 cold experts per layer on the CPU (68 GB of weights on a 64 GB card)
                 "props": {"default_generation_settings": {"n_ctx": 196608}, "total_slots": 1, "modalities": {"vision": True}}},
    "q27bf16": {"upstream": "http://127.0.0.1:8000", "id": "q27", "ctx": 80000, "server": "local-8000-vllm-cmp170hx",
                "label": "qwen3.8-27b-bf16", "reasoning_budget": 5000, "budget_field": "thinking_token_budget",
                "vision": True,    # vLLM + MTP 3; BF16 leaves room for only ~80k tokens of KV on the 64 GB card
                "props": {"default_generation_settings": {"n_ctx": 80000}, "total_slots": 1, "modalities": {"vision": True}}},
    "q27bf16gg": {"upstream": "http://127.0.0.1:8033", "id": "qwen3.8-27b-bf16", "ctx": 262144, "server": "local-8033-cmp170hx-5070ti",
                  "label": "qwen3.8-27b-bf16-gguf", "reasoning_budget": 5000},   # llama.cpp master + built-in MTP, CMP + 5070 Ti
    "q27q4": {"upstream": "http://127.0.0.1:8033", "id": "qwen3.8-27b-ud-q4_k_m", "ctx": 262144, "server": "local-8033-cmp170hx",
              "label": "qwen3.8-27b-unsloth-ud-q4_k_m", "reasoning_budget": 5000},     # llama.cpp master + MTP
    "flashq4": {"upstream": "http://127.0.0.1:8033", "id": "qwen3.8-flash-next-ud-q4_k_xl", "ctx": 262144,
                "server": "local-8033-cmp170hx", "label": "qwen3.8-flash-next-unsloth-ud-q4_k_xl", "reasoning_budget": 5000},
    "exl3-505": {"upstream": "http://127.0.0.1:5000", "id": "Qwen3.8-Flash-Next-exl3-5.05bpw", "ctx": 196608,
                 "server": "local-5000-tabbyapi-cmp170hx", "label": "qwen3.8-flash-next-exl3-5.05bpw", "reasoning_budget": 5000,
                 "vision": True,    # tuned 2026-10-06: config-505-c4096-q4-80.yml (4096 chunks, 4-bit KV, 80 CPU experts/layer)
                 "props": {"default_generation_settings": {"n_ctx": 196608}, "total_slots": 1, "modalities": {"vision": True}}},
    "glm": {"upstream": "http://127.0.0.1:8033", "id": "glm-5.3-flash-gsq-rco-3.0bit", "ctx": 131072, "server": "local-8033-2gpu",
            "label": "glm-5.3-flash-gsq-rco-3.0bit", "reasoning_budget": 5000},   # llama.cpp master, experts partly on CPU
    "glm-reap": {"upstream": "http://127.0.0.1:8033", "id": "glm-5.3-flash-reap50-iq3_m", "ctx": 131072,
                 "server": "local-8033-2gpu", "label": "glm-5.3-flash-reap50-iq3_m", "reasoning_budget": 5000,
                 "tool_adapter": "glm"},   # the REAP50 llama.cpp fork has no GLM tool-call parser
    "q27gptq": {"upstream": "http://127.0.0.1:8000", "id": "qwen3.8-27b", "ctx": 262144, "server": "local-8000-vllm-tnz-cmp170hx",
                "label": "qwen3.8-27b-gptq-w4a16-pearsonkyle-dflash2-15", "max_tokens_cap": 12000,
                "vision": True,   # pearsonkyle GPTQ W4A16 on the TnzGit vLLM stack, DFlash2 15 tokens; V2 runner: no thinking cap
                "props": {"default_generation_settings": {"n_ctx": 262144}, "total_slots": 1, "modalities": {"vision": True}}},
    "ninfer27": {"upstream": "http://127.0.0.1:8090", "id": "qwen3.8-27b", "ctx": 262144, "server": "local-8090-ninfer-cmp170hx",
                 "label": "qwen3.8-27b-ninfer-q4q5q6-dflash2-7", "reasoning_budget": 5000, "budget_field": "thinking_budget",
                 "vision": True,   # ninfer 0.14.2 sm_80 decode-route branch: DFlash2 7 + lm-head-draft, cuBLAS prefill 4096, int8 KV
                 "props": {"default_generation_settings": {"n_ctx": 262144}, "total_slots": 1, "modalities": {"vision": True}}},
    "flashar": {"upstream": "http://127.0.0.1:8000", "id": "Qwen3.8-Flash-Next", "ctx": 262144, "server": "local-8000-vllm-ple-cmp170hx",
                "label": "qwen3.8-flash-next-autoround-3bpw-mtp3", "reasoning_budget": 5000, "budget_field": "thinking_token_budget",
                "vision": True,   # klee100 AutoRound 3bpw on iIIusi0n's PLE-SSD vLLM, MTP 3, 4096-token chunks
                "props": {"default_generation_settings": {"n_ctx": 262144}, "total_slots": 1, "modalities": {"vision": True}}},
    "flashar-cap": {"upstream": "http://127.0.0.1:8000", "id": "Qwen3.8-Flash-Next", "ctx": 262144, "server": "local-8000-vllm-ple-cmp170hx",
                    "label": "qwen3.8-flash-next-autoround-3bpw-mtp3", "max_tokens_cap": 12000,
                    "vision": True,   # same, for a runner that ignores thinking_token_budget
                    "props": {"default_generation_settings": {"n_ctx": 262144}, "total_slots": 1, "modalities": {"vision": True}}},
    "q27tnz": {"upstream": "http://127.0.0.1:8000", "id": "qwen3.8-27b", "ctx": 262144, "server": "local-8000-vllm-tnz-cmp170hx",
               "label": "qwen3.8-27b-w4a16-tnz-dflash2", "max_tokens_cap": 12000,
               "vision": True,   # TnzGit vLLM 0.27.1 patch stack, DFlash2, mixed FP8 KV, prefix cache; its V2 model
                                 # runner rejects thinking_token_budget, so this entry runs without a thinking cap
               "props": {"default_generation_settings": {"n_ctx": 262144}, "total_slots": 1, "modalities": {"vision": True}}},
    "swift15": {"upstream": "http://127.0.0.1:8033", "id": "swift-1.5-qwen3.8-27b-q4_k_m", "ctx": 262144,
                "server": "local-8033-cmp170hx", "label": "swift-1.5-qwen3.8-27b-q4_k_m", "reasoning_budget": 5000},
    "occamy": {"upstream": "http://127.0.0.1:8034", "id": "occamy-1.0", "ctx": 200192, "server": "local-8034",
               "label": "Occamy 1.0", "min_tps": 15},    # ~30 tok/s when healthy; it has degraded to 1-6
    "m9033": {"upstream": "http://127.0.0.1:9033", "id": "local", "ctx": 131072, "server": "local-9033",
              "label": "local:9033"},
    "claude": {"upstream": "https://api.anthropic.com", "id": "claude-opus-5-5", "ctx": 200000, "server": "anthropic",
               "label": "Claude Opus 5.5", "hosted": True},
}
# machine-specific endpoints (other hosts, ports) go in bench/models.local.json, not in git:
# {"occamy": {"upstream": "http://host:8034", "server": "host-8034"}}
_local = Path(__file__).resolve().parents[1] / "models.local.json"
if _local.exists():
    for _k, _v in json.loads(_local.read_text()).items():
        MODELS.setdefault(_k, {}).update(_v)
# every tool pre-approved: Claude Code never stops to ask (it refuses --dangerously-skip-permissions as root)
ALLOWED = ("--allowedTools=Bash,Read,Write,Edit,MultiEdit,Glob,Grep,LS,WebFetch,WebSearch,Task,TodoWrite,"
           "NotebookEdit,BashOutput,KillShell")


RAMENESS_BENCH = Path("/opt/rameness-bench")      # the bench user can't read /root, so it runs an installed copy


def sync_rameness() -> None:
    """Reinstall the bench copy of rameness when it differs from this checkout, so runs test the current code."""
    import fcntl
    with open("/tmp/rameness-bench-sync.lock", "w") as lock:   # two runners starting together must not both install
        fcntl.flock(lock, fcntl.LOCK_EX)
        _sync_rameness()


def _sync_rameness() -> None:
    src = Path(__file__).resolve().parents[2] / "rameness"
    dst = next(RAMENESS_BENCH.glob("lib/python3*/site-packages/rameness"), None)
    stale = dst is None or any(not (dst / f.relative_to(src)).exists() or
                               (dst / f.relative_to(src)).read_bytes() != f.read_bytes() for f in src.rglob("*.py"))
    if stale:
        print("[bench] rameness copy is out of date; reinstalling", flush=True)
        subprocess.run([str(RAMENESS_BENCH / "bin/python3"), "-m", "pip", "install", "--quiet", "--no-deps",
                        "--force-reinstall", str(src.parent)], check=True)


def harness_cmd(h: str, m: dict, proxy: str, run_dir: Path, work: Path, labels: dict) -> tuple[list[str], dict]:
    env = {"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C.UTF-8", "TERM": "dumb"}
    if h == "rameness":
        home = run_dir / "home"
        (home / ".rameness").mkdir(parents=True, exist_ok=True)
        registry = {"auto_propose": False}               # isolated benchmarks never publish to GitHub
        pipeline = os.environ.get("BENCH_SOP_PIPELINE") == "1"
        if pipeline:
            # The whole SOP pipeline runs (learning, sop_save, extension, proposals), but proposals go to
            # local stand-in repos in the run folder instead of GitHub; pulls read the real registry index.
            sinks = run_dir / "sop-sinks"
            for name in ("RamenSOPs.git", "Rameness.git"):
                subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(sinks / name)], check=True)
            registry = {"auto_propose": True, "public": str(sinks / "RamenSOPs.git"),
                        "builtin": str(sinks / "Rameness.git")}
        (home / ".rameness" / "config.json").write_text(json.dumps(
            # Kev on CPU (bf16, ~8 GB RAM) next to the local llama-server
            {"jev": {"backend": "kev", "kev_url": "http://127.0.0.1:8008/v1/systemone"},
             "max_turns": 1000,                           # the wall-clock limit bounds the run, like the others
             "registry": registry,
             "permissions": {"mode": "auto"}, **json.loads(os.environ.get("BENCH_RAMENESS_CONFIG") or "{}")}))
        env.update(HOME=str(home), RAMENESS_TELEMETRY_URL=vm.VM_URL, RAMENESS_TELEMETRY_LABELS=json.dumps(labels))
        return ["/opt/rameness-bench/bin/rameness", "--base-url", f"{proxy}/v1", "--model", m["id"], "-y",
                "run", *([] if pipeline else ["--no-learn"]), TASK], env
    if h == "pi":
        agent = run_dir / "pi-agent"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "models.json").write_text(json.dumps({"providers": {"bench": {
            "baseUrl": f"{proxy}/v1", "api": "openai-completions", "apiKey": "bench",
            "models": [{"id": m["id"], "contextWindow": m["ctx"], "maxTokens": 32768}]}}}))
        env.update(HOME=str(run_dir / "home"), PI_CODING_AGENT_DIR=str(agent), PI_OFFLINE="1")
        return ["pi", "-p", "--provider", "bench", "--model", m["id"], "--mode", "json", "--no-extensions",
                "--no-skills", "--no-prompt-templates", "--session-dir", str(run_dir / "pi-sessions"), TASK], env
    if h == "dsh":
        patch = run_dir / "dsh-patch.yml"
        patch.write_text(f"""- id: llm-pi-ai
  config:
    providers:
      bench:
        displayName: Bench
        apiKeyEnv: BENCH_API_KEY
        api: openai-completions
        baseURL: {proxy}/v1
        models:
          - id: {m["id"]}
            contextWindow: {m["ctx"]}
- id: agent-default-model
  config:
    provider: bench
    model: {m["id"]}
- id: session-telemetry-otel
  config:
    mode: DISABLED
""")
        env.update(HOME=str(run_dir / "home"), DSH_HOME=str(run_dir / "dsh-home"), BENCH_API_KEY="bench",
                   DSH_PERMISSION_MODE="danger-full-access", DSH_TELEMETRY_MODE="DISABLED")
        return ["dsh", "--profile", "headless", "--patch", str(patch), TASK], env
    if h == "claude-code":
        env.update(ANTHROPIC_BASE_URL=proxy, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", DISABLE_TELEMETRY="1",
                   DISABLE_AUTOUPDATER="1")
        if m.get("hosted"):                   # Claude itself: root, with the user's own Claude Code login
            env["HOME"] = "/root"
            return ["/root/.local/bin/claude", "-p", "--output-format", "stream-json", "--verbose", ALLOWED, TASK], env
        env.update(HOME=str(run_dir / "home"), ANTHROPIC_AUTH_TOKEN="bench", ANTHROPIC_MODEL=m["id"],
                   ANTHROPIC_SMALL_FAST_MODEL=m["id"],
                   # a model Claude Code doesn't know: tell it the server's real window so it compacts in time
                   CLAUDE_CODE_MAX_CONTEXT_TOKENS=str(m["ctx"]))
        return ["/opt/claude-bench/claude", "-p", "--output-format", "stream-json", "--verbose", "--model", m["id"],
                ALLOWED, TASK], env
    raise ValueError(h)


def context_window(m: dict) -> tuple[int | None, int | None]:
    """(tokens per slot, slots) as the server reports them; hosted models use the documented window."""
    if m.get("hosted"):
        return m["ctx"], None
    if m.get("props"):                            # a server without /props (TabbyAPI): the stated values
        return m["props"]["default_generation_settings"]["n_ctx"], m["props"].get("total_slots")
    try:
        with urllib.request.urlopen(m["upstream"] + "/props", timeout=10) as r:
            d = json.loads(r.read())
        return (d.get("default_generation_settings") or {}).get("n_ctx") or d.get("n_ctx"), d.get("total_slots")
    except Exception:
        return None, None                         # unreachable: unknown, not guessed


def logged_compactions(h: str, run_dir: Path, rec_dir: Path) -> int | None:
    """Compactions the harness itself reported, from its own logs."""
    try:
        if h == "rameness":
            return (rec_dir / "stderr.log").read_text(errors="replace").count("[rameness] compacted context")
        if h == "pi":
            return sum(1 for ln in (rec_dir / "stdout.log").read_text(errors="replace").splitlines()
                       if '"type":"compaction_end"' in ln and '"errorMessage"' not in ln and '"aborted":true' not in ln)
        if h == "claude-code":
            return (rec_dir / "stdout.log").read_text(errors="replace").count('"subtype":"compact_boundary"')
        if h == "dsh":
            return sum(f.read_text(errors="replace").count('"compaction/summary"')
                       for f in (run_dir / "dsh-home").rglob("*.jsonl"))
    except OSError:
        return None
    return None


def workspace_stats(work: Path) -> tuple[int, int]:
    files = [f for f in work.rglob("*") if f.is_file() and f.suffix in (".html", ".js", ".css", ".mjs")
             and "node_modules" not in f.parts and not any(p.startswith(".") for p in f.relative_to(work).parts)]
    loc = 0
    for f in files:
        try:
            loc += len(f.read_text(errors="replace").splitlines())
        except OSError:
            pass
    return len(files), loc


def score_build(game: Path, shot: Path, repeats: int = 3) -> dict:
    """Score a build ``repeats`` times; a check passes when it passes in most runs.

    The movement and interaction checks compare screenshots over time, so one run can flip a check
    either way under CPU load (software WebGL). The majority of three is stable."""
    runs = []
    for i in range(repeats):
        p = subprocess.run(SCORER + [str(game), "--screenshot", str(shot)], cwd=REPO,
                           capture_output=True, text=True, timeout=900)
        try:
            runs.append(json.loads(p.stdout.strip().splitlines()[-1]))
        except (json.JSONDecodeError, IndexError):
            runs.append({"error": p.stderr[-1000:]})
    ok = [r for r in runs if "checks" in r]
    if not ok:
        return {"score": 0.0, "passed": 0, "total": 14, "checks": {}, "fps": None, "loc": 0, "files": 0,
                "error": runs[-1].get("error")}
    s = dict(ok[-1])
    s["checks"] = {k: {"pass": sum(r["checks"][k]["pass"] for r in ok) * 2 > len(ok),
                       "detail": v["detail"], "passes": f"{sum(r['checks'][k]['pass'] for r in ok)}/{len(ok)}"}
                   for k, v in ok[-1]["checks"].items()}
    fps = sorted(r["fps"] for r in ok if r.get("fps") is not None)
    s["fps"] = fps[len(fps) // 2] if fps else None
    s["passed"] = sum(c["pass"] for c in s["checks"].values())
    s["score"] = round(100 * s["passed"] / s["total"], 1)
    s["score_runs"] = [r.get("score") for r in runs]
    return s


def mem_available_mb() -> int:
    """MemAvailable plus half the free swap: on a host with swap a short spike pages out instead of killing a
    process, so swap counts (at half, since swapped pages are slow). Servers that hold experts in host RAM
    (TabbyAPI CPU MoE) leave too little MemAvailable alone for any run to start."""
    info = {}
    for ln in Path("/proc/meminfo").read_text().splitlines():
        key, _, rest = ln.partition(":")
        if key in ("MemAvailable", "SwapFree"):
            info[key] = int(rest.split()[0]) // 1024
    if "MemAvailable" not in info:
        return 1 << 30
    return info["MemAvailable"] + info.get("SwapFree", 0) // 2


def wait_for_memory(need_mb: int, timeout: float = 1800) -> bool:
    """Don't start a run into a memory shortage: the host also holds the models, the decision model and the
    scorer's browser, and running out stops everything."""
    t0 = time.time()
    while mem_available_mb() < need_mb:
        if time.time() - t0 > timeout:
            return False
        time.sleep(30)
    return True


def probe_tps(m: dict) -> float:
    """Decode speed of an idle-ish llama-server over a long reply (0 if it doesn't answer). Long, because a
    degraded server can look healthy on short replies: occamy measured 16 tok/s over 400 tokens but 5.8 over 3000
    (healthy: ~30 at any length)."""
    body = json.dumps({"model": m["id"], "max_tokens": 1500, "chat_template_kwargs": {"enable_thinking": False},
                       "messages": [{"role": "user", "content": "Write a long, detailed essay on the history of "
                                     "computing. Keep going until you are stopped."}]}).encode()
    try:
        req = urllib.request.Request(m["upstream"] + "/v1/chat/completions", body, {"content-type": "application/json"})
        with urllib.request.urlopen(req, timeout=900) as r:
            return float(json.load(r)["timings"]["predicted_per_second"])
    except Exception:
        return 0.0


def wait_for_speed(m: dict, timeout: float = 10 * 3600, out=print) -> bool:
    """A run on a degraded server measures the server, not the harness: wait until two probes in a row reach
    the model's min_tps."""
    need = m.get("min_tps")
    if not need:
        return True
    t0, ok = time.time(), 0
    while time.time() - t0 < timeout:
        tps = probe_tps(m)
        ok = ok + 1 if tps >= need else 0
        if ok >= 2:
            return True
        if not ok:
            out(f"[bench] {m['label']} decodes at {tps:.1f} tok/s (< {need}); waiting", flush=True)
            time.sleep(900)                          # each probe loads the server for minutes
    return False


def sweep_tmp(user: str = "bench", out=print) -> None:
    """Runs share the bench user's /tmp, so one run could find an earlier run's scratch tests and probes. Clear
    them before a run - only when no other process of that user is running, so a concurrent run keeps its files."""
    if subprocess.run(["pgrep", "-u", user], capture_output=True).stdout.strip():
        return
    gone = 0
    for f in Path("/tmp").iterdir():
        try:
            if f.owner() == user:
                shutil.rmtree(f) if f.is_dir() and not f.is_symlink() else f.unlink()
                gone += 1
        except (OSError, KeyError):
            pass
    if gone:
        out(f"[bench] cleared {gone} leftover /tmp entries of {user}", flush=True)


def reap(run_dir: Path, user: str = "bench", out=print) -> None:
    """Stop what a run left running: servers the agent started with nohup/setsid escape the harness's process
    group and keep ports (one outlived its run by 20 h). Only processes working inside this run's directory, so a
    concurrent run keeps its own."""
    import pwd
    uid, root, left = pwd.getpwnam(user).pw_uid, str(run_dir.resolve()), []
    for d in Path("/proc").iterdir():
        if d.name.isdigit():
            try:
                if d.stat().st_uid == uid and os.readlink(d / "cwd").startswith(root):
                    os.kill(int(d.name), signal.SIGTERM)
                    left.append(d.name)
            except OSError:
                pass
    if left:
        out(f"[bench] stopped {len(left)} process(es) the run left running", flush=True)


def finish_task_run(h, m, run, rec_dir, run_dir, work, wall, timed_out, p, totals, window, slots, budget, cost,
                    labels) -> dict:
    """Score a --task run with the task's own scorer and record it like a Minecraft run."""
    try:
        r = subprocess.run([sys.executable, str(TASK_DIR / "score.py"), str(work)], capture_output=True, text=True,
                           timeout=900, cwd=REPO)
        score = json.loads(r.stdout)
    except Exception as e:
        score = {"score": 0.0, "passed": 0, "total": 0, "checks": {}, "error": repr(e)[:300]}
    result = {"run": run, "harness": h, "model": m["id"], "model_label": m["label"], "server": m["server"],
              "task": TASK_DIR.name, "wall_seconds": round(wall, 1), "timed_out": timed_out, "exit_code": p.returncode,
              "cost_usd": cost, **totals, "context_window": window, "slots": slots, "reasoning_budget": budget,
              "score": score.get("score"), "passed": score.get("passed"), "total": score.get("total"),
              "judge": (score.get("judge") or {}).get("overall"),
              "checks": {k: v["pass"] for k, v in (score.get("checks") or {}).items()}}
    (rec_dir / "result.json").write_text(json.dumps({**result, "score_detail": score}, indent=1))
    for log in ("events.jsonl", "tool_calls.jsonl"):
        if (run_dir / log).exists():
            shutil.copy(run_dir / log, rec_dir / log)
    shutil.copytree(work, rec_dir / "work", dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules", ".git"))
    vm.push([(f"bench_run_{k}", result[k], labels) for k in ("wall_seconds", "score", "passed")])
    print(f"[{run}] done in {wall / 60:.1f} min: score {result['score']} ({result['passed']}/{result['total']}), "
          f"judge {result['judge']}, {totals.get('llm_requests')} requests"
          + (", TIMED OUT" if timed_out else f", exit {p.returncode}"), flush=True)
    return result


def find_game(work: Path) -> Path | None:
    if (work / "index.html").exists():
        return work
    cands = sorted((p.parent for p in work.rglob("index.html") if "node_modules" not in p.parts),
                   key=lambda p: len(p.parts))
    return cands[0] if cands else None


def run_one(h: str, mkey: str, batch: str, out: Path, timeout: int, reasoning_budget: int | None = None) -> dict:
    m = MODELS[mkey]
    run = f"{batch}-{h}-{mkey}"
    labels = {"harness": h, "model": m["id"], "server": m["server"], "run": run, "batch": batch}
    local = not m.get("hosted")
    # thinking tokens per reply, llama-server only (Claude's thinking is its own setting): --reasoning-budget,
    # else the model's default
    budget = (reasoning_budget if reasoning_budget is not None else m.get("reasoning_budget")) if local else None
    run_dir = (Path("/home/bench/runs") if local else out) / run
    if local:
        sweep_tmp()
    stamp = time.strftime("%H%M%S")
    for d in {run_dir, out / run}:                  # a rerun starts clean; the earlier attempt is kept
        if d.exists():
            d.rename(d.with_name(f"{d.name}.attempt-{stamp}"))
    work = run_dir / "work"
    work.mkdir(parents=True, exist_ok=True)
    if TASK_DIR and (TASK_DIR / "seed").is_dir():             # the task's starting files (a repo, sources...)
        shutil.copytree(TASK_DIR / "seed", work, dirs_exist_ok=True)
    rec_dir = out / run
    rec_dir.mkdir(parents=True, exist_ok=True)
    window, slots = context_window(m)
    vm.push([("bench_model_context_window", window, {"model": m["id"], "server": m["server"]}),
             ("bench_model_slots", slots, {"model": m["id"], "server": m["server"]}),
             ("bench_run_context_window", window, labels)])
    print(f"[{run}] start (context window {window}{f' x {slots} slots' if slots else ''}"
          f"{f', reasoning budget {budget}' if budget is not None else ''})", flush=True)
    t0 = time.time()
    with Proxy(m["upstream"], labels, rec_dir / "llm.jsonl", reasoning_budget=budget,
               vision=m.get("vision"), props=m.get("props"),
               budget_field=m.get("budget_field", "reasoning_budget_tokens"),
               tool_adapter=m.get("tool_adapter"), max_tokens_cap=m.get("max_tokens_cap")) as px:
        cmd, env = harness_cmd(h, m, px.url, run_dir, work, labels)
        if h == "rameness":
            env["RAMENESS_EVENT_LOG"] = str(run_dir / "events.jsonl")
        if local:
            subprocess.run(["chown", "-R", "bench:bench", str(run_dir)], check=True)
        stdout, stderr = (rec_dir / "stdout.log").open("wb"), (rec_dir / "stderr.log").open("wb")
        kw = {"user": "bench", "group": "bench", "extra_groups": []} if local else {}
        p = subprocess.Popen(cmd, cwd=work, env=env, stdout=stdout, stderr=stderr, stdin=subprocess.DEVNULL,
                             start_new_session=True, **kw)
        # live logs -> Loki: the agent's own log (parsed per harness), each model request, JEV decisions
        shipper = loki.Shipper({k: labels[k] for k in ("batch", "run", "harness", "model")},
                               loki.sources_for(h, rec_dir, run_dir, work))
        shipper.start()
        shipper.log("start", f"{h} on {m['label']} ({m['server']}), window {window}, reasoning budget {budget}, "
                             f"timeout {timeout}s")
        stop = threading.Event()

        def ticker():
            while not stop.wait(15):
                n, loc = workspace_stats(work)
                tot = px.recorder.totals()
                vm.push([("bench_run_elapsed_seconds", time.time() - t0, labels), ("bench_run_active", 1, labels),
                         ("bench_run_workspace_files", n, labels), ("bench_run_workspace_loc", loc, labels),
                         ("bench_run_llm_requests", tot["llm_requests"], labels)])
        threading.Thread(target=ticker, daemon=True).start()
        timed_out = False
        try:
            p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGTERM)
            try:
                p.wait(timeout=30)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait()
        stop.set()
        shipper.stop()
        wall = time.time() - t0
        if local:
            reap(run_dir)
        totals = px.recorder.totals()
    vm.push([("bench_run_active", 0, labels)])
    cost = None
    try:
        for ln in reversed((rec_dir / "stdout.log").read_text(errors="replace").splitlines()):
            if '"total_cost_usd"' in ln:
                cost = json.loads(ln).get("total_cost_usd")
                break
    except (OSError, json.JSONDecodeError):
        pass
    if TASK_DIR:
        return finish_task_run(h, m, run, rec_dir, run_dir, work, wall, timed_out, p, totals, window, slots, budget,
                               cost, labels)
    game = find_game(work)
    score = {"score": 0.0, "passed": 0, "total": 14, "checks": {}, "fps": None, "loc": 0, "files": 0}
    if game:
        score = score_build(game, rec_dir / "screenshot.png")
    result = {"run": run, "harness": h, "model": m["id"], "model_label": m["label"], "server": m["server"],
              "wall_seconds": round(wall, 1), "timed_out": timed_out, "exit_code": p.returncode,
              "game_dir": str(game.relative_to(work)) if game else None, "cost_usd": cost, **totals,
              "context_window": window, "slots": slots, "reasoning_budget": budget,
              "context_utilization": round(totals["context_tokens_max"] / window, 3) if window else None,
              "compactions_logged": logged_compactions(h, run_dir, rec_dir),
              "score": score.get("score"), "passed": score.get("passed"), "fps": score.get("fps"),
              "loc": score.get("loc"), "files": score.get("files"), "block_count": score.get("block_count"),
              "page_errors": len(score.get("page_errors") or []), "click_starts": score.get("click_starts"),
              "checks": {k: v["pass"] for k, v in (score.get("checks") or {}).items()}}
    (rec_dir / "result.json").write_text(json.dumps({**result, "score_detail": score}, indent=1))
    shipper.log("end", f"score={result['score']}% passed={result['passed']}/14 wall={result['wall_seconds']}s "
                       f"timed_out={timed_out} exit={p.returncode} "
                       f"failed={[k for k, v in result['checks'].items() if not v]}")
    for log in ("events.jsonl", "tool_calls.jsonl"):
        if (run_dir / log).exists():
            shutil.copy(run_dir / log, rec_dir / log)
                                                   # keep the build with its results, and publish a readable copy
    shutil.copytree(work, rec_dir / "build", dirs_exist_ok=True, ignore=shutil.ignore_patterns("node_modules", ".git"))
    subprocess.run(["rsync", "-a", "--chmod=a+rX", f"{out.parent.parent}/", "/srv/bench/results/"], check=False)
    s = [(f"bench_run_{k}", result[k], labels) for k in (
        "wall_seconds", "score", "passed", "fps", "loc", "files", "cost_usd", "page_errors", "context_window",
        "context_utilization", "compactions_logged", "reasoning_budget")]
    s += [("bench_run_timed_out", int(timed_out), labels), ("bench_run_exit_code", p.returncode, labels),
          ("bench_run_completed", 1, labels)]
    s += [(f"bench_run_{k}", v, labels) for k, v in totals.items()]
    s += [("bench_run_check_pass", int(v), {**labels, "check": k}) for k, v in result["checks"].items()]
    vm.push(s)
    print(f"[{run}] done in {wall / 60:.1f} min: score {result['score']} ({result['passed']}/{score.get('total', 14)}), "
          f"{totals['llm_requests']} requests, max context {totals['context_tokens_max']}/{window}, "
          f"compactions {totals['compactions']} (logged {result['compactions_logged']}), "
          f"{'TIMED OUT' if timed_out else 'exit ' + str(p.returncode)}", flush=True)
    return result


def main():
    global TASK, TASK_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--harnesses", default="rameness,pi,dsh,claude-code")
    ap.add_argument("--models", default="qwen,occamy,claude")
    ap.add_argument("--timeout", type=int, default=150 * 60)
    ap.add_argument("--out", default="/root/bench/results/minecraft")
    ap.add_argument("--task", help="a task in bench/tasks (task.md, seed/, score.py) instead of the Minecraft build")
    ap.add_argument("--batch")
    ap.add_argument("--runs", help="explicit harness:model pairs, e.g. dsh:occamy,claude-code:qwen")
    ap.add_argument("--reasoning-budget", type=int, help="thinking tokens per reply on llama-server models "
                    "(default: the model's reasoning_budget in MODELS, else unlimited)")
    ap.add_argument("--rameness-config", default="", help="JSON merged into Rameness's config (flag experiments)")
    ap.add_argument("--serial", action="store_true", help="one run at a time across all servers (saves memory)")
    ap.add_argument("--sop-pipeline", action="store_true",
                    help="Rameness: learn and save SOPs and run the proposal pipeline, into local stand-in repos")
    ap.add_argument("--min-free-mb", type=int, default=5000, help="wait for this much free memory before a run")
    ap.add_argument("--smoke", action="store_true", help="a trivial task, to check each harness is wired up")
    a = ap.parse_args()
    if a.task:
        TASK_DIR = REPO / "bench" / "tasks" / a.task
        TASK = (TASK_DIR / "task.md").read_text()
        if a.out == "/root/bench/results/minecraft":
            a.out = f"/root/bench/results/{a.task}"
    if a.smoke:
        TASK = "Create a file index.html in the current directory containing <h1>ok</h1>. Then stop."
    batch = a.batch or time.strftime("mc-%Y%m%d-%H%M%S")
    out = Path(a.out) / batch
    out.mkdir(parents=True, exist_ok=True)
    harnesses = a.harnesses.split(",")
    queues: dict[str, list[tuple[str, str]]] = {}
    pairs = [tuple(x.split(":")) for x in a.runs.split(",")] if a.runs else \
        [(h, mk) for mk in a.models.split(",") for h in harnesses]
    for h, mk in pairs:
        if MODELS[mk].get("hosted") and h != "claude-code":
            continue                          # Claude (the model) only runs in Claude Code here
        queues.setdefault(MODELS[mk]["server"], []).append((h, mk))
    if any(h == "rameness" for h, _ in pairs):
        sync_rameness()
    results, lock = [], threading.Lock()

    def worker(jobs):
        for h, mk in jobs:
            if not wait_for_memory(a.min_free_mb):
                print(f"[{h}/{mk}] skipped: less than {a.min_free_mb} MB free for 30 min", flush=True)
                continue
            if not wait_for_speed(MODELS[mk]):
                print(f"[{h}/{mk}] skipped: server stayed below {MODELS[mk]["min_tps"]} tok/s for 10 h", flush=True)
                continue
            try:
                r = run_one(h, mk, batch, out, a.timeout, a.reasoning_budget)
            except Exception as e:
                r = {"run": f"{batch}-{h}-{mk}", "harness": h, "model": mk, "error": repr(e)}
                print(f"[{h}/{mk}] failed: {e!r}", flush=True)
            with lock:
                results.append(r)
                (out / f"results-{os.getpid()}.json").write_text(json.dumps(results, indent=1))
    if a.rameness_config:
        json.loads(a.rameness_config)
        os.environ["BENCH_RAMENESS_CONFIG"] = a.rameness_config
    if a.sop_pipeline:
        os.environ["BENCH_SOP_PIPELINE"] = "1"
    if a.serial:
        queues = {"all": [j for jobs in queues.values() for j in jobs]}
    threads = [threading.Thread(target=worker, args=(jobs,)) for jobs in queues.values()]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    print(f"results: {out / 'results.json'}")


if __name__ == "__main__":
    sys.exit(main())
