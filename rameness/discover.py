"""SOP discovery: find the standard procedures agents keep redoing by hand, from their event logs.

Rameness's learner mines *repeated step sequences* in successful runs. The procedures that cost agents the
most rarely look like that: starting and restarting a server is never the same command twice (kill, fuser,
nohup, a different port...), and hunting a syntax error is spread over failing stretches. So discovery mines
for *cost* instead:

1. Every run becomes a list of steps. The model's own generative work (writing, editing and reading files,
   planning) is set aside: that is LLM work and never an SOP.
2. The remaining steps are grouped by what they do: the programs a command runs, the kind of output it gets
   back (an error, a port conflict...), and the model's own words for what it is doing at that turn.
3. Each group is costed (steps, turns, tool time, failures) and counted across runs, models and harnesses.
4. Its trigger is inferred from where it happens: right after the work produced an output (hook), at the start
   or end of a run (hook), or at points the model chose (tool).
5. JEV screens the most expensive groups: a mechanical procedure, judgement work, or incidental noise; and
   whether it would carry over to other tasks with only its parameters changed.

Nothing is generated: the output is a report of candidate SOPs to consider.

    rameness sop discover <events.jsonl | directory> ... [--top 12] [--json]
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path

GENERATIVE = {"write_file", "edit_file", "edit_lines", "read_file", "todo_write", "recall", "glob", "grep", "sop_search",
              "sop_save"}
_STOP = set("""a an the and or of to in on for with by from at as is are was were be been it this that these those i
you we they me my our your can could would should will just into about using use then than so do does did not no
yes if any all some let lets now next check see make sure also first need needs try again still one two new""".split())
_SIGNALS = {                      # what came back, as a group feature (not the full output)
    "EADDRINUSE": "sig:port-in-use", "Address already in use": "sig:port-in-use",
    "SyntaxError": "sig:syntax-error", "Unexpected token": "sig:syntax-error", "command not found": "sig:missing-tool",
    "No such file": "sig:missing-file", "Cannot find module": "sig:missing-module", "ModuleNotFoundError": "sig:missing-module",
    "Permission denied": "sig:permission", "timed out": "sig:timeout", "ECONNREFUSED": "sig:conn-refused",
    "Connection refused": "sig:conn-refused", "404": "sig:http-404",
}


@dataclass
class Step:
    run: str
    turn: int
    tool: str
    text: str                     # the command / input, as the model wrote it
    ok: bool
    out: str
    seconds: float
    intent: str                   # the model's words at that turn
    pos: float = 0.0              # position in the run, 0..1
    prev_tool: str = ""
    tokens: Counter = field(default_factory=Counter)


# --------------------------------------------------------------------------- loading

def load_events(path: Path, run: str | None = None) -> list[Step]:
    """Steps from a Rameness event log (RAMENESS_EVENT_LOG)."""
    ev = [json.loads(ln) for ln in path.read_text(errors="replace").splitlines() if ln.strip()]
    intent: dict[int, str] = {}
    for e in ev:
        if e.get("kind") == "llm":
            intent[e.get("turn", -1)] = " ".join(filter(None, [e.get("text", ""), e.get("reasoning", "")[:600]]))
    steps = []
    for e in ev:
        if e.get("kind") != "tool":
            continue
        cmd = _command(e.get("input", ""))
        steps.append(Step(run or path.parent.name, e.get("turn", 0), e.get("tool", ""), str(cmd), bool(e.get("ok", True)),
                          e.get("output", ""), float(e.get("seconds") or 0), intent.get(e.get("turn", -1), "")))
    return _finish(steps)


def _command(raw: str) -> str:
    """The command (or url/path) from a logged tool input, which may be JSON cut off at the log's size limit."""
    try:
        d = json.loads(raw)
        return str(d.get("command") or d.get("url") or d.get("path") or raw)
    except (json.JSONDecodeError, AttributeError):
        m = re.search(r'"(?:command|url|path)":\s*"((?:[^"\\]|\\.)*)', raw)
        if not m:
            return raw
        body = m.group(1)
        for cut in range(0, 8):                   # a truncated escape at the end: drop characters until it decodes
            try:
                return json.loads('"' + body[: len(body) - cut] + '"')
            except json.JSONDecodeError:
                continue
        return body


def load_steps(dicts: list[dict], run: str) -> list[Step]:
    """Steps from already-normalized dicts {turn, tool, text, ok, out, seconds, intent} (other harnesses)."""
    return _finish([Step(run, d.get("turn", i), d["tool"], d.get("text", ""), d.get("ok", True), d.get("out", ""),
                         d.get("seconds", 0.0), d.get("intent", "")) for i, d in enumerate(dicts)])


def _finish(steps: list[Step]) -> list[Step]:
    n = max(1, len(steps) - 1)
    for i, s in enumerate(steps):
        s.pos = i / n
        s.prev_tool = steps[i - 1].tool if i else ""
        s.tokens = features(s)
    return steps


# --------------------------------------------------------------------------- features and grouping

