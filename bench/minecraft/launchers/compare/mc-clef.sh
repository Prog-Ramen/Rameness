#!/bin/bash
# User request 2026-10-06: after mc-bundle, the same Minecraft run (27B Q4_K_M, new default workflow) with Clef-flash
# (srv-clef :8010, RTX 5070 Ti, 8-bit) as JEV instead of Kev, to compare the decision model. Restores llama-server.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; M=/root/models/unsloth/q27
log() { echo "$* $(date +%T)" >> $S; }
while systemctl is-active -q mc-bundle; do sleep 60; done
systemctl stop llama-server; sleep 10
systemctl reset-failed srv-clef 2>/dev/null   # Clef only for this run (it was stopped at 05:34)
systemd-run --unit=srv-clef -p WorkingDirectory=/data/clef -E CUDA_VISIBLE_DEVICES=1 -E CUDA_DEVICE_ORDER=PCI_BUS_ID \
  /data/clef/venv/bin/python /data/clef/serve.py --bits 8
timeout 900 bash -c 'until curl -sf -m3 127.0.0.1:8010/health >/dev/null || ! systemctl is-active -q srv-clef; do sleep 5; done'
systemctl reset-failed srv-q27q4 2>/dev/null
systemd-run --unit=srv-q27q4 -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B -E CUDA_VISIBLE_DEVICES=0 \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 \
  --presence-penalty 0 --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' \
  -m $M/Qwen3.8-27B-UD-Q4_K_M.gguf --mmproj $M/mmproj-BF16.gguf -ngl 99 -c 262144 -ctk q8_0 -ctv q8_0 \
  --spec-type draft-mtp -md $M/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99 --spec-draft-n-max 3
if ! timeout 1500 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-q27q4; do sleep 5; done' \
   || ! systemctl is-active -q srv-q27q4 || ! curl -sf -m10 127.0.0.1:8010/health >/dev/null; then
  log "mc clef: a server failed (q27q4 $(systemctl is-active srv-q27q4), clef $(systemctl is-active srv-clef))"
  systemctl stop srv-q27q4 srv-clef; systemctl start llama-server; exit 1; fi
CFG='{"jev": {"backend": "kev", "kev_url": "http://127.0.0.1:8010/v1/systemone", "kev_model": "clef-flash"}, "draft_then_revise": true, "review_pass": true, "batch_workflow": true, "progress_review": {"every": 40}, "todo_reminder_turns": 30}'
cd /root/projects/Rameness
log "mc clef start"
python3 -m bench.minecraft.run --runs rameness:q27q4 --batch mc-q27q4-clef-1 --serial --rameness-config "$CFG" \
  > /root/bench/results/mc-q27q4-clef-1.log 2>&1
log "mc clef done"
systemctl stop srv-q27q4 srv-clef; sleep 10; systemctl start llama-server
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-q27q4-clef-1 >> /root/bench/results/mc-q27q4-clef-1.log 2>&1
log "mc clef judged; service $(systemctl is-active llama-server)"
