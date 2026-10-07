#!/bin/bash
# GLM-5.3-Flash GSQ-RCO 3.0-bit Minecraft run despite a borderline probe (8.0-10.6 tok/s), then restore llama-server.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; D=/root/models/glm
systemctl stop llama-server; sleep 10
systemctl reset-failed srv-glm 2>/dev/null
systemd-run --unit=srv-glm -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 --jinja -m $D/GLM-5.3-Flash-GSQ-RCO-3.0bit.gguf \
  --mmproj $D/GLM-5.3-Flash-mmproj-BF16.gguf --fit on --fit-target 1536,1024 --fit-ctx 131072 -c 131072 -ctk q8_0 -ctv q8_0 -fa on
if timeout 2400 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-glm; do sleep 5; done' \
   && systemctl is-active -q srv-glm; then
  timeout 900 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1
  echo "mc glm start (slow: ~8-11 tok/s) $(date +%T)" >> $S
  cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:glm --batch mc-glm-gsq-3bit-vision-1 --serial \
    > /root/bench/results/mc-glm-gsq-3bit-vision-1.log 2>&1
  echo "mc glm done $(date +%T)" >> $S
else echo "mc glm: server failed $(date +%T)" >> $S; fi
systemctl stop srv-glm; sleep 10
systemctl start llama-server
echo "mc-glm job done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
