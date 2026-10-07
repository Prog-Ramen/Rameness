# Speed summary: prefill and decode (CMP 170HX, plus RTX 5070 Ti where noted)

Generated 2026-10-06 from `/root/bench/compare/speed.jsonl`.

- **Prefill** = prompt tokens / time to first token, for a 32k-token prompt reprocessed from scratch (the `edit` rows
  at depth 32000). Deeper `edit` rows are left out because they partly reuse the cache.
- **Decode** = generation tok/s at each context depth; ranges cover the repeat measurements (`fresh` and `edit`).
- "—" = not measured at that depth (or the context did not fit).

| Setup | Prefill (32k), tok/s | Decode 1k | Decode 32k | Decode 96k | Decode 160k |
|---|---|---|---|---|---|
| **Flash-Next AutoRound 3bpw, vLLM + MTP 3** | **3,560** | **152–185** | **146–150** | **140–163** | **146–180** |
| Flash-Next AutoRound 3bpw, vLLM + MTP 1 | 3,520 | 116–122 | 112–114 | 112–117 | 113–119 |
| Flash-Next AutoRound 3bpw, vLLM, no speculation | 3,520 | 73 | 72 | 71–72 | 71–72 |
| 27B BF16, vLLM, no speculation | 2,136 | 21 | 21 | 19 | — |
| 27B BF16, vLLM + MTP | 2,054 | 56–63 | 35–44 | — | — |
| Flash-Next EXL3 3.05bpw, TabbyAPI, no MTP | 1,965 | 53 | 52 | 51–52 | 51 |
| 27B W4A16, vLLM, no speculation | 1,841 | 55 | 50 | 42 | 36 |
| 27B W4A16, vLLM + DFlash2 | 1,800 | 151–207 | 119–130 | 29–63 | 29–43 |
| 27B W4A16, vLLM + MTP | 1,770 | 114–125 | 65–67 | 32–36 | 24–26 |
| 27B W4A16, TnzGit (Reddit) recipe + DFlash2 | 1,741 | 141–160 | 142–149 | 106–110 | 84–93 |
| Flash-Next EXL3 3.05bpw, TabbyAPI + MTP | 1,594 | 55–113 | 98–100 | 106–112 | 99–114 |
| 27B W8A16, vLLM + DFlash2 | 1,548 | 114–129 | 86–89 | 47–48 | 28–35 |
| 27B W8A16, vLLM + MTP | 1,526 | 86–95 | 54–61 | 27–34 | 20–22 |
| 27B EXL3 4bpw, TensorFold + DFlash2 | 1,483 | 92–136 | 92–120 | 78–96 | — |
| Flash-Next EXL3 4.05bpw, TabbyAPI + MTP | 1,020 | 48–76 | 70–75 | 73–88 | 70–86 |
| Flash-Next EXL3 5.05bpw, TabbyAPI + MTP (CMP + 5070 Ti + CPU experts) | 968 | 69–74 | 74–76 | 70–82 | 69–82 |
| Flash-Next IQ3_S GSQ-RCO, llama.cpp + MTP (current llama-server service) | 580 | 66–76 | 72–76 | 58–61 | 47–50 |
| GLM-5.3 REAP50 IQ3_M, llama.cpp | 372 | 27–28 | 25 | 22 | — |

## From Minecraft run logs (not in the speed test, only roughly comparable)

Measured on 2026-10-06 from llama-server's own timings during long agent runs; context grew through the run.

| Setup | Prefill, tok/s | Decode ~75–85k | Decode ~145–150k | Decode ~200k |
|---|---|---|---|---|
| 27B Unsloth UD-Q4_K_M, llama.cpp + MTP (CMP) | ~460–740 | 48 | 36 | 29–32 |
| 27B BF16 GGUF, llama.cpp + MTP (CMP 52.5 GB + 5070 Ti 15 GB) | ~600–1,360 | 40 | 33 | 26–29 |

## Takeaways

- AutoRound 3bpw + MTP 3 is fastest on both prefill and decode, and holds ~150 tok/s at every context length.
- Flash-Next builds stay roughly flat with context; dense 27B builds slow sharply (W4A16 + DFlash2: ~200 → ~30 tok/s
  by 160k). The TnzGit recipe holds up best of the 27B builds at long context (84–93 tok/s at 160k).
- vLLM leads on prefill; llama.cpp trails, especially for Flash-Next (IQ3_S ~580 tok/s vs AutoRound ~3,560).

## Quality (for reference; greedy, thinking off)

| Model | HumanEval+ | MATH |
|---|---|---|
| Flash-Next EXL3 5.05bpw | 157/164 | 97/100 |
| Flash-Next EXL3 3.05bpw | 156/164 | 95/100 |
| Flash-Next IQ3_S | 154/164 | 95/100 |
| 27B BF16 | 153/164 | 94/100 |
| 27B EXL3 4bpw (TensorFold) | 152/164 | 95/100 |
| 27B W8A16 | 152/164 | 94/100 |
| 27B W4A16 (TnzGit recipe) | 151/164 | 94/100 |
| Flash-Next AutoRound 3bpw | 149/164 | 94/100 |
| 27B W4A16 AutoRound | 148/164 | 94/100 |
