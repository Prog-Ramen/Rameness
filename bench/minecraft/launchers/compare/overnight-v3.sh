#!/bin/bash
# User go-ahead 2026-10-06 ("continue overnight with the queue"; then "rerun the bf16 minecraft test"). Replaces
# overnight-v2 (its run 1 died at 104 min: llama-server's 8 GB RAM prompt cache + Kev hit the OOM killer; the Clef
# readiness ping timed out after 10 s). Fixes: --cache-ram 1024 on every model server, 180 s warm-up ping.
#   1) mc-q27q4-v2-2      27B UD-Q4_K_M, new defaults, JEV = Kev (CPU bf16)
#   2) mc-q27q4-v2-clef-1 same, JEV = Clef-flash (5070 Ti, 8-bit; Kev stopped meanwhile)
#   3) mc-q27bf16-v2-1    27B BF16 GGUF on CMP + 5070 Ti, 262k, new defaults, JEV = Kev
# Restores llama-server + Kev at the end.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; M=/root/models/unsloth/q27
G=/data/models/q27-bf16/Qwen3.8-27B-BF16.gguf
log() { echo "$* $(date +%T)" >> $S; }
jev_up() { curl -s -m180 $1/v1/systemone -H "Content-Type: application/json" \
  -d '{"state":"ping","questions":{"up":{"type":"noul","instructions":"Is this a ping?"}}}' | grep -q answers; }
start_kev() {
  curl -s -m5 127.0.0.1:8008/v1/systemone >/dev/null 2>&1 && jev_up http://127.0.0.1:8008 && return 0
  systemctl stop kev 2>/dev/null; systemctl reset-failed kev 2>/dev/null
  systemd-run --unit=kev -p WorkingDirectory=/root/.rameness/jev/kev/src -E CUDA_VISIBLE_DEVICES= -E KEV_DTYPE=bf16 \
    /root/.rameness/jev/kev/src/.venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8008   # bf16: fp32 OOMs
  for i in $(seq 60); do sleep 10; jev_up http://127.0.0.1:8008 && return 0; done; return 1
}
COMMON=(--host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 --presence-penalty 0
        --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' -c 262144 -ctk q8_0 -ctv q8_0
        --cache-ram 1024)
serve() {  # unit, env..., -- llama-server args...
  local unit=$1; shift; local envs=(); while [ "$1" != "--" ]; do envs+=(-E "$1"); shift; done; shift
  systemctl stop $unit 2>/dev/null; systemctl reset-failed $unit 2>/dev/null
  systemd-run --unit=$unit -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B "${envs[@]}" \
    $B/llama-server "${COMMON[@]}" "$@"
  timeout 1800 bash -c "until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q $unit; do sleep 5; done"
  systemctl is-active -q $unit
}
mc() {  # model key, batch, server unit, config overlay
  log "mc $2 start"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:$1 --batch $2 --serial \
     ${4:+--rameness-config "$4"} > /root/bench/results/$2.log 2>&1)
  log "mc $2 done; server $3 $(systemctl is-active $3) ($(systemctl show $3 -p Result --value))"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.judge /root/bench/results/minecraft/$2 >> /root/bench/results/$2.log 2>&1)
  log "mc $2 judged: $(grep -o 'overall [0-9.]*' /root/bench/results/$2.log | tail -1)"
}
q27q4() { serve srv-q27q4 CUDA_VISIBLE_DEVICES=0 -- -m $M/Qwen3.8-27B-UD-Q4_K_M.gguf --mmproj $M/mmproj-BF16.gguf -ngl 99 \
  --spec-type draft-mtp -md $M/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99 --spec-draft-n-max 3; }

while systemctl is-active -q overnight-v2; do sleep 30; done
systemctl stop llama-server; sleep 10

# 1) Q4_K_M + Kev
start_kev || log "overnight-v3: kev failed to start"
if q27q4; then mc q27q4 mc-q27q4-v2-2 srv-q27q4 ""; else log "overnight-v3: q27q4 server failed"; fi

# 2) Q4_K_M + Clef (Kev and Clef together ran RAM out at 03:35)
systemctl stop kev
systemctl reset-failed srv-clef 2>/dev/null
systemd-run --unit=srv-clef -p WorkingDirectory=/data/clef -E CUDA_VISIBLE_DEVICES=1 -E CUDA_DEVICE_ORDER=PCI_BUS_ID \
  /data/clef/venv/bin/python /data/clef/serve.py --bits 8
timeout 900 bash -c 'until curl -sf -m3 127.0.0.1:8010/health >/dev/null || ! systemctl is-active -q srv-clef; do sleep 5; done'
if jev_up http://127.0.0.1:8010 && { systemctl is-active -q srv-q27q4 || q27q4; }; then
  mc q27q4 mc-q27q4-v2-clef-1 srv-q27q4 '{"jev": {"backend": "kev", "kev_url": "http://127.0.0.1:8010/v1/systemone", "kev_model": "clef-flash"}}'
else log "overnight-v3: clef run skipped (clef $(systemctl is-active srv-clef), q27q4 $(systemctl is-active srv-q27q4))"; fi
systemctl stop srv-clef srv-q27q4; sleep 10

# 3) BF16 on both GPUs + Kev
start_kev || log "overnight-v3: kev failed to start"
if [ ! -s $G ] || ! grep -q "CONVERT EXIT 0" /data/models/q27-bf16/convert.log 2>/dev/null; then
  log "bf16 convert start"; rm -f $G; systemctl reset-failed conv-bf16 2>/dev/null
  systemd-run --wait --unit=conv-bf16 -p MemoryMax=14G -p MemorySwapMax=0 -p WorkingDirectory=/root/models/llama.cpp/llama-mtp-fmad \
    /bin/bash -c "/root/models/drafts/venv/bin/python convert_hf_to_gguf.py /root/models27/Qwen3.8-27B --outtype bf16 --outfile $G > /data/models/q27-bf16/convert.log 2>&1; echo \"CONVERT EXIT \$?\" >> /data/models/q27-bf16/convert.log"
  log "bf16 convert: $(tail -1 /data/models/q27-bf16/convert.log) ($(du -h $G 2>/dev/null | cut -f1))"
fi
if grep -q "CONVERT EXIT 0" /data/models/q27-bf16/convert.log; then
  BF=(-m $G --mmproj $M/mmproj-BF16.gguf -ngl 99 -sm layer -ts 52,12 -mg 0)
  if serve srv-bf16 CUDA_DEVICE_ORDER=PCI_BUS_ID -- "${BF[@]}" --spec-type draft-mtp --spec-draft-n-max 3; then log "bf16: built-in MTP head"
  elif serve srv-bf16 CUDA_DEVICE_ORDER=PCI_BUS_ID -- "${BF[@]}" --spec-type draft-mtp -md $M/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99 --spec-draft-n-max 3; then
    log "bf16: Unsloth MTP head (built-in did not load)"
  else log "bf16 server failed: $(journalctl -u srv-bf16 --since '-40min' --no-pager -o cat | grep -E ' E |error|out of memory' | head -2 | tr '\n' ' ' | cut -c1-250)"; fi
  if systemctl is-active -q srv-bf16; then
    log "bf16 vram: $(nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader | tr '\n' ' ')"
    timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1
    mc q27bf16gg mc-q27bf16-v2-1 srv-bf16 ""
  fi
fi
systemctl stop srv-bf16 2>/dev/null; sleep 10
start_kev; systemctl start llama-server
log "overnight-v3 finished; llama-server $(systemctl is-active llama-server), kev $(systemctl is-active kev)"
