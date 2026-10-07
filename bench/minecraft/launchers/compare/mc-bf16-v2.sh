#!/bin/bash
# User request 2026-10-06: after the overnight Clef run, rerun Qwen3.8-27B BF16 on Minecraft now that the 5070 Ti adds
# 16 GB: llama.cpp master (same runtime and flags as the UD-Q4_K_M runs) across CMP 170HX + RTX 5070 Ti, full 262k context
# (the Oct 4 BF16 run was vLLM capped at 80k), built-in MTP head, new Rameness defaults. Restores llama-server + Kev.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; G=/data/models/q27-bf16/Qwen3.8-27B-BF16.gguf
log() { echo "$* $(date +%T)" >> $S; }
kev_up() { curl -s -m10 127.0.0.1:8008/v1/systemone -H "Content-Type: application/json" \
  -d '{"state":"ping","questions":{"up":{"type":"noul","instructions":"Is this a ping?"}}}' | grep -q answers; }
start_kev() {
  kev_up && return 0
  systemctl stop kev 2>/dev/null; systemctl reset-failed kev 2>/dev/null
  systemd-run --unit=kev -p WorkingDirectory=/root/.rameness/jev/kev/src -E CUDA_VISIBLE_DEVICES= -E KEV_DTYPE=bf16 \
    /root/.rameness/jev/kev/src/.venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8008   # bf16: fp32 OOMs
  for i in $(seq 120); do kev_up && return 0; sleep 5; done; return 1
}
restore() { systemctl stop srv-bf16 2>/dev/null; sleep 10; start_kev; systemctl start llama-server
  log "mc-bf16-v2 finished; llama-server $(systemctl is-active llama-server), kev $(systemctl is-active kev)"; }

while systemctl is-active -q overnight-v2; do sleep 60; done
systemctl stop llama-server; sleep 10
start_kev || { log "mc-bf16-v2: kev failed"; restore; exit 1; }

if [ ! -s $G ] || ! grep -q "CONVERT EXIT 0" /data/models/q27-bf16/convert.log; then
  log "bf16 convert start"
  systemctl reset-failed conv-bf16 2>/dev/null
  systemd-run --wait --unit=conv-bf16 -p MemoryMax=14G -p MemorySwapMax=0 -p WorkingDirectory=/root/models/llama.cpp/llama-mtp-fmad \
    /bin/bash -c "/root/models/drafts/venv/bin/python convert_hf_to_gguf.py /root/models27/Qwen3.8-27B --outtype bf16 --outfile $G > /data/models/q27-bf16/convert.log 2>&1; echo \"CONVERT EXIT \$?\" >> /data/models/q27-bf16/convert.log"
  grep -q "CONVERT EXIT 0" /data/models/q27-bf16/convert.log || { log "bf16 convert failed: $(tail -c 200 /data/models/q27-bf16/convert.log | tr '\n' ' ')"; restore; exit 1; }
  log "bf16 convert done ($(du -h $G | cut -f1))"
fi

serve() {  # extra speculative args...
  systemctl stop srv-bf16 2>/dev/null; systemctl reset-failed srv-bf16 2>/dev/null
  systemd-run --unit=srv-bf16 -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B -E CUDA_DEVICE_ORDER=PCI_BUS_ID \
    $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 \
    --presence-penalty 0 --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' \
    -m $G --mmproj /root/models/unsloth/q27/mmproj-BF16.gguf -ngl 99 -sm layer -ts 52,12 -mg 0 \
    -c 262144 -ctk q8_0 -ctv q8_0 --spec-type draft-mtp --spec-draft-n-max 3 "$@"
  timeout 1800 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-bf16; do sleep 5; done'
  systemctl is-active -q srv-bf16
}
if serve; then log "bf16: built-in MTP head"
elif serve -md /root/models/unsloth/q27/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99; then log "bf16: Unsloth MTP head (built-in did not load)"
else log "bf16 server failed: $(journalctl -u srv-bf16 --since '-40min' --no-pager -o cat | grep -E ' E |error|out of memory' | head -2 | tr '\n' ' ' | cut -c1-250)"; restore; exit 1; fi
nvidia-smi --query-gpu=index,memory.used,memory.total --format=csv,noheader | tr '\n' ' ' >> $S; echo >> $S
timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1

log "mc mc-q27bf16-v2-1 start"
(cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:q27bf16gg --batch mc-q27bf16-v2-1 --serial \
   > /root/bench/results/mc-q27bf16-v2-1.log 2>&1)
log "mc mc-q27bf16-v2-1 done"
restore
(cd /root/projects/Rameness && python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-q27bf16-v2-1 >> /root/bench/results/mc-q27bf16-v2-1.log 2>&1)
log "mc mc-q27bf16-v2-1 judged: $(grep -o 'overall [0-9.]*' /root/bench/results/mc-q27bf16-v2-1.log | tail -1)"
