#!/bin/bash
# User goal 2026-10-07 ~4 AM EDT: overnight, resume the 3 paused Minecraft tests; if any fail, fix and rerun.
#  1) Flash-Next AutoRound 3bpw, MTP 3, 4096 chunks (iIIusi0n vLLM; thinking budget via thinking_token_budget)
#  2) 27B pearsonkyle GPTQ W4A16 + DFlash2-15, lookup ON with a FIXED verify length (TnzGit vLLM)
#  3) Flash-Next EXL3 5.05bpw tuned (TabbyAPI, config-505-c4096-q4-80)
# Robustness: wait for an empty GPU and >= 18 GB available RAM between servers; fail fast on engine startup errors;
# retry a server that does not start, and a build that ends in < 30 min, once. llama-server stays off (user).
cd /root/bench/compare
S=status.txt; log() { echo "$* $(date +%T)" >> $S; }
if ! swapon --show | grep -q swapfile-bench; then
  [ -f /data/swapfile-bench ] || { fallocate -l 48G /data/swapfile-bench && chmod 600 /data/swapfile-bench && mkswap -q /data/swapfile-bench; }
  swapon --priority 10 /data/swapfile-bench
fi
ping_kev() { curl -s -m180 127.0.0.1:8008/v1/systemone -H "Content-Type: application/json" -d '{"model":"kev","state":"ping","questions":{"up":{"type":"noul","instructions":"Is this a ping?","criteria":{"true":"It is a ping.","false":"It is not."}}}}' | grep -q answers; }
start_kev() { ping_kev && return 0; systemctl stop kev 2>/dev/null; systemctl reset-failed kev 2>/dev/null
  systemd-run --unit=kev -p WorkingDirectory=/root/.rameness/jev/kev/src -E CUDA_VISIBLE_DEVICES= -E KEV_DTYPE=bf16 \
    /root/.rameness/jev/kev/src/.venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8008
  for i in $(seq 60); do sleep 10; ping_kev && return 0; done; return 1; }
