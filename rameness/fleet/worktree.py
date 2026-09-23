"""Per-agent git worktrees, created inside the agent's environment (local or remote).

Deliver-type agents get ``rameness/<agent-id>`` branched from the parent's branch
(or the repo HEAD), so parallel agents never collide and a lead's children
branch from the lead's work. Research agents share the repo read-only.
"""

from __future__ import annotations

import shlex

from .envs import Environment


def is_repo(env: Environment, path: str) -> bool:
    return env.run("git rev-parse --is-inside-work-tree", path).out.strip() == "true"


def create(env: Environment, repo: str, agent_id: str, base: str | None = None) -> tuple[str, str]:
    branch = f"rameness/{agent_id}"
    top = env.run("git rev-parse --show-toplevel", repo).out.strip() or repo
    path = f"{top}/.rameness/worktrees/{agent_id}"
    base = base or env.run("git rev-parse HEAD", top).out.strip()
    r = env.run(f"git worktree add -b {shlex.quote(branch)} {shlex.quote(path)} {shlex.quote(base)}", top)
    if r.code:
        raise RuntimeError(f"git worktree add failed: {r.err.strip()}")
    # keep worktrees out of the main tree's status
    env.run("mkdir -p .rameness && (grep -qx 'worktrees/' .rameness/.gitignore 2>/dev/null || echo 'worktrees/' >> .rameness/.gitignore)", top)
    return path, branch


def diffstat(env: Environment, path: str, base: str = "HEAD") -> str:
    env.run(f"git add -A -- . {EXCLUDE}", path)
    return env.run(f"git diff --cached --stat {shlex.quote(base)}", path).out.strip()


EXCLUDE = "':(exclude)**/__pycache__/**' ':(exclude)**/*.pyc' ':(exclude).rameness/**'"


def commit_all(env: Environment, path: str, message: str) -> bool:
    env.run(f"git add -A -- . {EXCLUDE}", path)
    if env.run("git diff --cached --quiet", path).code == 0:
        return False
    r = env.run(f"git -c user.name=rameness -c user.email=rameness@localhost commit -q -m {shlex.quote(message)}", path)
    return r.code == 0


def merge(env: Environment, repo: str, branch: str, into: str | None = None) -> tuple[bool, str]:
    top = env.run("git rev-parse --show-toplevel", repo).out.strip() or repo
    if into:
        env.run(f"git checkout -q {shlex.quote(into)}", top)
    r = env.run(f"git -c user.name=rameness -c user.email=rameness@localhost merge --no-ff -q -m "
                f"{shlex.quote('merge ' + branch)} {shlex.quote(branch)}", top)
    if r.code:
        env.run("git merge --abort", top)
        return False, (r.out + r.err).strip()
    return True, env.run("git log -1 --oneline", top).out.strip()


def remove(env: Environment, path: str, branch: str | None = None, delete_branch: bool = False) -> None:
    top = env.run("git rev-parse --show-toplevel", path + "/..").out.strip()
    env.run(f"git worktree remove --force {shlex.quote(path)}", top or ".")
    if branch and delete_branch:
        env.run(f"git branch -D {shlex.quote(branch)}", top or ".")
