#!/bin/bash
# BF16 retry after mc-clef-v3: overnight-v3's fixed split (-ts 52,12) ran the 5070 Ti out of memory, so llama.cpp's
# --fit picks the split (context fixed at 262144 by -c, so fit cannot shrink it). Aborts if layers landed on the CPU.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; M=/root/models/unsloth/q27
G=/data/models/q27-bf16/Qwen3.8-27B-BF16.gguf
log() { echo "$* $(date +%T)" >> $S; }
ping_kev() { curl -s -m180 127.0.0.1:8008/v1/systemone -H "Content-Type: application/json" -d '{"model":"kev","state":"ping","questions":{"up":{"type":"noul","instructions":"Is this a ping?","criteria":{"true":"It is a ping.","false":"It is not."}}}}' | grep -q answers; }
start_kev() {
  ping_kev && return 0
  systemctl stop kev 2>/dev/null; systemctl reset-failed kev 2>/dev/null
  systemd-run --unit=kev -p WorkingDirectory=/root/.rameness/jev/kev/src -E CUDA_VISIBLE_DEVICES= -E KEV_DTYPE=bf16 \
    /root/.rameness/jev/kev/src/.venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8008
  for i in $(seq 60); do sleep 10; ping_kev && return 0; done; return 1
}
restore() { systemctl stop srv-bf16 2>/dev/null; sleep 10; start_kev; systemctl start llama-server
  log "mc-bf16-v3 finished; llama-server $(systemctl is-active llama-server), kev $(systemctl is-active kev)"; }
while systemctl is-active -q mc-clef-v3; do sleep 60; done
swapon --show | grep -q swapfile-bench || swapon --priority 10 /data/swapfile-bench   # llama.cpp MTP host-RAM leak
systemctl stop llama-server; sleep 10
start_kev || log "mc-bf16-v3: kev failed to start"
serve() {
  systemctl stop srv-bf16 2>/dev/null; systemctl reset-failed srv-bf16 2>/dev/null
  systemd-run --unit=srv-bf16 -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B -E CUDA_DEVICE_ORDER=PCI_BUS_ID \
    $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 --presence-penalty 0 \
    --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' -c 262144 -ctk q8_0 -ctv q8_0 --cache-ram 1024 \
    -m $G --mmproj $M/mmproj-BF16.gguf --fit on --fit-target 1536 --spec-type draft-mtp --spec-draft-n-max 3 "$@"
  timeout 1800 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-bf16; do sleep 5; done'
  systemctl is-active -q srv-bf16
}
if serve; then log "bf16 (fit): built-in MTP head"
elif serve -md $M/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99; then log "bf16 (fit): Unsloth MTP head"
else log "bf16 (fit) server failed: $(journalctl -u srv-bf16 --since '-40min' --no-pager -o cat | grep -E ' E |error|out of memory' | head -2 | tr '\n' ' ' | cut -c1-250)"; restore; exit 1; fi
VRAM=$(nvidia-smi --query-gpu=memory.used --format=csv,noheader,nounits | paste -sd+ | bc)
P=$(systemctl show -p MainPID --value srv-bf16); ANON=$(awk '/RssAnon/{print int($2/1024)}' /proc/$P/status)
log "bf16 placement: $(nvidia-smi --query-gpu=index,memory.used --format=csv,noheader | tr '\n' ' ') total ${VRAM} MiB; server RAM ${ANON} MiB"
if [ "$VRAM" -lt 54000 ]; then log "bf16: layers on the CPU (VRAM ${VRAM} MiB) - not running"; restore; exit 1; fi
timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1
log "mc mc-q27bf16-v2-1 start"
(cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:q27bf16gg --batch mc-q27bf16-v2-1 --serial \
   > /root/bench/results/mc-q27bf16-v2-1.log 2>&1)
log "mc mc-q27bf16-v2-1 done; server $(systemctl is-active srv-bf16) ($(systemctl show srv-bf16 -p Result --value))"
restore
(cd /root/projects/Rameness && python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-q27bf16-v2-1 >> /root/bench/results/mc-q27bf16-v2-1.log 2>&1)
log "mc mc-q27bf16-v2-1 judged: $(grep -o 'overall [0-9.]*' /root/bench/results/mc-q27bf16-v2-1.log | tail -1)"
swapoff /data/swapfile-bench && rm -f /data/swapfile-bench && log "temporary swap removed"