settle() {  # empty GPU, page cache dropped, and enough RAM actually available
  for i in $(seq 60); do [ -z "$(nvidia-smi --query-compute-apps=pid --format=csv,noheader)" ] && break; sleep 5; done
  for p in $(nvidia-smi --query-compute-apps=pid --format=csv,noheader); do kill $p 2>/dev/null; done
  sync; echo 1 > /proc/sys/vm/drop_caches
  for i in $(seq 60); do [ "$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)" -ge 18 ] && break; sleep 5; done
  log "settle: RAM available $(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo) GB, GPU apps: $(nvidia-smi --query-compute-apps=pid --format=csv,noheader | wc -l)"
}
up_vllm() {  # log file -> 0 when /health answers, 1 if the engine died
  timeout 2400 bash -c "until curl -sf -m3 127.0.0.1:8000/health >/dev/null; do grep -q 'Engine core initialization failed' $1 && exit 1; sleep 5; done"
}
mc() {  # model key, batch -> 0 if the build ran >= 30 min (a real run), 1 if it ended early
  log "mc $2 start"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:$1 --batch $2 --serial > /root/bench/results/$2.log 2>&1)
  local wall=$(python3 -c "import json,glob; f=glob.glob('/root/bench/results/minecraft/$2/*/result.json'); print(int(json.load(open(f[0]))['wall_seconds']) if f else 0)")
  log "mc $2 done (wall ${wall}s)"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.judge /root/bench/results/minecraft/$2 >> /root/bench/results/$2.log 2>&1)
  log "mc $2 judged: $(grep -o 'overall [0-9.]*' /root/bench/results/$2.log | tail -1)"
  [ "$wall" -ge 1800 ]
}
# ---- server starters (each returns 0 when serving) ----
start_autoround() {
  export VLLM_WORKDIR=/root/vllm CUDA_HOME=/usr/local/cuda QWEN_HOST=127.0.0.1 QWEN_PORT=8000 QWEN_MODEL_DIR=/data/models/Qwen3.8-Flash-Next-AutoRound-3bpw-MTP
  QWEN_MTP=3 QWEN_BATCH_TOKENS=4096 QWEN_SEQS=1 \
  QWEN_CAPTURE_SIZES='[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,18,20,22,24,26,28,30,32,64,128,256,512,1024,2048,4096]' \
    /root/vllm/cmp170hx/scripts/serve.sh > log-ovn-autoround.txt 2>&1 &
  up_vllm log-ovn-autoround.txt
}
start_q27() {
  ( export CUDA_DEVICE_ORDER=PCI_BUS_ID CUDA_VISIBLE_DEVICES=0 MODEL=/data/models/pearsonkyle-Qwen3.8-27B-GPTQ-W4A16 SPEC=dflash2 DFLASH_TOKENS=15
    export DRAFT=/root/tnz/models/Qwen3.8-27B-DFlash2-W4A16 CTX=cmp-mixed-fp8 CMP_TARGET_ATTN_BACKEND=FLASHINFER CMP_TARGET_KV_DTYPE=fp8
    export DFLASH_ATTN_BACKEND=FLASH_ATTN DFLASH_KV_CACHE_DTYPE=bfloat16 SPEC_ATTN=1 VLLM_SPEC_DECODE_ATTN_SEGMENTS=35 VLLM_FP8_SPEC_VERIFY=1
    export VLLM_FP8_SPEC_FULL_CG=0 VLLM_ALIGN_HETEROGENEOUS_ATTN_PAGES=1 CUDAGRAPH_MODE=PIECEWISE MAX_LEN=262144 DFLASH_MAX_LEN=262144
    export MAX_SEQS=1 PORT=8000 VISION=1 VISION_OFFLOAD=1 PREFIX_CACHE=1 EXTRA_ARGS="" KV_MEM=17179869184
    export LOOKUP=1 LOOKUP_ADAPTIVE=0 VLLM_DFLASH2_LOOKUP_ADAPTIVE=0
    cd /root/tnz/repo && exec bash single-user/start_qwen.sh ) > log-ovn-q27.txt 2>&1 &
  up_vllm log-ovn-q27.txt
}
start_exl3() {
  systemctl reset-failed tabby-505t 2>/dev/null
  systemd-run --unit=tabby-505t -p WorkingDirectory=/root/exl3/tabby -p LimitMEMLOCK=infinity \
    /root/exl3/tabby/venv/bin/python main.py --config /root/exl3/tabby/config-505-c4096-q4-80.yml
  timeout 2400 bash -c 'until curl -sf -m3 127.0.0.1:5000/health >/dev/null || ! systemctl is-active -q tabby-505t; do sleep 5; done'
  systemctl is-active -q tabby-505t
}
stop_servers() { pkill -f "vllm[ ]serve"; pkill -f "single-user/start_qwe[n]"; systemctl stop tabby-505t 2>/dev/null; sleep 10; settle; }
# ---- one model: start (retry once), smoke, build (retry once if it ends early) ----
run_model() {  # name starter url model_id key batch smoke_budget_field
  local name=$1 starter=$2 url=$3 mid=$4 key=$5 batch=$6 bf=$7
  for attempt in 1 2; do
    if $starter; then
      log "$name: server up (attempt $attempt)"
      timeout 600 python3 /root/exl3/smoke_any.py $url $mid $bf >> $S 2>&1
      if mc $key ${batch}; then stop_servers; return 0; fi
      log "$name: build ended early (attempt $attempt)"; stop_servers
      batch=${batch%-*}-r$attempt
    else
      log "$name: server failed to start (attempt $attempt): $(grep -hE 'Error|error' log-ovn-*.txt 2>/dev/null | grep -v 'Engine core' | tail -1 | cut -c1-200)"
      stop_servers
    fi
  done
  log "$name: gave up after 2 attempts"; return 1
}
systemctl stop llama-server 2>/dev/null
settle
start_kev || log "overnight3: kev failed to start"
run_model autoround start_autoround http://127.0.0.1:8000 Qwen3.8-Flash-Next flashar mc-flashar-mtp3-3 thinking_token_budget
run_model q27gptq   start_q27       http://127.0.0.1:8000 qwen3.8-27b       q27gptq mc-q27gptq-df15-3 thinking_token_budget
run_model exl3-505t start_exl3      http://127.0.0.1:5000 Qwen3.8-Flash-Next-exl3-5.05bpw exl3-505 mc-exl3-505t-3 reasoning_budget_tokens
swapoff /data/swapfile-bench 2>/dev/null && rm -f /data/swapfile-bench
log "mc-overnight3 finished; swap removed; llama-server left off"