GLUE = {"head", "tail", "grep", "sed", "awk", "cat", "ls", "wc", "sort", "uniq", "tr", "cut", "xargs", "tee", "printf",
        "test", "[", "if", "for", "while", "read", "exit", "return", "local", "mkdir", "rm", "cp", "mv", "touch"}
_HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?.*?\n(.*?)\n\s*\1\b", re.S)


def _programs(cmd: str) -> list[str]:
    """The programs a shell command runs, with the sub-command that names what it does (npm test, python3 -m x).
    Heredoc bodies (a script being written) and comments are not commands."""
    cmd = _HEREDOC.sub(lambda m: "<<" + m.group(1), cmd)
    cmd = re.sub(r"<<-?\s*['\"]?\w+['\"]?[\s\S]*$", "", cmd) if "<<" in cmd and "\n" in cmd else cmd
    # quoted strings are data (an inline script for node -e / python3 -c, a message), not commands
    inline = bool(re.search(r"\b(node|python3?|deno|bun|ruby|perl)\s+(-e|-c|--eval)\s", cmd))
    cmd = re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", " STR ", cmd, flags=re.S)
    cmd = re.sub(r"#[^\n]*", "", cmd)
    out = ["inline-script"] if inline else []
    for seg in re.split(r"&&|\|\||;|\||\n|\(|\)|`|\$\(", cmd):
        w = [x for x in seg.strip().split() if not re.match(r"^\w+=", x)]      # drop VAR=value prefixes
        while w and w[0] in ("sudo", "nohup", "timeout", "env", "exec", "time") or (w and re.match(r"^\d+s?$", w[0])):
            w = w[1:]
        if not w or w[0] in ("cd", "echo", "sleep", "true", "false", "set", "export", "then", "fi", "do", "done"):
            continue
        prog = Path(w[0].strip("\"'")).name
        if not re.match(r"^[A-Za-z][\w.+-]*$", prog):
            continue
        sub = ""
        if prog in ("python", "python3") and len(w) > 2 and w[1] == "-m":
            sub = w[2]
        elif prog in ("npm", "npx", "git", "pip", "pip3", "docker", "cargo", "go", "yarn", "pnpm") and len(w) > 1:
            sub = w[1]
        elif prog in ("node", "python", "python3", "bash", "sh") and len(w) > 1 and not w[1].startswith("-"):
            sub = Path(w[1]).suffix or "script"
        out.append(f"{prog}:{sub}" if sub else prog)
    return out


def _words(text: str, limit: int = 40) -> list[str]:
    return [w for w in re.findall(r"[a-z][a-z-]{2,}", text.lower()[:1200]) if w not in _STOP][:limit]


def features(s: Step) -> Counter:
    f = Counter()
    if s.tool == "bash":
        for p in _programs(s.text):
            base = p.split(":")[0]
            w = 0.5 if base in GLUE else 3          # pipes through head/grep say little about the step's purpose
            f["prog:" + p] += w
            f["prog:" + base] += w / 3
        if re.search(r"&\s*($|\n|;)|nohup|disown|setsid", s.text):
            f["bg"] += 2
        for m in re.findall(r"(?:localhost|127\.0\.0\.1):?(\d{2,5})|--port[ =]\d+|-p \d+", s.text):
            f["port"] += 2
    else:
        f["tool:" + s.tool] += 4
    for k, sig in _SIGNALS.items():
        if k in s.out[:3000]:
            f[sig] += 2
    for w in _words(s.intent):
        f["w:" + w] += 0.5
    return f


def _cos(a: Counter, b: Counter) -> float:
    dot = sum(v * b.get(k, 0) for k, v in a.items())
    na, nb = math.sqrt(sum(v * v for v in a.values())), math.sqrt(sum(v * v for v in b.values()))
    return dot / (na * nb) if na and nb else 0.0


@dataclass
class Group:
    steps: list[Step] = field(default_factory=list)
    centroid: Counter = field(default_factory=Counter)

    def add(self, s: Step) -> None:
        self.steps.append(s)
        for k, v in s.tokens.items():
            self.centroid[k] += v

    # ---------------------------------------------------------------- summary

    @property
    def runs(self) -> set[str]:
        return {s.run for s in self.steps}

    def turns(self) -> int:
        return len({(s.run, s.turn) for s in self.steps})

    def failures(self) -> int:
        return sum(1 for s in self.steps if not s.ok or re.search(r"^exit=(?!0\b)", s.out) or
                   any(k in s.out[:2000] for k in ("Error", "error:", "EADDRINUSE", "not found")))

    def label(self, n: int = 5) -> list[str]:
        keys = [k for k, _ in self.centroid.most_common(40) if not k.startswith("w:")]
        words = [k[2:] for k, _ in self.centroid.most_common(60) if k.startswith("w:")]
        return keys[:n] + words[:4]

    def trigger(self) -> tuple[str, dict]:
        """Where the procedure happens -> how it should run: a hook on an event, or a tool the model calls."""
        c = Counter()
        for s in self.steps:
            if s.prev_tool in ("write_file", "edit_file", "edit_lines") or (s.tool == "bash" and "sig:syntax-error" in s.tokens):
                c["after_output"] += 1
            elif s.pos < 0.08:
                c["on_start"] += 1
            elif s.pos > 0.92:
                c["before_finish"] += 1
            else:
                c["tool"] += 1
        top, n = c.most_common(1)[0]
        share = n / max(1, sum(c.values()))
        kind = f"hook:{top}" if top != "tool" and share >= 0.5 else "tool"
        return kind, {k: round(v / max(1, sum(c.values())), 2) for k, v in c.items()}


