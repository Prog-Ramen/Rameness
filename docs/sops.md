# SOPs

**In brief:** an SOP (standard operating procedure) is a tested procedure the agent calls like a tool,
so a known job runs as code instead of being re-derived by the model. SOPs live in layered folders:
built-in, pulled from the RamenSOPs registry, and your private ones.

## What an SOP is

A small folder: an interface (`sop.json`), code, and the tests that prove it works.

* **Category:** a directory with `_node.json` (`description`, `keywords`, `requires`). `requires` lists
  the information needed to choose among the children, e.g. `data_source`. If neither the task, the org
  context nor the org defaults supply it, traversal **defers** at that node instead of guessing, and the
  router asks.
* **Leaf:** `sop.json` with a `kind` of:
  * `script`: `run.py`/`run.sh`; reads a JSON object of arguments on stdin, prints one JSON object
  * `composite`: steps over other SOPs with `${input.x}` / `${steps.N.field}` templating
  * `skill`: instructions injected into the prompt (for procedures too fuzzy to script)
* **`inputs`** is a JSON schema. **`permissions`** (`fs:read`, `network`, `exec`, `side-effect`, …) are
  enforced deterministically; anything outside `permissions.sop_allow` needs approval. JEV confidence
  never grants permission.
* **`requirements`** lists the Python packages and programs an SOP may need beyond Python and a POSIX
  shell, detected from its imports and the commands it runs (by code, not a model). It is shown with the
  SOP to people and to the agent ("May need programs: jq"); nothing is installed automatically.

## Tests

An SOP is `validated` only when its tests pass; until then it is a `candidate`.

Tests live in `sop.json` and travel with the SOP wherever it is published ([Sharing](sharing.md)). Each
test has an `input` and asserts at least one of:

* `expect`: concrete output values (dotted keys reach into nested results, e.g. `"rows.0.id"`),
* `expect_keys`: output keys that must be present,
* `expect_error: true`: the SOP must fail on this input.

