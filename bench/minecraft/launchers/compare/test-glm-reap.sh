#!/bin/bash
# GLM-5.3-Flash REAP50 IQ3_M (half the experts pruned, 72 GB) on llama.cpp master, --fit across both GPUs.
# Probe speed; at >=10 tok/s run speed + quality + Minecraft. Restores llama-server at the end.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-reap50/build/bin; D=/root/models/glm-reap
# B is the REAP50 author's llama.cpp fork: stock llama.cpp cannot load these files
# (already downloaded)
echo "glm-reap downloaded $(date +%T)" >> $S
systemctl stop llama-server; sleep 10
systemctl reset-failed srv-glm 2>/dev/null
systemd-run --unit=srv-glm -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 --jinja -m $D/GLM-5.3-Flash-REAP50-IQ3_M.gguf \
  --mmproj $D/mmproj-GLM-5.3-Flash-REAP50-F16.gguf --fit on --fit-target 1536,1024 --fit-ctx 131072 -c 131072 -ctk q8_0 -ctv q8_0 -fa on
L=glm-reap50-iq3m
if timeout 2400 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-glm; do sleep 5; done' \
   && systemctl is-active -q srv-glm; then
  nvidia-smi --query-gpu=memory.used --format=csv,noheader | paste -sd+ | sed "s/^/{\"config\": \"$L\", \"vram\": \"/; s/$/\"}/" >> /root/bench/compare/speed.jsonl
  /root/models/drafts/venv/bin/python /root/bench/speed/measure_any.py $L-probe http://127.0.0.1:8033 x 1000 > /root/bench/compare/glm-reap-probe.jsonl 2>&1
  TPS=$(python3 -c "import json; r=[json.loads(l) for l in open('/root/bench/compare/glm-reap-probe.jsonl') if 'decode_tps' in l]; print(min(x['decode_tps'] for x in r) if r else 0)")
  echo "glm-reap probe: $TPS tok/s at 1k $(date +%T)" >> $S
  if python3 -c "import sys; sys.exit(0 if $TPS >= 10 else 1)"; then
    timeout 900 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1
    /root/models/drafts/venv/bin/python /root/bench/speed/measure_any.py $L http://127.0.0.1:8033 x 1000,32000,96000 >> /root/bench/compare/speed.jsonl 2>> /root/bench/compare/log-$L.txt
    python3 /root/bench/quality/eval.py --url http://127.0.0.1:8033 --label $L >> /root/bench/compare/quality.jsonl 2>> /root/bench/compare/log-$L.txt
    echo "glm-reap quality done $(date +%T)" >> $S
    cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:glm-reap --batch mc-glm-reap50-iq3m-vision-1 --serial \
      > /root/bench/results/mc-glm-reap50-iq3m-vision-1.log 2>&1
    echo "mc glm-reap done $(date +%T)" >> $S
  else echo "glm-reap too slow; stopped after the probe" >> $S; fi
else echo "glm-reap server failed: $(journalctl -u srv-glm --since "-30min" --no-pager -o cat | grep -E ' E |GGML_ASSERT' | head -2 | tr '\n' ' ' | cut -c1-250) $(date +%T)" >> $S; fi
systemctl stop srv-glm; sleep 10
systemctl start llama-server
echo "test-glm-reap done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
