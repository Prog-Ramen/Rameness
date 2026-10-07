#!/bin/bash
# Redo 2026-10-07 ~12:05 AM EDT of the two top-3 Minecraft runs that never started: 27B GPTQ + DFlash2-15 (now with a
# fixed 16 GB KV cache: auto-sizing left too little VRAM for the DFlash-15 verify buffers) and EXL3 5.05bpw tuned (host
# RAM was not yet released by the previous server). Between servers: wait for an empty GPU and drop the page cache.
cd /root/bench/compare
S=status.txt; log() { echo "$* $(date +%T)" >> $S; }
gpu_free() { for i in $(seq 60); do [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)" ] && return 0; sleep 5; done
  for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader); do kill $p 2>/dev/null; done; sleep 15; }
settle() { gpu_free; sync; echo 1 > /proc/sys/vm/drop_caches; sleep 5; }
if ! swapon --show | grep -q swapfile-bench; then   # safety net for RAM spikes (Kev + CPU experts)
  [ -f /data/swapfile-bench ] || { fallocate -l 48G /data/swapfile-bench && chmod 600 /data/swapfile-bench && mkswap -q /data/swapfile-bench; }
  swapon --priority 10 /data/swapfile-bench
fi
ping_kev() { curl -s -m180 127.0.0.1:8008/v1/systemone -H "Content-Type: application/json" -d '{"model":"kev","state":"ping","questions":{"up":{"type":"noul","instructions":"Is this a ping?","criteria":{"true":"It is a ping.","false":"It is not."}}}}' | grep -q answers; }
start_kev() { ping_kev && return 0; systemctl stop kev 2>/dev/null; systemctl reset-failed kev 2>/dev/null
  systemd-run --unit=kev -p WorkingDirectory=/root/.rameness/jev/kev/src -E CUDA_VISIBLE_DEVICES= -E KEV_DTYPE=bf16 \
    /root/.rameness/jev/kev/src/.venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8008
  for i in $(seq 60); do sleep 10; ping_kev && return 0; done; return 1; }
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
if ! swapon --show | grep -q swapfile-bench; then   # safety net for RAM spikes (Kev + CPU experts)
  [ -f /data/swapfile-bench ] || { fallocate -l 48G /data/swapfile-bench && chmod 600 /data/swapfile-bench && mkswap -q /data/swapfile-bench; }
  swapon --priority 10 /data/swapfile-bench
fi
systemctl stop llama-server; settle
start_kev || log "mc-redo: kev failed to start"
# 1) 27B GPTQ + DFlash2-15
export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=0 MODEL=/data/models/pearsonkyle-Qwen3.8-27B-GPTQ-W4A16 DRAFT=/root/tnz/models/Qwen3.8-27B-DFlash2-W4A16
export SPEC=dflash2 DFLASH_TOKENS=15 CTX=cmp-mixed-fp8 CMP_TARGET_ATTN_BACKEND=FLASHINFER CMP_TARGET_KV_DTYPE=fp8 DFLASH_ATTN_BACKEND=FLASH_ATTN DFLASH_KV_CACHE_DTYPE=bfloat16
export SPEC_ATTN=1 VLLM_SPEC_DECODE_ATTN_SEGMENTS=35 VLLM_FP8_SPEC_VERIFY=1 VLLM_FP8_SPEC_FULL_CG=0 VLLM_ALIGN_HETEROGENEOUS_ATTN_PAGES=1
export CUDAGRAPH_MODE=PIECEWISE MAX_LEN=262144 DFLASH_MAX_LEN=262144 MAX_SEQS=1 PORT=8000 VISION=1 VISION_OFFLOAD=1 PREFIX_CACHE=1 EXTRA_ARGS="" KV_MEM=17179869184   # 16 GiB, in bytes
(cd /root/tnz/repo && bash single-user/start_qwen.sh > /root/bench/compare/log-mc-q27gptq.txt 2>&1) &
SRV=$!
if wait_up http://127.0.0.1:8000 /root/bench/compare/log-mc-q27gptq.txt; then
  timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8000 qwen3.8-27b >> $S 2>&1
  mc q27gptq mc-q27gptq-df15-2
else log "mc-top3: q27 gptq server failed"; fi
pkill -f "vllm[ ]serve"; kill $SRV 2>/dev/null; wait $SRV 2>/dev/null; settle
unset MODEL DRAFT SPEC DFLASH_TOKENS CTX EXTRA_ARGS PORT MAX_LEN
# 3) EXL3 5.05bpw tuned
systemctl reset-failed tabby-505t 2>/dev/null
systemd-run --unit=tabby-505t -p WorkingDirectory=/root/exl3/tabby -p LimitMEMLOCK=infinity \
  /root/exl3/tabby/venv/bin/python main.py --config /root/exl3/tabby/config-505-c4096-q4-80.yml
if timeout 2400 bash -c 'until curl -sf -m3 127.0.0.1:5000/health >/dev/null || ! systemctl is-active -q tabby-505t; do sleep 5; done' \
   && systemctl is-active -q tabby-505t; then
  timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:5000 Qwen3.8-Flash-Next-exl3-5.05bpw >> $S 2>&1
  mc exl3-505 mc-exl3-505t-1
else log "mc-top3: tabby 5.05 failed: $(journalctl -u tabby-505t --since '-40min' --no-pager -o cat | grep -iE 'error' | tail -1 | cut -c1-200)"; fi
systemctl stop tabby-505t; sleep 15
settle
systemctl start llama-server
swapoff /data/swapfile-bench 2>/dev/null && rm -f /data/swapfile-bench
log "mc-redo finished; llama-server $(systemctl is-active llama-server), kev $(systemctl is-active kev), swap removed"
