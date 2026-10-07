"""Feature cycles on their own git branch and folder: the project directory always holds the last verified version.

Before the first cycle the verified core is committed (``git init`` first if the project is not a repository). Each
cycle gets a branch ``rameness/cycle-N`` checked out as a git worktree in a sibling folder; the agent's file tools
and shell commands are redirected there. A cycle that finishes verified is merged into the base branch; one that does
not (time limit, crash, max turns) is committed to its branch as unverified work and the base stays as it was.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

# kept out of commits; heavy ones that exist are linked into each cycle's folder so its checks still run
IGNORED = ["node_modules/", ".venv/", "venv/", "__pycache__/", ".rameness/", ".scratch/", "*.pyc", ".DS_Store"]
LINKED = ["node_modules", ".venv", "venv"]
IDENTITY = ["-c", "user.name=rameness", "-c", "user.email=rameness@localhost"]


def available() -> bool:
    return shutil.which("git") is not None


class FeatureBranches:
    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.base = ""                     # the branch that holds the verified version
        self.cycle = 0
        self.folder: Path | None = None    # the current cycle's worktree

    def _git(self, *args: str, cwd: Path | None = None, check: bool = True) -> str:
        r = subprocess.run(["git", *IDENTITY, *args], cwd=cwd or self.root, capture_output=True, text=True, timeout=120)
        if check and r.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {(r.stderr or r.stdout).strip()[:300]}")
        return r.stdout.strip()

    def _commit_all(self, message: str, cwd: Path) -> bool:
        self._git("add", "-A", cwd=cwd)
        if not self._git("status", "--porcelain", cwd=cwd):
            return False
        self._git("commit", "-q", "--no-verify", "-m", message, cwd=cwd)
        return True

    def commit_core(self) -> str:
        """Make the project a repository if needed and commit the verified core on its current branch."""
        if self._git("rev-parse", "--is-inside-work-tree", check=False) != "true":
            self._git("init", "-q", "-b", "main")
        exclude = Path(self._git("rev-parse", "--git-path", "info/exclude"))
        exclude = exclude if exclude.is_absolute() else self.root / exclude
        exclude.parent.mkdir(parents=True, exist_ok=True)
        have = exclude.read_text().splitlines() if exclude.exists() else []
        exclude.write_text("\n".join(have + [p for p in IGNORED if p not in have]) + "\n")
        self.base = self._git("symbolic-ref", "--short", "-q", "HEAD", check=False)   # works before the first commit
        if not self.base:
            raise RuntimeError("the project is on a detached HEAD; feature branches need a branch to merge into")
        self._commit_all("rameness: verified core", self.root)
        return self.base

    def start(self, n: int) -> Path:
        """Branch rameness/cycle-n from the verified base into its own folder next to the project."""
        self.cycle = n
        self.folder = self.root.parent / f".{self.root.name}-rameness-cycle-{n}"
        if self.folder.exists():
            self._git("worktree", "remove", "--force", str(self.folder), check=False)
            shutil.rmtree(self.folder, ignore_errors=True)
        self._git("worktree", "prune", check=False)
        self._git("branch", "-D", f"rameness/cycle-{n}", check=False)
        self._git("worktree", "add", "-q", "-b", f"rameness/cycle-{n}", str(self.folder), self.base)
        for name in LINKED:
            if (self.root / name).is_dir() and not (self.folder / name).exists():
                (self.folder / name).symlink_to(self.root / name, target_is_directory=True)
        return self.folder

    def merge(self) -> str:
        """The cycle passed its checks: commit it and merge it into the base branch (the project directory)."""
        n, folder = self.cycle, self.folder
        self._unlink(folder)
        self._commit_all(f"rameness: feature cycle {n} (verified)", folder)
        self._commit_all("rameness: changes made outside the cycle folder", self.root)
        r = subprocess.run(["git", *IDENTITY, "merge", "--no-edit", "-q", f"rameness/cycle-{n}"], cwd=self.root,
                           capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            self._git("merge", "--abort", check=False)
            return f"cycle {n} not merged (conflict); its work stays on branch rameness/cycle-{n}"
        self._remove(folder)
        self.folder = None
        return f"merged rameness/cycle-{n} into {self.base}"

    def abandon(self) -> str:
        """The cycle did not finish verified: keep its work on its branch, leave the base as it was."""
        n, folder = self.cycle, self.folder
        if folder is None:
            return ""
        self._unlink(folder)
        self._commit_all(f"rameness: feature cycle {n} (unfinished, not verified)", folder)
        self._remove(folder)
        self.folder = None
        return f"cycle {n} unfinished: its work is on branch rameness/cycle-{n}; {self.base} keeps the verified version"

    def _unlink(self, folder: Path) -> None:
        for name in LINKED:
            if (folder / name).is_symlink():
                (folder / name).unlink()

    def _remove(self, folder: Path) -> None:
        self._git("worktree", "remove", "--force", str(folder), check=False)
        shutil.rmtree(folder, ignore_errors=True)

    def redirect_command(self, command: str) -> str:
        """Point a shell command that names the project directory at the cycle folder instead."""
        if self.folder is None:
            return command
        return re.sub(re.escape(str(self.root)) + r"(?=/|\s|$|['\";:)])", str(self.folder), command)

    def redirect_path(self, path: Path) -> Path:
        if self.folder is None:
            return path
        try:
            return self.folder / Path(path).resolve().relative_to(self.root)
        except ValueError:
            return path
