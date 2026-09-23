#!/usr/bin/env bash
# rameness installer: one self-contained environment with the harness, the fleet manager,
# the UI, the JEV tooling and both model SDKs (Anthropic + OpenAI-compatible for
# llama-server / ollama / vLLM / DeepSeek...).
#
#   curl -fsSL https://raw.githubusercontent.com/Prog-Ramen/Rameness/main/install.sh | bash
#   ./install.sh                       # from a checkout
#   ./install.sh --prefix /opt/rameness --with-herdr
#
# Options:
#   --prefix DIR     where the environment lives        (default: ~/.rameness/env)
#   --bin DIR        where the `rameness` launcher goes  (default: ~/.local/bin)
#   --source SRC     pip source: path, git URL or wheel (default: this checkout, else the GitHub repo)
#   --with-herdr     also install herdr (agent session runtime) via its official installer
#   --dev            editable install (for working on rameness itself)
set -euo pipefail

PREFIX="${RAMENESS_PREFIX:-$HOME/.rameness/env}"
BIN="$HOME/.local/bin"
SOURCE=""
HERDR=0
DEV=0
while [ $# -gt 0 ]; do
  case "$1" in
    --prefix) PREFIX="$2"; shift 2;;
    --bin) BIN="$2"; shift 2;;
    --source) SOURCE="$2"; shift 2;;
    --with-herdr) HERDR=1; shift;;
    --dev) DEV=1; shift;;
    -h|--help) sed -n '2,17p' "$0"; exit 0;;
    *) echo "unknown option: $1" >&2; exit 2;;
  esac
done

say() { printf '\033[1;35m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m!!\033[0m %s\n' "$*" >&2; }

# ---- python
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
    PY="$(command -v "$c")"; break
  fi
done
[ -n "$PY" ] || { echo "rameness needs Python >= 3.10" >&2; exit 1; }
command -v git >/dev/null || warn "git not found: worktrees and merges need it"

# ---- source
HERE="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || true)"
if [ -z "$SOURCE" ]; then
  if [ -n "$HERE" ] && [ -f "$HERE/pyproject.toml" ] && grep -q 'name = "rameness"' "$HERE/pyproject.toml"; then
    SOURCE="$HERE"
  else
    SOURCE="git+https://github.com/Prog-Ramen/Rameness.git"
  fi
fi

# ---- environment
say "creating environment at $PREFIX (python: $PY)"
mkdir -p "$(dirname "$PREFIX")"
if command -v uv >/dev/null 2>&1; then
  uv venv -q --python "$PY" "$PREFIX"
  PIP=(uv pip install -q --python "$PREFIX/bin/python")
else
  "$PY" -m venv "$PREFIX"
  "$PREFIX/bin/python" -m pip install -q --upgrade pip
  PIP=("$PREFIX/bin/python" -m pip install -q)
fi
say "installing rameness from $SOURCE"
if [ "$DEV" = 1 ]; then "${PIP[@]}" -e "$SOURCE"; else "${PIP[@]}" "$SOURCE"; fi

# ---- launcher
mkdir -p "$BIN"
cat > "$BIN/rameness" <<LAUNCH
#!/usr/bin/env bash
exec "$PREFIX/bin/python" -m rameness "\$@"
LAUNCH
chmod +x "$BIN/rameness"

# ---- defaults (never overwrite existing config)
mkdir -p "$HOME/.rameness"
[ -f "$HOME/.rameness/fleet.json" ] || cat > "$HOME/.rameness/fleet.json" <<'CFG'
{
  "backend": "auto",
  "mode": "review",
  "privacy": "any",
  "environments": [],
  "slots": [],
  "scan_ports": [],
  "decision_policy": {"*": "auto"}
}
CFG

# ---- optional session runtimes
if [ "$HERDR" = 1 ]; then
  say "installing herdr (https://herdr.dev)"
  curl -fsSL https://herdr.dev/install.sh | sh
fi
for t in herdr tmux screen; do command -v $t >/dev/null && say "session backend available: $t"; done
command -v tmux >/dev/null || command -v herdr >/dev/null || command -v screen >/dev/null || \
  warn "no tmux/screen/herdr: agents will run headless (install tmux or pass --with-herdr to watch them live)"

"$PREFIX/bin/python" -m rameness --version >/dev/null
say "installed: $BIN/rameness"
case ":$PATH:" in *":$BIN:"*) ;; *) warn "add $BIN to your PATH";; esac
cat <<'NEXT'

Next:
  rameness init                  # in a project: creates .rameness/ (config, org profile, SOPs)
  rameness fleet slots           # models found: API keys, llama-server / ollama / vLLM ports, CLI agents
  rameness fleet envs            # environments (add ssh / container ones in ~/.rameness/fleet.json)
  rameness up                    # manager + UI at http://127.0.0.1:7788
  rameness run "fix the failing test"                      # single agent
  rameness --base-url http://localhost:8080/v1 run "..."   # any llama-server on a port
NEXT
