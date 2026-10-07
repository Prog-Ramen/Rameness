#!/bin/bash
# User go-ahead 2026-10-06: one Minecraft run of the latest Rameness defaults (feature cycles on git branches, group
# review, group debugging with Occam's razor, thinking floor after write_file, progress review every 10, full tool log)
# on 27B UD-Q4_K_M + MTP (CMP only), JEV = Kev (CPU bf16). Temporary swap for the llama.cpp MTP host-RAM leak.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; M=/root/models/unsloth/q27
log() { echo "$* $(date +%T)" >> $S; }
ping_kev() { curl -s -m180 127.0.0.1:8008/v1/systemone -H "Content-Type: application/json" -d '{"model":"kev","state":"ping","questions":{"up":{"type":"noul","instructions":"Is this a ping?","criteria":{"true":"It is a ping.","false":"It is not."}}}}' | grep -q answers; }
start_kev() {
  ping_kev && return 0
  systemctl stop kev 2>/dev/null; systemctl reset-failed kev 2>/dev/null
  systemd-run --unit=kev -p WorkingDirectory=/root/.rameness/jev/kev/src -E CUDA_VISIBLE_DEVICES= -E KEV_DTYPE=bf16 \
    /root/.rameness/jev/kev/src/.venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8008
  for i in $(seq 60); do sleep 10; ping_kev && return 0; done; return 1
}
restore() { systemctl stop srv-q27q4 2>/dev/null; sleep 10; start_kev; systemctl start llama-server
  swapoff /data/swapfile-bench 2>/dev/null && rm -f /data/swapfile-bench
  log "mc-v3 finished; llama-server $(systemctl is-active llama-server), kev $(systemctl is-active kev), swap removed"; }
if ! swapon --show | grep -q swapfile-bench; then
  [ -f /data/swapfile-bench ] || { fallocate -l 48G /data/swapfile-bench && chmod 600 /data/swapfile-bench && mkswap -q /data/swapfile-bench; }
  swapon --priority 10 /data/swapfile-bench
fi
systemctl stop llama-server; sleep 10
start_kev || { log "mc-v3: kev failed"; restore; exit 1; }
systemctl reset-failed srv-q27q4 2>/dev/null
systemd-run --unit=srv-q27q4 -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B -E CUDA_VISIBLE_DEVICES=0 \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 --presence-penalty 0 \
  --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' -c 262144 -ctk q8_0 -ctv q8_0 --cache-ram 1024 \
  -m $M/Qwen3.8-27B-UD-Q4_K_M.gguf --mmproj $M/mmproj-BF16.gguf -ngl 99 \
  --spec-type draft-mtp -md $M/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99 --spec-draft-n-max 3
timeout 1500 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-q27q4; do sleep 5; done'
systemctl is-active -q srv-q27q4 || { log "mc-v3: q27q4 server failed"; restore; exit 1; }
timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1
log "mc mc-q27q4-v3-1 start"
(cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:q27q4 --batch mc-q27q4-v3-1 --serial \
   > /root/bench/results/mc-q27q4-v3-1.log 2>&1)
log "mc mc-q27q4-v3-1 done; server $(systemctl is-active srv-q27q4) ($(systemctl show srv-q27q4 -p Result --value))"
restore
(cd /root/projects/Rameness && python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-q27q4-v3-1 >> /root/bench/results/mc-q27q4-v3-1.log 2>&1)
log "mc mc-q27q4-v3-1 judged: $(grep -o 'overall [0-9.]*' /root/bench/results/mc-q27q4-v3-1.log | tail -1)"