def group(steps: list[Step], threshold: float = 0.45) -> list[Group]:
    groups: list[Group] = []
    for s in steps:
        if s.tool in GENERATIVE or not s.tokens:
            continue
        best, sim = None, 0.0
        for g in groups:
            c = _cos(s.tokens, g.centroid)
            if c > sim:
                best, sim = g, c
        if best is not None and sim >= threshold:
            best.add(s)
        else:
            g = Group()
            g.add(s)
            groups.append(g)
    return groups


# --------------------------------------------------------------------------- screening and report

VERDICTS = None


def screen(g: Group, jev) -> dict:
    """JEV: is this a mechanical procedure (SOP), judgement work (LLM), or incidental noise; and is it generic."""
    from .jev import Option
    ex = [s.text.replace("\n", " ")[:160] for s in g.steps[:6]]
    said = [s.intent.replace("\n", " ")[:160] for s in g.steps[:4] if s.intent]
    state = (f"Activity seen {len(g.steps)} times in {len(g.runs)} runs; {g.failures()} of them failed.\n"
             f"Features: {', '.join(g.label(8))}\nExample commands:\n- " + "\n- ".join(ex)
             + ("\nWhat the agent said it was doing:\n- " + "\n- ".join(said) if said else ""))
    kind = jev.choose("What kind of work is this recurring agent activity?", state, [
        Option("procedure", "mechanical repeated same steps setup start stop restart check install run server port "
                            "environment boilerplate", desc="A mechanical procedure done the same way each time "
                                                            "(only parameters change); a script could do it."),
        Option("judgement", "debugging reasoning design creative analysis investigate understand decide",
               desc="Work that needs reasoning or creativity each time (debugging, design, analysis)."),
        Option("noise", "one-off incidental unrelated trivial listing", desc="Incidental one-off activity, not worth "
                                                                            "automating."),
    ])
    generic = jev.yes("Would this procedure work the same way in other kinds of tasks, with only its parameters changed?",
                      state, "generic reusable any project any task parameters port path command",
                      "specific this task only one project unique",
                      yes_desc="Yes: it is generic and would carry over to other tasks.",
                      no_desc="No: it is specific to this task.")
    return {"kind": kind.best, "kind_probs": kind.probs, "generic": round(generic, 3)}


def discover(runs: dict[str, list[Step]], jev=None, top: int = 12, min_runs: int = 2) -> list[dict]:
    steps = [s for ss in runs.values() for s in ss]
    groups = [g for g in group(steps) if len(g.runs) >= min_runs]
    total_turns = sum(len({s.turn for s in ss}) for ss in runs.values()) or 1
    groups.sort(key=lambda g: -(g.turns() + 0.5 * g.failures()))
    out = []
    for g in groups[:top]:
        trig, where = g.trigger()
        row = {"label": g.label(), "steps": len(g.steps), "turns": g.turns(),
               "share_of_turns": round(g.turns() / total_turns, 3), "runs": len(g.runs),
               "failures": g.failures(), "tool_seconds": round(sum(s.seconds for s in g.steps)),
               "trigger": trig, "where": where,
               "examples": [s.text.replace("\n", " ")[:140] for s in _spread(g.steps, 4)]}
        if jev is not None:
            row.update(screen(g, jev))
        out.append(row)
    return out


def _spread(xs: list, n: int) -> list:
    if len(xs) <= n:
        return xs
    return [xs[round(i * (len(xs) - 1) / (n - 1))] for i in range(n)]


def report(rows: list[dict], runs: int, turns: int) -> str:
    lines = [f"Candidate SOPs from {runs} runs ({turns} model turns). Nothing generated; ranked by turns spent.", ""]
    for i, r in enumerate(rows, 1):
        verdict = ""
        if "kind" in r:
            verdict = f"  JEV: {r['kind']} ({r['kind_probs'][r['kind']]:.2f}), generic {r['generic']:.2f}"
        lines.append(f"{i:2}. {' '.join(r['label'][:6])}")
        lines.append(f"    {r['turns']} turns ({r['share_of_turns']:.0%}) / {r['steps']} steps in {r['runs']} runs, "
                     f"{r['failures']} failed, {r['tool_seconds']}s in tools; trigger: {r['trigger']} {r['where']}{verdict}")
        for e in r["examples"]:
            lines.append(f"      $ {e}")
    return "\n".join(lines)


def load_paths(paths: list[str]) -> dict[str, list[Step]]:
    runs: dict[str, list[Step]] = {}
    for p in paths:
        pp = Path(p)
        for f in ([pp] if pp.is_file() else sorted(pp.rglob("events.jsonl"))):
            name = f.parent.name
            if name in runs:                   # the same run copied into results and still in its run dir
                continue
            runs[name] = load_events(f, name)
    return runs
