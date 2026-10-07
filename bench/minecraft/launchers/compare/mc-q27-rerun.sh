#!/bin/bash
# Rerun of the 27B GPTQ + DFlash2-15 Minecraft build: run 1 crashed at 12 min (Rameness KeyError on a truncated edit_file call, fixed).
# User request 2026-10-06 ~8:40 PM EDT: Minecraft reruns of the top 3 speed setups with the current Rameness defaults.
#  1) 27B pearsonkyle GPTQ W4A16 + DFlash2-15 (TnzGit vLLM)  2) Flash-Next AutoRound 3bpw MTP 3, 4096 chunks (PLE vLLM)
#  3) Flash-Next EXL3 5.05bpw tuned (TabbyAPI, config-505-c4096-q4-80). JEV = Kev (CPU bf16). Restores llama-server.
cd /root/bench/compare
S=status.txt; log() { echo "$* $(date +%T)" >> $S; }
while systemctl is-active -q mc-top3; do sleep 60; done   # after the top-3 queue
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
systemctl stop llama-server; sleep 10
start_kev || log "mc-top3: kev failed to start"

# 1) 27B GPTQ + DFlash2-15
export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=0 MODEL=/data/models/pearsonkyle-Qwen3.8-27B-GPTQ-W4A16 DRAFT=/root/tnz/models/Qwen3.8-27B-DFlash2-W4A16
export SPEC=dflash2 DFLASH_TOKENS=15 CTX=cmp-mixed-fp8 CMP_TARGET_ATTN_BACKEND=FLASHINFER CMP_TARGET_KV_DTYPE=fp8 DFLASH_ATTN_BACKEND=FLASH_ATTN DFLASH_KV_CACHE_DTYPE=bfloat16
export SPEC_ATTN=1 VLLM_SPEC_DECODE_ATTN_SEGMENTS=35 VLLM_FP8_SPEC_VERIFY=1 VLLM_FP8_SPEC_FULL_CG=0 VLLM_ALIGN_HETEROGENEOUS_ATTN_PAGES=1
export CUDAGRAPH_MODE=PIECEWISE MAX_LEN=262144 DFLASH_MAX_LEN=262144 MAX_SEQS=1 PORT=8000 VISION=1 VISION_OFFLOAD=1 PREFIX_CACHE=1 EXTRA_ARGS=""
(cd /root/tnz/repo && bash single-user/start_qwen.sh > /root/bench/compare/log-mc-q27gptq.txt 2>&1) &
SRV=$!
if wait_up http://127.0.0.1:8000 /root/bench/compare/log-mc-q27gptq.txt; then
  timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8000 qwen3.8-27b >> $S 2>&1
  mc q27gptq mc-q27gptq-df15-2
else log "mc-top3: q27 gptq server failed"; fi
pkill -f "vllm[ ]serve"; kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; sleep 20
unset MODEL DRAFT SPEC DFLASH_TOKENS CTX EXTRA_ARGS PORT MAX_LEN
systemctl start llama-server
swapoff /data/swapfile-bench 2>/dev/null && rm -f /data/swapfile-bench
log "mc-q27-rerun finished; llama-server $(systemctl is-active llama-server), swap removed"
