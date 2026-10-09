"""Is an SOP safe to share? Code finds the risky constructs, the model reviews the whole script for risks, and
JEV decides: share it, fix it first, or keep it private.

An SOP's inputs come from whoever calls it (an agent, or anyone who pulls it from the registry), so a script that
builds a shell command from an input, evaluates input as code, or writes outside its folder is a risk to its
users. The checks run before an SOP is shared; a "fix" verdict goes back to the model with the findings
(learning.finish_sop), and the review is redone on the fixed code.
"""
from __future__ import annotations

import ast
import hashlib
import json
import time

from . import schemas
from .jev import Jev, Option
from .sops import SOP

SECURITY_SYSTEM = """You review a small Python script (an SOP) for injection risks. The SOP is called by an AI agent
working on the user's machine. The agent can already run any command itself, so a command, script or code that the
caller passes in on purpose (an input whose documented job is to be run, such as a test command) is fine, and so is
running code the SOP itself extracts or builds when that is its stated job. The risk is DATA: an input that is a
path, file name, URL, name, text or file content (which the agent copies from a repository, a web page or a file
it did not write) and that the script puts into a shell command string (sh -c, shell=True, os.system), passes to
eval or exec, or otherwise lets be interpreted as code. Whoever wrote that data would then control what runs. Do not
report network access, file writes, missing timeouts or anything else. Check each construct the static scan lists
and say whether it is a real data-injection risk. Report only real ones, with the line. Reply {"risks": [{"line": n,
"kind": "data injection", "detail": "which input reaches what", "severity": "high"}]}; an empty list if there
are none. Output JSON only."""

SHELL_RUNNERS = {"sh", "bash", "zsh", "dash"}


def _name(node: ast.AST) -> str:
    """Dotted name of a call target: subprocess.run, os.system, eval, ..."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    return ""


def _constant(node: ast.AST) -> bool:
    return isinstance(node, ast.Constant) or (isinstance(node, (ast.List, ast.Tuple))
                                              and all(_constant(e) for e in node.elts))


def static_risks(source: str) -> list[dict]:
    """Constructs where an input could be run, found by reading the code (no model): a shell command that is not a
    constant string, os.system/os.popen, eval/exec/compile of something not constant, dynamic imports. They are
    hints: whether the input that reaches them is a command the caller means to run (fine) or data (a risk) is
    for the model to say."""
    try:
        tree = ast.parse(source)
    except SyntaxError as e:
        return [{"line": e.lineno or 0, "kind": "syntax", "detail": "does not parse", "severity": "high"}]
    risks = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        name = _name(node.func)
        line = getattr(node, "lineno", 0)
        kw = {k.arg: k.value for k in node.keywords if k.arg}
        if name.startswith("subprocess.") and isinstance(kw.get("shell"), ast.Constant) and kw["shell"].value is True:
            if not (node.args and _constant(node.args[0])):
                risks.append({"line": line, "kind": "shell injection", "severity": "high",
                              "detail": f"{name}(..., shell=True) with a command that is not a constant string"})
        if name.startswith("subprocess.") and node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
            argv = node.args[0].elts
            if len(argv) >= 3 and isinstance(argv[0], ast.Constant) and str(argv[0].value).split("/")[-1] in \
                    SHELL_RUNNERS and isinstance(argv[1], ast.Constant) and argv[1].value == "-c" \
                    and not _constant(argv[2]):
                risks.append({"line": line, "kind": "shell injection", "severity": "high",
                              "detail": f"{argv[0].value} -c with a command string built at run time"})
        if name in ("os.system", "os.popen") and not (node.args and _constant(node.args[0])):
            risks.append({"line": line, "kind": "shell injection", "severity": "high",
                          "detail": f"{name} with a command that is not a constant string"})
        if name in ("eval", "exec", "compile", "builtins.eval", "builtins.exec") and \
                not (node.args and _constant(node.args[0])):
            risks.append({"line": line, "kind": "code execution", "severity": "high",
                          "detail": f"{name} of something that is not a constant"})
        if name in ("__import__", "importlib.import_module") and not (node.args and _constant(node.args[0])):
            risks.append({"line": line, "kind": "code execution", "severity": "medium",
                          "detail": f"{name} of a module named at run time"})
    return sorted(risks, key=lambda r: r["line"])


def runs_processes(source: str) -> bool:
    """Does the script start other programs (so it must declare the exec permission)?"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return False
    return any(isinstance(n, ast.Call) and (_name(n.func).startswith(("subprocess.", "os.exec", "os.spawn"))
                                           or _name(n.func) in ("os.system", "os.popen"))
               for n in ast.walk(tree))


