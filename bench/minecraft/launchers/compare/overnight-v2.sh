#!/bin/bash
# User go-ahead 2026-10-06 ("continue overnight with the queue"): the new Rameness defaults (Opus-style workflow +
# task_scope: open-ended -> 3 feature cycles) on the Minecraft build, 27B Unsloth UD-Q4_K_M, same settings as
# mc-q27-ud-q4km-vision-1 (100%, visual 7.25). 1) JEV = Kev (CPU bf16)  2) JEV = Clef-flash (5070 Ti, 8-bit).
# Restores llama-server and Kev at the end. Kev MUST be bf16 (KEV_DTYPE=bf16): fp32 (~19 GB) OOM-kills the model server.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; M=/root/models/unsloth/q27
log() { echo "$* $(date +%T)" >> $S; }
ping_jev() { curl -s -m10 $1/v1/systemone -H "Content-Type: application/json" \
  -d '{"state":"ping","questions":{"up":{"type":"noul","instructions":"Is this a ping?"}}}' | grep -q answers; }
start_kev() {
  ping_jev http://127.0.0.1:8008 && return 0
  systemctl stop kev 2>/dev/null; systemctl reset-failed kev 2>/dev/null
  systemd-run --unit=kev -p WorkingDirectory=/root/.rameness/jev/kev/src -E CUDA_VISIBLE_DEVICES= -E KEV_DTYPE=bf16 \
    /root/.rameness/jev/kev/src/.venv/bin/python -m kev.serve --run jaredpalmer/kev-4b --port 8008
  timeout 600 bash -c "until curl -s -m5 127.0.0.1:8008/v1/systemone -H 'Content-Type: application/json' -d '{\"state\":\"ping\",\"questions\":{\"up\":{\"type\":\"noul\",\"instructions\":\"Is this a ping?\"}}}' | grep -q answers; do sleep 5; done"
}
restore() { systemctl stop srv-q27q4 srv-clef 2>/dev/null; sleep 10; start_kev; systemctl start llama-server
  log "overnight-v2 finished; llama-server $(systemctl is-active llama-server), kev $(systemctl is-active kev)"; }
mc() {  # batch, config overlay
  log "mc $1 start"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:q27q4 --batch $1 --serial \
     ${2:+--rameness-config "$2"} > /root/bench/results/$1.log 2>&1)
  log "mc $1 done"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.judge /root/bench/results/minecraft/$1 >> /root/bench/results/$1.log 2>&1)
  log "mc $1 judged: $(grep -o 'overall [0-9.]*' /root/bench/results/$1.log | tail -1)"
}

systemctl stop llama-server; sleep 10
start_kev || { log "overnight-v2: kev failed"; restore; exit 1; }
systemctl reset-failed srv-q27q4 2>/dev/null
systemd-run --unit=srv-q27q4 -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B -E CUDA_VISIBLE_DEVICES=0 \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 \
  --presence-penalty 0 --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' \
  -m $M/Qwen3.8-27B-UD-Q4_K_M.gguf --mmproj $M/mmproj-BF16.gguf -ngl 99 -c 262144 -ctk q8_0 -ctv q8_0 \
  --spec-type draft-mtp -md $M/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99 --spec-draft-n-max 3
if ! timeout 1500 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-q27q4; do sleep 5; done' \
   || ! systemctl is-active -q srv-q27q4; then log "overnight-v2: q27q4 server failed"; restore; exit 1; fi

# 1) new defaults, JEV = Kev
mc mc-q27q4-v2-1 ""

# 2) the same with Clef-flash as JEV (Kev stopped meanwhile: Kev + Clef in RAM caused the 03:35 OOM)
systemctl stop kev
systemctl reset-failed srv-clef 2>/dev/null
systemd-run --unit=srv-clef -p WorkingDirectory=/data/clef -E CUDA_VISIBLE_DEVICES=1 -E CUDA_DEVICE_ORDER=PCI_BUS_ID \
  /data/clef/venv/bin/python /data/clef/serve.py --bits 8
if timeout 900 bash -c 'until curl -sf -m3 127.0.0.1:8010/health >/dev/null || ! systemctl is-active -q srv-clef; do sleep 5; done' \
   && ping_jev http://127.0.0.1:8010; then
  mc mc-q27q4-v2-clef-1 '{"jev": {"backend": "kev", "kev_url": "http://127.0.0.1:8010/v1/systemone", "kev_model": "clef-flash"}}'
else log "overnight-v2: clef server failed"; fi
restore