A test can bring **fixtures**: `files` (relative path → text) and `setup` (Python that builds anything
else, e.g. a binary file). Every test runs in a fresh, empty folder with its fixtures, never in your
project, so a test that writes a file cannot overwrite your work. Sharing requires stricter tests
(at least two with concrete values); see [Sharing](sharing.md#tests-go-where-the-sop-goes).

## How the agent sees SOPs

Only tested SOPs are offered, and each says so, so the agent can rely on the interface alone.

* JEV traverses the SOP tree per task and exposes the selected SOPs as normal tools (`sop_http__get`, …);
  the model can pull in more with `sop_search`, which also looks in the registry when nothing local matches
  well ([Pulling](sharing.md#pulling-sops-from-the-registry)).
* Only `validated` SOPs are offered. Candidates (tests failing or missing) are never shown to the agent
  or run on the `direct` route; `rameness sop promote <id>` after review.
* Each SOP's description carries a trust line, e.g. *"Tested: its 4 tests pass, used successfully 3
  times; its output is verified, no need to recompute it."* The prompt tells the agent to call an SOP
  instead of doing its steps by hand, and not to re-check what it returns.
* The agent can save a procedure it just worked out with `sop_save`; it is tested before it is
  registered, and a failed attempt is replaced by the next one rather than left behind as a copy.
* Built-in and registry SOPs run with a scrubbed environment (no model keys, cloud credentials or
  import overrides).

## Where SOPs live

Layered folders; a later layer's SOP with the same id replaces an earlier one.

```
rameness/builtin_sops/      built-in: ships with Rameness (code, data, dev, fs, git, http, resolve)
~/.rameness/public/<pkg>/   registry: public packages pulled from RamenSOPs when a task needs them
~/.rameness/sops/           private: yours, for every project
./.rameness/sops/           private: this project / organization
<your folders>              private: any you choose, e.g. one per use case or session
```

Where an SOP lives decides what it is, and an organization can override a built-in or registry SOP
without forking it.

### Your own private folders

Keep separate SOP libraries for separate use cases or sessions.

* `sops.private` in the config lists more private folders (`~` and project-relative paths work).
* `RAMENESS_SOPS` adds folders for a process (`os.pathsep`-separated).
* `--sops DIR` (repeatable) adds folders for one session.

A session sees only the folders it was given, and they win over the default private folders. New private
SOPs go to `sops.save_to`, else the last folder given, else `./.rameness/sops`. An SOP learned across two
or more projects goes to `~/.rameness/sops` so every project can use it.

**Private stays private:** private SOPs are never proposed, and a private folder inside a git work tree
gets a `.gitignore` that ignores its contents, so the SOPs and their tests can't be committed and pushed
by accident.

## The tree stays small at every level

Like Google's Dremel serving tree: categories stay small, so finding an SOP means a few small steps, never a scan.

The folders are the tree: a category is a folder with a `_node.json`, an SOP is a folder with a `sop.json`, and
an SOP's id is its path (`code.lint.eslint_check` is `code/lint/eslint_check/`). Finding an SOP walks it level
by level; at each level JEV scores only that category's children. So every category is kept small:

* **Filing.** A new SOP is placed by walking down the tree: at each level JEV picks the subcategory it belongs
  in, or keeps it at that level. A deeper path the model proposes is kept, never flattened.
* **Splitting.** When more than 8 SOPs sit directly in a category, it splits inside itself: the model proposes
  groups of related SOPs (each with a name and a one-line description), JEV confirms each member, and each
  group of at least 3 becomes a subcategory. A subcategory that grows past 8 splits the same way, so the tree
  deepens only where SOPs pile up.
* **Folding.** A subcategory left with fewer than 3 SOPs folds back into its parent.
* **Nothing breaks.** A moved SOP keeps its old id as an alias (`_aliases.json`), keeps its usage stats, and
  must still pass its tests in its new place, or it is moved back.

Every tree gets the same rules; who runs them differs:

| Tree | Rebalanced by | Moves land as |
|---|---|---|
| Your private folders | Rameness, right after it adds an SOP | direct changes (they are yours) |
| RamenSOPs | Rameness, while proposing an SOP that would crowd a category | the same PR as the SOP; RamenSOPs CI fails a PR that leaves a category crowded |
| Built-ins | Rameness, while proposing a built-in that would crowd a category | the same draft PR; Rameness CI fails while a built-in category is crowded |

`rameness sop rebalance --root DIR --check` reports crowded categories from the folders alone (no model, no
code run). A proposal is placed in the **target's** tree, not yours: JEV walks the registry's (or the
built-ins') own categories to find where it belongs.

With at most about 8 choices per level, 5,000 SOPs are 4-5 levels: a handful of small listings and a few dozen
JEV questions to find any one of them.

## Keep SOPs simple

Occam's razor: the standard library and the tools every Linux system has.

SOPs are generated with the standard library and standard tools. When a new SOP still needs something
extra, Rameness asks for a standard-only rewrite and keeps it only if it passes the same tests and needs
less (`urllib` instead of `requests`, `json` instead of `jq`); the model can answer that an extra is
unavoidable, and then the original stays. A close SOP is extended rather than duplicated
([Learning](learning.md#extend-before-adding)).

## The organization profile

Private facts and defaults that steer SOP choice and block leaks.

`org.json` holds facts, `defaults` that resolve SOP-tree `requires`, a glossary, and the `private_terms`
the scrubber enforces (see `examples/acme`). It never leaves your machine.

## Commands

```bash
rameness sop tree | list | show ID | search Q | run ID --args '{...}' | test ID | promote ID
rameness sop install <git-url|dir>      # install a public package
rameness sop remote <query>             # search the registry
rameness sop pull <id>                  # pull one registry SOP (see Sharing)
```
