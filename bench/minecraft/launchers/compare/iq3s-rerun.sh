#!/bin/bash
# Rerun of the IQ3_S xhigh Minecraft run at 196k context (262k + MTP + vision ran out of VRAM at 158k).
S=/root/bench/compare/status.txt
B=/root/models/llama.cpp/llama-mtp-fmad/build/bin
until grep -q "overnight done" $S; do sleep 60; done
systemctl stop llama-server; sleep 10
systemctl reset-failed srv-iq3s 2>/dev/null
systemd-run --unit=srv-iq3s -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 \
  --presence-penalty 0 --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' \
  -m /root/models/qwen38-flash-next-gsq-rco/IQ3_S/Qwen3.8-Flash-Next-GSQ-RCO-IQ3_S-00001-of-00002.gguf \
  --mmproj /root/models/qwen38-flash-next-gsq-rco/mmproj-Qwen3.8-Flash-Next-BF16.gguf -ngl 99 -lm mmap --lazy-mode on \
  -c 196608 -ctk q8_0 -ctv q8_0 \
  --spec-type draft-mtp -md /root/models/drafts/flashnext-mtp-q8_0-embd.gguf --spec-draft-ngl 99 --spec-draft-n-max 3
if timeout 1500 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok; do sleep 5; done'; then
  echo "mc iq3s-196k start $(date +%T); vram $(nvidia-smi --query-gpu=memory.used --format=csv,noheader)" >> $S
  cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:flashnext --batch mc-iq3s-mtp-xhigh-196k-1 --serial \
    > /root/bench/results/mc-iq3s-mtp-xhigh-196k-1.log 2>&1
  echo "mc iq3s-196k done $(date +%T)" >> $S
else echo "iq3s-196k server failed $(date +%T)" >> $S; fi
systemctl stop srv-iq3s; sleep 10
systemctl start llama-server
echo "iq3s rerun done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
