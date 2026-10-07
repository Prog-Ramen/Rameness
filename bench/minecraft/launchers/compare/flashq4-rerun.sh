#!/bin/bash
# Flash-Next Unsloth UD-Q4_K_XL rerun: more expert layers on the CPU so the MTP head (3.3 GB) fits; 196k context.
S=/root/bench/compare/status.txt
B=/root/models/llama.cpp/llama-mtp-fmad/build/bin
until grep -q "iq3s rerun done" $S; do sleep 60; done
/root/vllm/.venv/bin/python -c "
from huggingface_hub import snapshot_download
snapshot_download('unsloth/Qwen3.8-Flash-Next-GGUF', local_dir='/root/models/unsloth/flash',
                  allow_patterns=['UD-Q4_K_XL/*', 'MTP/mtp-Qwen3.8-Flash-Next-Q8_0.gguf', 'mmproj-BF16.gguf'], max_workers=4)" \
  > /root/models/unsloth/dl2.log 2>&1 || { echo "flashq4 re-download failed $(date +%T)" >> $S; exit 1; }
F=/root/models/unsloth/flash/UD-Q4_K_XL/Qwen3.8-Flash-Next-UD-Q4_K_XL-00001-of-00004.gguf
N=$(/root/models/drafts/venv/bin/python /root/bench/compare/ncmoe.py $F 46 2>/dev/null)
echo "flashq4 rerun: --n-cpu-moe $N $(date +%T)" >> $S
systemctl stop llama-server; sleep 10
systemctl reset-failed srv-flashq4 2>/dev/null
systemd-run --unit=srv-flashq4 -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 \
  --presence-penalty 0 --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' \
  -m $F --mmproj /root/models/unsloth/flash/mmproj-BF16.gguf -ngl 99 --n-cpu-moe $N -lm mmap --lazy-mode on \
  -c 196608 -ctk q8_0 -ctv q8_0 \
  --spec-type draft-mtp -md /root/models/unsloth/flash/MTP/mtp-Qwen3.8-Flash-Next-Q8_0.gguf --spec-draft-ngl 99 --spec-draft-n-max 3
if timeout 1800 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok; do sleep 5; done'; then
  timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1
  echo "mc flashq4 start $(date +%T); vram $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)" >> $S
  cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:flashq4 --batch mc-flash-ud-q4kxl-vision-1 --serial \
    > /root/bench/results/mc-flash-ud-q4kxl-vision-1.log 2>&1
  echo "mc flashq4 done $(date +%T)" >> $S
else echo "flashq4 rerun server failed: $(journalctl -u srv-flashq4 --no-pager | grep -E ' E ' | head -2 | tr '\n' ' ' | cut -c1-250) $(date +%T)" >> $S; fi
systemctl stop srv-flashq4; sleep 10
systemctl start llama-server
echo "flashq4 rerun done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
