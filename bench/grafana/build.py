"""Generate the benchmark dashboards for Grafana (VictoriaMetrics datasource).

    python -m bench.grafana.build [--out /var/lib/grafana/dashboards]

* **JEV decision models**: accuracy (standard / paraphrased, per decision set), P(correct option),
  latency p50/p95, batched-question latency, throughput, memory, load time, per-decision
  latency over time, and Rameness's live decision telemetry.
* **Harness comparison: Minecraft**: one row per run (harness x model) with score, time, tokens,
  context, tool calls, speed, cost; the scorer's checks per run; live progress of running jobs
  (elapsed, code written, context and speed per model request); GPU and CPU while they ran.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

DS = {"type": "prometheus", "uid": "victoriametrics"}
_id = 0


def pid() -> int:
    global _id
    _id += 1
    return _id


def target(expr, legend="", ref="A", instant=False, fmt=None):
    t = {"datasource": DS, "expr": expr, "legendFormat": legend, "refId": ref}
    if instant:
        t.update(instant=True, range=False)
    else:
        t["range"] = True
    if fmt:
        t["format"] = fmt
    return t


def panel(kind, title, targets, x, y, w, h, unit=None, desc="", **extra):
    p = {"id": pid(), "type": kind, "title": title, "description": desc, "datasource": DS, "targets": targets,
         "gridPos": {"x": x, "y": y, "w": w, "h": h}, "fieldConfig": {"defaults": {}, "overrides": []},
         "options": {}}
    if unit:
        p["fieldConfig"]["defaults"]["unit"] = unit
    for k, v in extra.items():
        if k == "defaults":
            p["fieldConfig"]["defaults"].update(v)
        else:
            p[k] = v
    return p


def ts(title, targets, x, y, w, h, unit=None, desc="", points=False, **kw):
    p = panel("timeseries", title, targets, x, y, w, h, unit, desc, **kw)
    p["fieldConfig"]["defaults"]["custom"] = {"lineWidth": 2, "fillOpacity": 6, "showPoints": "always" if points else "never",
                                              "pointSize": 5, "spanNulls": 3600000, "drawStyle": "points" if points else "line"}
    p["options"] = {"legend": {"displayMode": "table", "placement": "right", "calcs": ["lastNotNull", "max"]},
                    "tooltip": {"mode": "multi", "sort": "desc"}}
    return p


def bars(title, targets, x, y, w, h, unit=None, desc="", minv=None, maxv=None, thresholds=None):
    p = panel("bargauge", title, targets, x, y, w, h, unit, desc)
    d = p["fieldConfig"]["defaults"]
    if minv is not None:
        d["min"] = minv
    if maxv is not None:
        d["max"] = maxv
    d["color"] = {"mode": "thresholds"} if thresholds else {"mode": "palette-classic"}
    if thresholds:
        d["thresholds"] = {"mode": "absolute", "steps": thresholds}
    p["options"] = {"orientation": "horizontal", "displayMode": "gradient", "showUnfilled": True,
                    "reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "namePlacement": "left",
                    "valueMode": "color", "minVizHeight": 14, "maxVizHeight": 28}
    return p


def row(title, y, collapsed=False):
    return {"id": pid(), "type": "row", "title": title, "collapsed": collapsed, "gridPos": {"x": 0, "y": y, "w": 24, "h": 1},
            "panels": []}


def var(name, query, label, multi=True):
    return {"name": name, "label": label, "type": "query", "datasource": DS, "query": {"query": query, "refId": name},
            "definition": query, "refresh": 2, "multi": multi, "includeAll": True, "allValue": ".*",
            "current": {"text": "All", "value": "$__all"}, "sort": 1}


def dashboard(uid, title, panels, variables, desc, hours=12):
    return {"uid": uid, "title": title, "description": desc, "tags": ["bench", "rameness", "jev"], "timezone": "browser",
            "schemaVersion": 39, "version": 1, "editable": True, "refresh": "30s", "graphTooltip": 1,
            "time": {"from": f"now-{hours}h", "to": "now"}, "templating": {"list": variables}, "panels": panels,
            "annotations": {"list": []}}


GOOD = [{"color": "red", "value": None}, {"color": "orange", "value": 0.5}, {"color": "green", "value": 0.8}]
LAT = [{"color": "green", "value": None}, {"color": "orange", "value": 0.5}, {"color": "red", "value": 2}]


def jev_dashboard():
    L = '{jev_model=~"$model",run=~"$run"}'
    last = lambda m, extra="": f'last_over_time({m}{{jev_model=~"$model",run=~"$run"{extra}}}[30d])'
    y, P = 0, []
    P.append(row("Accuracy", y)); y += 1
    P.append(bars("Accuracy: standard wording", [target(last("bench_jev_accuracy", ',set="all",phrasing="standard"'),
                  "{{jev_model}} ({{checkpoint}}, {{device}})", instant=True)], 0, y, 8, 7, "percentunit",
                  "Share of labelled harness decisions the model got right.", 0, 1, GOOD))
    P.append(bars("Accuracy: paraphrased wording", [target(last("bench_jev_accuracy", ',set="all",phrasing="paraphrased"'),
                  "{{jev_model}} ({{checkpoint}})", instant=True)], 8, y, 8, 7, "percentunit",
                  "Same decisions phrased casually, avoiding the options' keywords.", 0, 1, GOOD))
    P.append(bars("P(correct option), mean", [target(last("bench_jev_p_gold", ',set="all"'), "{{jev_model}} {{phrasing}}",
                  instant=True)], 16, y, 8, 7, "percentunit",
                  "Probability the model put on the right answer: its confidence where it matters. Low values send decisions to the user.",
                  0, 1, GOOD))
    y += 7
    acc = panel("table", "Accuracy by decision set (standard wording)",
                [target(last("bench_jev_accuracy", ',set!="all",phrasing="standard"'), "", instant=True, fmt="table")],
                0, y, 12, 9, "percentunit", "Rows: decision sets. Columns: models.")
    acc["transformations"] = [{"id": "groupingToMatrix", "options": {"columnField": "jev_model", "rowField": "set",
                                                                     "valueField": "Value"}}]
    acc["fieldConfig"]["defaults"].update({"custom": {"cellOptions": {"type": "color-background"}, "align": "center"},
                                           "color": {"mode": "thresholds"},
                                           "thresholds": {"mode": "absolute", "steps": GOOD}, "decimals": 2})
    P.append(acc)
    P.append(bars("Decisions that failed (server error / fallback)", [target(last("bench_jev_failed_decisions"),
                  "{{jev_model}}", instant=True)], 12, y, 12, 4, "short", "", 0))
    P.append(bars("Model load time", [target(last("bench_jev_load_seconds"), "{{jev_model}} ({{device}})", instant=True)],
                  12, y + 4, 6, 5, "s"))
    P.append(bars("Server memory (RSS)", [target(last("bench_jev_rss_bytes"), "{{jev_model}} ({{params}})", instant=True)],
                  18, y + 4, 6, 5, "bytes"))
    y += 9
    P.append(row("Speed", y)); y += 1
    P.append(bars("Decision latency p50", [target(last("bench_jev_latency_seconds", ',quantile="p50"'), "{{jev_model}} ({{device}})",
                  instant=True)], 0, y, 6, 7, "s", "One choice question per request.", 0, None, LAT))
    P.append(bars("Decision latency p95", [target(last("bench_jev_latency_seconds", ',quantile="p95"'), "{{jev_model}}",
                  instant=True)], 6, y, 6, 7, "s", "", 0, None, LAT))
    P.append(bars("Throughput", [target(last("bench_jev_throughput_dps"), "{{jev_model}}", instant=True)], 12, y, 6, 7,
                  "short", "Decisions per second, sequential.", 0))
    bq = bars("Batched questions: latency per request", [target(last("bench_jev_batch_seconds"),
              "{{jev_model}} x{{questions}}", instant=True)], 18, y, 6, 7, "s",
              "One request carrying 1 / 4 / 8 / 16 yes-no questions: what a per-option `activate` decision costs.", 0)
    P.append(bq)
    y += 7
    P.append(ts("Per-decision latency", [target(f"bench_jev_decision_seconds{L}", "{{jev_model}}")], 0, y, 12, 8, "s",
                "Every benchmark decision as a point.", points=True))
    P.append(ts("Per-decision P(correct option)", [target(f"bench_jev_decision_p_gold{L}", "{{jev_model}}")], 12, y, 12, 8,
                "percentunit", "", points=True))
    y += 8
    P.append(row("Rameness live decisions (RAMENESS_TELEMETRY_URL)", y)); y += 1
    P.append(ts("Decision latency by backend", [target("rameness_jev_decision_seconds", "{{backend}} {{kind}} {{run}}")],
                0, y, 8, 8, "s", "Every decision a running Rameness made.", points=True))
    P.append(ts("Decision confidence", [target("rameness_jev_decision_confidence", "{{backend}} {{run}}")], 8, y, 8, 8,
                "percentunit", "", points=True))
    P.append(ts("Fallbacks and state size", [target("sum by (backend) (rameness_jev_decision_fallback)", "fallback {{backend}}", "A"),
                                               target("rameness_jev_decision_state_chars", "state chars {{backend}}", "B")],
                16, y, 8, 8, "short", "Fallback = the decision model was unreachable and a weaker backend answered.", points=True))
    y += 8
    P.append(row("Host while benchmarking", y)); y += 1
    P.append(ts("CPU load", [target("node_load1", "load1")], 0, y, 8, 7, "short"))
    P.append(ts("Memory available", [target("node_memory_MemAvailable_bytes", "available")], 8, y, 8, 7, "bytes"))
    P.append(ts("GPU", [target("max(gpu_utilization_ratio)", "utilisation", "A"),
                        target("max(gpu_memory_used_bytes) / 17179869184", "memory used (of 16 GiB)", "B")], 16, y, 8, 7,
                "percentunit"))
    return dashboard("jev-models", "JEV decision models", P,
                     [var("model", "label_values(bench_jev_accuracy, jev_model)", "Model"),
                      var("run", "label_values(bench_jev_accuracy, run)", "Run")],
                     "Laya, Kev and Open-Jev on Rameness's labelled decision benchmark (bench/jevmodels.py).", hours=24)


def harness_dashboard():
    sel = '{batch=~"$batch",harness=~"$harness",model=~"$model"}'
    def last(m):
        """latest value per run; `m` is a metric name or an expression over bench_* metrics"""
        if m.startswith("("):
            import re
            return "max by (run, harness, model) " + re.sub(r"(bench_[a-z_]+)", lambda x: f"last_over_time({x.group(1)}{sel}[30d])", m)
        return f'max by (run, harness, model) (last_over_time({m}{sel}[30d]))'
    y, P = 0, []
    P.append(row("Running now", y)); y += 1
    live = lambda m: f'max by (run, harness, model, server) (last_over_time({m}{sel}[1m]))'
    run_now = panel("table", "Runs in progress", [
        target(f'{live("bench_run_active")} == 1', "", "A", instant=True, fmt="table"),
        target(live("bench_run_elapsed_seconds"), "", "B", instant=True, fmt="table"),
        target(live("bench_run_workspace_loc"), "", "C", instant=True, fmt="table"),
        target(live("bench_run_llm_requests"), "", "D", instant=True, fmt="table"),
        target(f'max by (run, harness, model, server) (last_over_time(bench_run_context_tokens_max{sel}[1m]))', "", "E",
               instant=True, fmt="table")],
        0, y, 24, 5, None, "Runs that reported in the last minute (the runner reports every 15 s while a harness runs).")
    run_now["transformations"] = [
        {"id": "merge", "options": {}},
        {"id": "filterByValue", "options": {"filters": [{"fieldName": "Value #A", "config": {"id": "isNull"}}],
                                            "type": "exclude", "match": "any"}},
        {"id": "organize", "options": {"excludeByName": {"Time": True, "Value #A": True},
                                       "renameByName": {"Value #B": "Elapsed", "Value #C": "Lines written",
                                                        "Value #D": "Model requests", "Value #E": "Largest context so far"}}}]
    run_now["fieldConfig"]["overrides"] = [{"matcher": {"id": "byName", "options": "Elapsed"},
                                            "properties": [{"id": "unit", "value": "s"}]}]
    run_now["options"] = {"showHeader": True, "footer": {"show": False}}
    P.append(run_now)
    y += 5
    P.append(row("Results per run", y)); y += 1
    cols = [("bench_run_score", "Score %"), ("bench_run_judge_overall", "Visual /10"), ("bench_run_wall_seconds", "Wall time"), ("bench_run_llm_requests", "Model requests"),
            ("bench_run_tool_calls_total", "Tool calls"), ("bench_run_context_tokens_max", "Max context"),
            ("bench_run_prompt_tokens_total", "Prompt tokens sent (incl. cached)"), ("bench_run_cached_tokens_total", "Cached tokens"),
            ("(bench_run_prompt_tokens_total - bench_run_cached_tokens_total)", "Prompt tokens newly processed"),
            ("bench_run_completion_tokens_total", "Output tokens"), ("bench_run_gen_tps_mean", "Gen tok/s"),
            ("bench_run_llm_seconds_total", "Time in model"), ("bench_run_fps", "Game FPS"), ("bench_run_loc", "Lines of code"),
            ("bench_run_llm_errors", "Model errors"), ("bench_run_timed_out", "Timed out"), ("bench_run_cost_usd", "Cost $"),
            ("bench_run_context_window", "Context window"), ("bench_run_context_utilization", "Window used"),
            ("bench_run_compactions", "Compactions (detected)"), ("bench_run_compactions_logged", "Compactions (logged)"),
            ("bench_run_reasoning_budget", "Think budget (tokens)")]
    tbl = panel("table", "Every run", [target(last(m), "", chr(65 + i), instant=True, fmt="table") for i, (m, _) in enumerate(cols)],
                0, y, 24, 9, None, "One row per harness x model run. Score is the share of the 14 black-box checks passed.")
    rename = {f"Value #{chr(65 + i)}": name for i, (_, name) in enumerate(cols)}
    tbl["transformations"] = [{"id": "merge", "options": {}},
                              {"id": "organize", "options": {"excludeByName": {"Time": True, "run": False},
                                                             "renameByName": rename}},
                              {"id": "sortBy", "options": {"sort": [{"field": "Score %", "desc": True}]}}]
    tbl["fieldConfig"]["overrides"] = [
        {"matcher": {"id": "byName", "options": "Score %"}, "properties": [
            {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "gradient"}}, {"id": "min", "value": 0},
            {"id": "max", "value": 100}, {"id": "color", "value": {"mode": "continuous-RdYlGr"}}]},
        {"matcher": {"id": "byName", "options": "Wall time"}, "properties": [{"id": "unit", "value": "s"}]},
        {"matcher": {"id": "byName", "options": "Time in model"}, "properties": [{"id": "unit", "value": "s"}]},
        {"matcher": {"id": "byName", "options": "Cost $"}, "properties": [{"id": "unit", "value": "currencyUSD"}]},
        {"matcher": {"id": "byRegexp", "options": ".*tokens|Max context|Context window"}, "properties": [{"id": "unit", "value": "short"}]},
        {"matcher": {"id": "byName", "options": "Window used"}, "properties": [
            {"id": "unit", "value": "percentunit"}, {"id": "custom.cellOptions", "value": {"type": "gauge", "mode": "basic"}},
            {"id": "min", "value": 0}, {"id": "max", "value": 1}]}]
    P.append(tbl)
    y += 9
    for i, (m, title, unit, d) in enumerate([
            ("bench_run_score", "Score (black-box checks passed)", "percent", ""),
            ("bench_run_judge_overall", "Visual quality (Claude judge, /10)", "none", "Screenshot graded on Minecraft likeness, rendering correctness, world richness, UI."),
            ("bench_run_wall_seconds", "Wall time", "s", "Until the harness stopped on its own, or the 2.5 h limit."),
            ("bench_run_context_tokens_max", "Largest context used", "short", "Biggest prompt + output in any one request."),
            ("bench_run_completion_tokens_total", "Output tokens", "short", "")]):
        P.append(bars(title, [target(last(m), "{{harness}} / {{model}}", instant=True)], (i % 5) * 5 if i < 4 else 20, y, 5 if i < 4 else 4, 8,
                      unit, d, 0, 100 if unit == "percent" else 10 if "judge" in m else None))
    y += 8
    P.append(bars("Share of the context window used (peak)", [target(last("bench_run_context_utilization"),
                  "{{harness}} / {{model}}", instant=True)], 0, y, 8, 8, "percentunit",
                  "Largest context in any request / the model's context window per slot.", 0, 1,
                  [{"color": "green", "value": None}, {"color": "orange", "value": 0.7}, {"color": "red", "value": 0.9}]))
    P.append(bars("Compactions", [target(last("bench_run_compactions"), "{{harness}} / {{model}} detected", "A", instant=True),
                                  target(last("bench_run_compactions_logged"), "{{harness}} / {{model}} logged", "B", instant=True)],
                  8, y, 8, 8, "short", "Detected: the main conversation's prompt shrank by 30%+ from 8k+ tokens (recording "
                  "proxy, same rule for every harness). Logged: what the harness itself reported.", 0))
    mw = panel("table", "Model context windows (as the servers report them)",
               [target('max by (model, server) (last_over_time(bench_model_context_window[30d]))', "", "A", instant=True, fmt="table"),
                target('max by (model, server) (last_over_time(bench_model_slots[30d]))', "", "B", instant=True, fmt="table")],
               16, y, 8, 8, None, "llama-server /props n_ctx per slot and total slots; Claude from Claude Code's modelUsage.")
    mw["transformations"] = [{"id": "merge", "options": {}},
                             {"id": "organize", "options": {"excludeByName": {"Time": True},
                                                            "renameByName": {"Value #A": "Context window (tokens)",
                                                                             "Value #B": "Slots"}}}]
    P.append(mw)
    y += 8
    chk = panel("table", "Checks passed per run", [target(f'max by (run, check) (last_over_time(bench_run_check_pass{sel}[30d]))',
                "", instant=True, fmt="table")], 0, y, 24, 8, None, "1 = passed. Checks come from bench/minecraft/score.py.")
    chk["transformations"] = [{"id": "groupingToMatrix", "options": {"columnField": "check", "rowField": "run", "valueField": "Value"}}]
    chk["fieldConfig"]["defaults"].update({"custom": {"cellOptions": {"type": "color-background"}, "align": "center"},
                                           "color": {"mode": "thresholds"},
                                           "thresholds": {"mode": "absolute", "steps": [{"color": "red", "value": None},
                                                                                        {"color": "green", "value": 1}]}})
    P.append(chk)
    y += 8
    P.append(row("Live progress", y)); y += 1
    P.append(ts("Elapsed", [target(f"bench_run_elapsed_seconds{sel}", "{{harness}} / {{model}}")], 0, y, 8, 8, "s"))
    P.append(ts("Code written (lines)", [target(f"bench_run_workspace_loc{sel}", "{{harness}} / {{model}}")], 8, y, 8, 8, "short"))
    P.append(ts("Model requests", [target(f"bench_run_llm_requests{sel}", "{{harness}} / {{model}}")], 16, y, 8, 8, "short"))
    y += 8
    P.append(row("Model requests (from the recording proxy)", y)); y += 1
    P.append(ts("Context per request (dashed: the model's window)", [
                target(f"bench_llm_context_tokens{sel}", "{{harness}} / {{model}}", "A"),
                target(f'max by (model) (last_over_time(bench_model_context_window{{model=~"$model"}}[30d]))', "window {{model}}", "B")],
                0, y, 12, 8, "short", "Prompt + output tokens of every request: how each harness grows its context, and "
                "where compactions cut it.", points=True,
                ))
    P[-1]["fieldConfig"]["overrides"] = [{"matcher": {"id": "byFrameRefID", "options": "B"}, "properties": [
        {"id": "custom.drawStyle", "value": "line"}, {"id": "custom.lineStyle", "value": {"fill": "dash", "dash": [10, 10]}},
        {"id": "custom.showPoints", "value": "never"}]}]
    P.append(ts("Generation speed", [target(f"bench_llm_gen_tps{sel}", "{{harness}} / {{model}}")], 12, y, 12, 8,
                "short", "Tokens per second (llama-server timings).", points=True))
    y += 8
    P.append(ts("Prompt processing speed", [target(f"bench_llm_prompt_tps{sel}", "{{harness}} / {{model}}")], 0, y, 8, 8,
                "short", "", points=True))
    P.append(ts("Time to first token", [target(f"bench_llm_ttft_seconds{sel}", "{{harness}} / {{model}}")], 8, y, 8, 8, "s",
                "", points=True))
    P.append(ts("Cached prompt tokens per request", [target(f"bench_llm_cached_tokens{sel}", "{{harness}} / {{model}}")],
                16, y, 8, 8, "short", "Prompt reuse (llama-server cache / Anthropic prompt cache).", points=True))
    y += 8
    P.append(ts("Request latency", [target(f"bench_llm_request_seconds{sel}", "{{harness}} / {{model}}")], 0, y, 8, 8, "s",
                "", points=True))
    P.append(ts("Tool calls requested", [target(f"bench_run_tool_calls_total{sel}", "{{harness}} / {{model}}")], 8, y, 8, 8,
                "short"))
    P.append(ts("Speculative decoding acceptance", [target(f"bench_llm_draft_accept_ratio{sel}", "{{harness}} / {{model}}")],
                16, y, 8, 8, "percentunit", "Only servers running a draft model report this.", points=True))
    y += 8
    P.append(row("Hardware during runs", y)); y += 1
    P.append(ts("GPU utilisation / memory", [target("max(gpu_utilization_ratio)", "utilisation", "A"),
                                               target("max(gpu_memory_used_bytes) / 17179869184", "memory (of 16 GiB)", "B")],
                0, y, 8, 7, "percentunit", "This host's GPU (serves the local Qwen model)."))
    P.append(ts("GPU power", [target("max(gpu_power_watts)", "watts")], 8, y, 8, 7, "watt"))
    P.append(ts("CPU load / memory available", [target("node_load1", "load1", "A"),
                                                  target("node_memory_MemAvailable_bytes / 2^30", "GiB available", "B")],
                16, y, 8, 7, "short"))
    return dashboard("harness-bench", "Harness comparison: Minecraft", P,
                     [var("batch", "label_values(bench_run_score, batch)", "Batch"),
                      var("harness", "label_values(bench_run_elapsed_seconds, harness)", "Harness"),
                      var("model", "label_values(bench_run_elapsed_seconds, model)", "Model")],
                     "Rameness vs DeepSeek Harness vs pi vs Claude Code building a Minecraft clone, on local models and Claude.")


LOKI = {"type": "loki", "uid": "loki"}
KINDS = ["text", "thinking", "tool_call", "tool_result", "compaction", "request", "decision", "loop_guard",
         "progress_review", "turn", "truncated", "prompt", "note", "start", "end"]


def lq(expr, ref="A", instant=False, legend=""):
    return {"datasource": LOKI, "expr": expr, "refId": ref, "queryType": "instant" if instant else "range",
            "legendFormat": legend}


def logs(title, expr, x, y, w, h, desc="", order="Ascending"):
    p = panel("logs", title, [lq(expr)], x, y, w, h, None, desc)
    p["datasource"] = LOKI
    p["options"] = {"showTime": True, "wrapLogMessage": True, "sortOrder": order, "enableLogDetails": True,
                    "prettifyLogMessage": False, "dedupStrategy": "none", "showLabels": False, "showCommonLabels": False}
    return p


def logs_dashboard():
    sel = 'job="bench", batch=~"$batch", run=~"$run", harness=~"$harness"'
    flt = ' | kind=~"$kind" |~ "(?i)$search"'
    P, y = [], 0
    P.append(row("Activity", y)); y += 1
    t = ts("Events per minute by kind", [lq(f'sum by (kind) (count_over_time({{{sel}}} | kind=~"$kind" [1m]))',
                                            legend="{{kind}}")],
           0, y, 12, 8, desc="What the agents were doing over time: model text, thinking, tool calls and results, "
                             "requests, JEV decisions, compactions.")
    t["datasource"] = LOKI
    P.append(t)
    tools = bars("Tool calls by harness and tool", [lq(f'sum by (harness, tool) (count_over_time({{{sel}, source="agent"}} '
                                                       f'| kind="tool_call" [$__range]))', instant=True,
                                                       legend="{{harness}} {{tool}}")],
                 12, y, 12, 8, desc="Which tools each harness leaned on in the selected runs.")
    tools["datasource"] = LOKI
    P.append(tools)
    y += 8
    counts = [("tool_call", "Tool calls"), ("tool_result\" | ok=\"False", "Tool errors"), ("text", "Model messages"),
              ("thinking", "Thinking blocks"), ("compaction", "Compaction events"), ("loop_guard", "Loop guard"),
              ("progress_review", "Progress reviews"), ("decision", "JEV decisions"), ("request", "Model requests")]
    tbl = panel("table", "Per run", [lq(f'sum by (run) (count_over_time({{{sel}}} | kind="{k}" [$__range]))',
                                        ref=chr(65 + i), instant=True) for i, (k, _) in enumerate(counts)],
                0, y, 24, 7, desc="Event counts per run over the dashboard time range.")
    tbl["datasource"] = LOKI
    tbl["transformations"] = [{"id": "merge", "options": {}},
                              {"id": "organize", "options": {"excludeByName": {"Time": True}, "renameByName": {
                                  **{f"Value #{chr(65 + i)}": name for i, (_, name) in enumerate(counts)}, "run": "Run"}}}]
    tbl["options"] = {"showHeader": True, "sortBy": [{"displayName": "Run", "desc": False}]}
    P.append(tbl)
    y += 7
    P.append(row("Timeline", y)); y += 1
    P.append(logs("Run timeline", f'{{{sel}, source=~"$source"}}{flt}', 0, y, 24, 22,
                  "Everything the selected runs logged, oldest first. Narrow it with Run, Source, Kind and Search. "
                  "Expand a line for its labels (tool, ok, turn)."))
    y += 22
    P.append(logs("Oversight: JEV decisions, loop guard, progress reviews, compactions",
                  f'{{{sel}}} | kind=~"decision|loop_guard|progress_review|compaction|truncated"', 0, y, 12, 12,
                  "Rameness's JEV decisions and interventions, and every harness's compactions."))
    P.append(logs("Errors: failed tool calls and requests", f'{{{sel}}} | ok="False"', 12, y, 12, 12,
                  "Tool results marked as errors and model requests that did not return 200."))
    y += 12
    P.append(row("Compare two runs", y)); y += 1
    for i, v in enumerate(("run_a", "run_b")):
        P.append(logs(f"${v}", f'{{job="bench", run="${v}", source="agent"}}{flt}', 12 * i, y, 12, 24,
                      "Side by side: the agent's own steps in each run."))
    lv = lambda name, label, q, multi=True, inc=True: {
        "name": name, "label": label, "type": "query", "datasource": LOKI, "query": q, "definition": q,
        "refresh": 2, "multi": multi, "includeAll": inc, "allValue": ".*", "sort": 1,
        "current": {"text": "All", "value": "$__all"} if inc else {}}
    variables = [
        lv("batch", "Batch", 'label_values({job="bench"}, batch)'),
        lv("harness", "Harness", 'label_values({job="bench", batch=~"$batch"}, harness)'),
        lv("run", "Run", 'label_values({job="bench", batch=~"$batch", harness=~"$harness"}, run)'),
        {"name": "source", "label": "Source", "type": "custom", "query": "agent,llm,jev,runner", "multi": True,
         "includeAll": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"}},
        {"name": "kind", "label": "Kind", "type": "custom", "query": ",".join(KINDS), "multi": True,
         "includeAll": True, "allValue": ".*", "current": {"text": "All", "value": "$__all"}},
        {"name": "search", "label": "Search", "type": "textbox", "query": "", "current": {"text": "", "value": ""}},
        lv("run_a", "Compare A", 'label_values({job="bench"}, run)', multi=False, inc=False),
        lv("run_b", "Compare B", 'label_values({job="bench"}, run)', multi=False, inc=False),
    ]
    d = dashboard("harness-logs", "Harness run logs", P, variables,
                  "Every step of every benchmark run, from Loki: the agent's messages, thinking, tool calls and "
                  "results, model requests, and Rameness's JEV decisions and interventions.", hours=24)
    d["refresh"] = "10s"
    d["links"] = [{"title": "Harness comparison", "type": "link", "url": "/d/harness-bench"},
                  {"title": "JEV decision models", "type": "link", "url": "/d/jev-models"}]
    return d


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="/var/lib/grafana/dashboards")
    a = ap.parse_args()
    for name, d in (("jev-models.json", jev_dashboard()), ("harness-bench.json", harness_dashboard()),
                    ("harness-logs.json", logs_dashboard())):
        path = Path(a.out) / name
        path.write_text(json.dumps(d, indent=1))
        print(f"wrote {path} ({len(d['panels'])} panels)")


if __name__ == "__main__":
    main()