def code_hash(sop: SOP) -> str:
    """The reviewed code; a change to it makes the review stale."""
    h = hashlib.sha256()
    for f in sorted(sop.path.glob("run.*")):
        h.update(f.read_bytes())
    return h.hexdigest()


def review(sop: SOP, llm, jev: Jev) -> dict:
    """Find the risks (code + the model reading the whole script), then JEV decides the verdict:
    ``safe`` (share it), ``fix`` (fix the risks first), or ``private`` (not for sharing)."""
    src = "".join(f.read_text(errors="replace") for f in sorted(sop.path.glob("run.*")))
    static = static_risks(src)
    risks = []
    if static and llm is None:                   # something could run an input, and nothing can tell command from data
        return {"verdict": "unreviewed", "risks": static, "probs": {}, "code": code_hash(sop), "t": time.time(),
                "error": "the script runs inputs and no model was available to review them"}
    if static and llm is not None:
        numbered = "\n".join(f"{i + 1:4d}  {line}" for i, line in enumerate(src.splitlines()))
        try:
            r = llm.complete_json(SECURITY_SYSTEM, (
                f"SOP {sop.id}: {sop.description}\nInputs: {json.dumps(sop.inputs)[:1500]}\n"
                f"Static scan findings: {json.dumps(static) if static else 'none'}\n\nScript:\n{numbered[:30000]}"),
                max_tokens=6000, schema=schemas.SOP_SECURITY, thinking=2048)
            for x in (r or {}).get("risks") or []:
                if isinstance(x, dict):
                    risks.append({"line": int(x.get("line") or 0), "kind": str(x.get("kind", ""))[:60],
                                  "detail": str(x.get("detail", ""))[:300], "severity": str(x.get("severity", "medium"))})
        except Exception as e:                   # not reviewed is not safe: try again later
            return {"verdict": "unreviewed", "risks": risks, "probs": {}, "code": code_hash(sop), "t": time.time(),
                    "error": f"the model review did not complete: {type(e).__name__}"}
    if not risks:
        verdict, probs = "safe", {"safe": 1.0}
    else:
        # JEV reads the findings (short, line by line), not the whole script
        state = (f"SOP {sop.id}: {sop.description}\nRisks found:\n" +
                 "\n".join(f"- line {x['line']} [{x['severity']}] {x['kind']}: {x['detail']}" for x in risks))
        d = jev.choose("Is this SOP safe to share as it is, should these data-injection risks be fixed first, or should it stay private?",
                       state,
                       [Option("safe", "safe harmless low risk theoretical acceptable documented as intended",
                               desc="The findings are harmless or intended: share it as it is."),
                        Option("fix", "fix injection unsafe shell quoting traversal fixable risk high medium",
                               desc="Real risks that a change to the code can remove: fix them, then share."),
                        Option("private", "inherently dangerous destructive arbitrary execution by design purpose",
                               desc="Risky by design: keep it private, never share it.")])
        verdict, probs = d.top(1)[0][0], d.probs
    return {"verdict": verdict, "risks": risks, "probs": probs, "code": code_hash(sop), "t": time.time()}


def record(sop: SOP, result: dict) -> None:
    data = json.loads((sop.path / "sop.json").read_text())
    data.setdefault("origin", {})["security"] = result
    (sop.path / "sop.json").write_text(json.dumps(data, indent=2) + "\n")


def current(sop: SOP) -> dict | None:
    """The recorded review, if it is for the code as it is now (an unfinished review does not count)."""
    sec = (sop.origin or {}).get("security")
    return sec if isinstance(sec, dict) and sec.get("code") == code_hash(sop) and sec.get("verdict") != "unreviewed" \
        else None
