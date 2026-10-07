#!/bin/bash
# 2026-10-07: 27B on ninfer (DFlash2 7 + lm-head-draft, cuBLAS prefill, thinking budget 5000) vs the overnight
# 27B GPTQ vLLM build (mc-q27gptq-df15-3). Server: ninfer-mc.service on :8090. Kev on :8008.
cd /root/bench/compare; S=status.txt; log() { echo "$* $(date +%T)" >> $S; }
B=mc-ninfer27-df7-1
log "mc $B start"
(cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:ninfer27 --batch $B --serial > /root/bench/results/$B.log 2>&1)
log "mc $B done"
(cd /root/projects/Rameness && python3 -m bench.minecraft.judge /root/bench/results/minecraft/$B >> /root/bench/results/$B.log 2>&1)
log "mc $B judged: $(grep -o 'overall [0-9.]*' /root/bench/results/$B.log | tail -1)"
systemctl stop ninfer-mc
log "ninfer-mc stopped"
