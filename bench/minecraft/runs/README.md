# Minecraft bench runs

One folder per batch, one sub-folder per run (`<batch>-<harness>-<model>`), exported from the bench machine by
`python3 -m bench.minecraft.archive`. What each run keeps:

| File | What it is |
|---|---|
| `result.json` | The 14 behaviour checks, score, wall time, request counts |
| `judge.json` | The visual judge's scores and notes for the final screenshot |
| `events.jsonl.gz` | Rameness's event log (turns, tool calls, JEV decisions, reviews), long fields cut to 300 characters |
| `llm.jsonl.gz` | One row per model request: tokens, cache, timing, HTTP status |
| `index.html` | The generated game's entry page (its other files are not kept) |

Left out to keep this small: the rest of each generated game, screenshots, raw console output and full tool
inputs. LAN addresses and the host name are replaced with `<lan-host>` and `<host>`.

`../launchers/` holds the scripts that queued the runs and their logs; `../reports/` the reports written during
the runs. The history of what changed between runs is in [`docs/iterations.md`](../../../docs/iterations.md).
Read a log with `zcat events.jsonl.gz | jq .` or Python's `gzip` module.
