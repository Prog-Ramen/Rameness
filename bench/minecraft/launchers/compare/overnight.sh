#!/bin/bash
# Overnight Minecraft runs, one GPU server at a time, after the 27B BF16 run:
#   IQ3_S GSQ-RCO (xhigh) -> 27B Unsloth UD-Q4_K_M -> Flash-Next Unsloth UD-Q4_K_XL -> EXL3 5.05bpw.
# Every llama.cpp run: master build (-fmad=false) + MTP + vision + reasoning_effort xhigh. Restores llama-server at the end.
S=/root/bench/compare/status.txt
B=/root/models/llama.cpp/llama-mtp-fmad/build/bin
export LD_LIBRARY_PATH=$B
log() { echo "$* $(date +%T)" >> $S; }
SAMPLING=(--temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 --presence-penalty 0 --repeat-penalty 1.0)
XHIGH=(--chat-template-kwargs '{"reasoning_effort":"xhigh"}')

serve_llama() {  # unit, args...
  local unit=$1; shift
  systemctl reset-failed $unit 2>/dev/null
  systemd-run --unit=$unit -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B \
    $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on "${SAMPLING[@]}" "${XHIGH[@]}" "$@"
  timeout 1500 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok; do sleep 5; done'
}
bench() {  # model key, batch
  log "mc $2 start"
  (cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:$1 --batch $2 --serial \
     > /root/bench/results/$2.log 2>&1)
  log "mc $2 done"
}
smoke() { timeout 600 python3 /root/exl3/smoke_any.py "$@" >> $S 2>&1; }

while systemctl is-active -q mc-q27bf16; do sleep 60; done
systemctl stop vllm-q27 llama-server 2>/dev/null; sleep 10

# 1. Flash-Next IQ3_S GSQ-RCO, xhigh
if serve_llama srv-iq3s -m /root/models/qwen38-flash-next-gsq-rco/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf \
     --mmproj /root/models/qwen38-flash-next-gsq-rco/mmproj-Qwen3.8-Flash-Next-BF16.gguf -ngl 99 -lm mmap --lazy-mode on \
     -c 262144 -ctk q8_0 -ctv q8_0 \
     --spec-type draft-mtp -md /root/models/drafts/flashnext-mtp-q8_0-embd.gguf --spec-draft-ngl 99 --spec-draft-n-max 3; then
  smoke http://127.0.0.1:8033 x; bench flashnext mc-iq3s-mtp-xhigh-1
else log "iq3s server failed"; fi
systemctl stop srv-iq3s; sleep 10

# 2. Qwen3.8-27B Unsloth UD-Q4_K_M
until grep -q "27B done" /root/models/unsloth/dl.log; do sleep 60; done
if serve_llama srv-q27q4 -m /root/models/unsloth/q27/Qwen3.8-27B-UD-Q4_K_M.gguf --mmproj /root/models/unsloth/q27/mmproj-BF16.gguf \
     -ngl 99 -c 262144 -ctk q8_0 -ctv q8_0 \
     --spec-type draft-mtp -md /root/models/unsloth/q27/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99 --spec-draft-n-max 3; then
  smoke http://127.0.0.1:8033 x; bench q27q4 mc-q27-ud-q4km-vision-1
else log "q27q4 server failed: $(journalctl -u srv-q27q4 --no-pager | grep -iE ' E |error' | tail -2 | tr '\n' ' ' | cut -c1-250)"; fi
systemctl stop srv-q27q4; sleep 10

# 3. Flash-Next Unsloth UD-Q4_K_XL (no Q4_K_M published), routed experts of the overflow layers on the CPU
until grep -q "FLASH done" /root/models/unsloth/dl.log; do sleep 60; done
F=/root/models/unsloth/flash/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf
N=$(/root/models/drafts/venv/bin/python /root/bench/compare/ncmoe.py $F 52 2>> $S)
log "flashq4: --n-cpu-moe $N"
if serve_llama srv-flashq4 -m $F --mmproj /root/models/unsloth/flash/mmproj-BF16.gguf -ngl 99 --n-cpu-moe $N -lm mmap --lazy-mode on \
     -c 262144 -ctk q8_0 -ctv q8_0 \
     --spec-type draft-mtp -md /root/models/unsloth/flash/MTP/mtp-Qwen3.8-Flash-Next-Q8_0.gguf --spec-draft-ngl 99 --spec-draft-n-max 3; then
  smoke http://127.0.0.1:8033 x; bench flashq4 mc-flash-ud-q4kxl-vision-1
else log "flashq4 server failed: $(journalctl -u srv-flashq4 --no-pager | grep -iE ' E |error' | tail -2 | tr '\n' ' ' | cut -c1-250)"; fi
systemctl stop srv-flashq4; sleep 10

# 4. EXL3 5.05bpw (h6/ng6): transient, so make room first
rm -rf /root/models/unsloth/flash
/root/vllm/.venv/bin/python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='turboderp/Qwen3.8-Flash-Next-exl3', revision='5.05bpw_h6_ng6',
                  local_dir='/root/exl3/models/Qwen3.8-Flash-Next-exl3-5.05bpw', max_workers=4)" > /root/bench/compare/exl3-505-download.log 2>&1 \
  && log "exl3 5.05 downloaded" || log "exl3 5.05 download failed"
systemctl reset-failed tabby-505 2>/dev/null
# MemoryMax: if ~26 GB of CPU experts does not fit next to Kev, only TabbyAPI is killed
systemd-run --unit=tabby-505 -p WorkingDirectory=/root/exl3/tabby -p LimitMEMLOCK=infinity -p MemoryMax=21G \
  /root/exl3/tabby/venv/bin/python main.py --config config-505.yml
if timeout 2400 bash -c 'until curl -sf -m3 127.0.0.1:5000/health >/dev/null; do sleep 5; done'; then
  smoke http://127.0.0.1:5000 Qwen3.8-Flash-Next-exl3-5.05bpw; bench exl3-505 mc-exl3-505-vision-1
else log "exl3 5.05 failed to start: $(journalctl -u tabby-505 --no-pager | grep -iE 'error|memory|killed' | tail -2 | tr '\n' ' ' | cut -c1-250)"; fi
systemctl stop tabby-505; sleep 10

systemctl start llama-server
log "overnight done; service $(systemctl is-active llama-server)"
