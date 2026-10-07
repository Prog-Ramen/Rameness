#!/bin/bash
# User request 2026-10-07 ~1:30 AM EDT: rerun the Flash-Next AutoRound Minecraft build with the corrected Rameness
# prompts (running draft first, then review-and-fix rounds) and the thinking budget actually reaching vLLM
# (smoke-tested with vLLM's own field; proxy now renames Rameness's per-turn budget). llama-server stays off.
cd /root/bench/compare
S=status.txt; log() { echo "$* $(date +%T)" >> $S; }
if ! swapon --show | grep -q swapfile-bench; then   # safety net for RAM spikes (Kev + CPU experts)
  [ -f /data/swapfile-bench ] || { fallocate -l 48G /data/swapfile-bench && chmod 600 /data/swapfile-bench && mkswap -q /data/swapfile-bench; }
  swapon --priority 10 /data/swapfile-bench
fi
ping_kev() { curl -s -m180 127.0.0.1:8008/v1/systemone -H "Content-Type: application/json" -d '{"model":"kev","state":"ping","questions":{"up":{"type":"noul","instructions":"Is this a ping?","criteria":{"true":"It is a ping.","false":"It is not."}}}}' | grep -q answers; }
start_kev() { ping_kev && return 0; systemctl stop kev 2>/dev/null; systemctl reset-failed kev 2>/dev/null
  systemd-run --unit=kev -p WorkingDirectory=/root/.rameness/jev/kev/src -E CUDA_VISIBLE_DEVICES= -E KEV_DTYPE=bf16 \
    /root/.rameness/jev/kev/src/.venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8008
  for i in $(seq 60); do sleep 10; ping_kev && return 0; done; return 1; }
wait_up() {  # url, log file, unit-or-pid check
  timeout 2400 bash -c "until curl -sf -m3 $1/health >/dev/null; do grep -qE 'Engine core initialization failed|Traceback' $2 2>/dev/null && ! curl -sf -m3 $1/health >/dev/null && sleep 20 && ! curl -sf -m3 $1/health >/dev/null && exit 1; sleep 5; done"
}
mc() {  # model key, batch
  log "mc $2 start"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:$1 --batch $2 --serial > /root/bench/results/$2.log 2>&1)
  log "mc $2 done"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.judge /root/bench/results/minecraft/$2 >> /root/bench/results/$2.log 2>&1)
  log "mc $2 judged: $(grep -o 'overall [0-9.]*' /root/bench/results/$2.log | tail -1)"
}
systemctl stop llama-server 2>/dev/null
start_kev || log "mc-flashar-2: kev failed to start"
export VLLM_WORKDIR=/root/vllm CUDA_HOME=/usr/local/cuda QWEN_HOST=127.0.0.1 QWEN_PORT=8000 QWEN_MODEL_DIR=/data/models/Qwen3.8-Flash-Next-AutoRound-3bpw-MTP
QWEN_MTP=3 QWEN_BATCH_TOKENS=4096 QWEN_SEQS=1 \
QWEN_CAPTURE_SIZES='[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,18,20,22,24,26,28,30,32,64,128,256,512,1024,2048,4096]' \
  /root/vllm/cmp170hx/scripts/serve.sh > /root/bench/compare/log-mc-flashar-2.txt 2>&1 &
SRV=$!
if wait_up http://127.0.0.1:8000 /root/bench/compare/log-mc-flashar-2.txt; then
  SMOKE=$(timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8000 Qwen3.8-Flash-Next thinking_token_budget 2>&1); echo "$SMOKE" >> $S
  RC=$(echo "$SMOKE" | grep -oE "budget 200: reasoning chars [0-9]+" | grep -oE "[0-9]+$")
  if [ -n "$RC" ] && [ "$RC" -lt 3000 ]; then KEY=flashar; else KEY=flashar-cap; fi
  log "autoround thinking budget (thinking_token_budget): reasoning chars ${RC:-none} -> bench key $KEY"
  mc $KEY mc-flashar-mtp3-2
else log "mc-flashar-2: autoround server failed"; fi
pkill -f "vllm[ ]serve"; kill $SRV 2>/dev/null; wait $SRV 2>/dev/null
for i in $(seq 60); do [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)" ] && break; sleep 5; done
swapoff /data/swapfile-bench 2>/dev/null && rm -f /data/swapfile-bench
log "mc-flashar-2 finished; swap removed"
