#!/bin/bash
# User request 2026-10-06: the Opus-style bundle on the Minecraft build instead of the short-task A/Bs. Same server and
# settings as mc-q27-ud-q4km-vision-1 (100%, visual 7.25, 107 min); srv-q27q4 is already up. Restores llama-server.
S=/root/bench/compare/status.txt
log() { echo "$* $(date +%T)" >> $S; }
CFG='{"draft_then_revise": true, "review_pass": true, "batch_workflow": true, "progress_review": {"every": 40}, "todo_reminder_turns": 30}'
cd /root/projects/Rameness
log "mc bundle start"
python3 -m bench.minecraft.run --runs rameness:q27q4 --batch mc-q27q4-bundle-1 --serial --rameness-config "$CFG" \
  > /root/bench/results/mc-q27q4-bundle-1.log 2>&1
log "mc bundle done"
systemctl stop srv-q27q4; sleep 10; systemctl start llama-server
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-q27q4-bundle-1 >> /root/bench/results/mc-q27q4-bundle-1.log 2>&1
log "mc bundle judged; service $(systemctl is-active llama-server)"
