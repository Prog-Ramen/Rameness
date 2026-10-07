#!/bin/bash
# GLM-5.3-Flash GSQ-RCO 3.0-bit (pfeifferj) on llama.cpp master across both GPUs, overflow experts on the CPU (mmap).
# Speed probe first; quality + Minecraft only if it reaches 10 tok/s. Restores llama-server at the end.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; D=/root/models/glm
# (started directly at the user's request)
/root/vllm/.venv/bin/python -c "
from huggingface_hub import snapshot_download
snapshot_download('pfeifferj/GLM-5.3-Flash-GSQ-RCO-GGUF', local_dir='$D', max_workers=4,
                  allow_patterns=['GLM-5.3-Flash-GSQ-RCO-3.0bit.gguf', 'GLM-5.3-Flash-mmproj-BF16.gguf'])" > $D-download.log 2>&1 \
  || { echo "glm download failed $(date +%T)" >> $S; exit 1; }
N=$(/root/models/drafts/venv/bin/python /root/bench/compare/ncmoe.py $D/GLM-5.3-Flash-GSQ-RCO-3.0bit.gguf 68 2>> $S)
echo "glm downloaded; layers and CPU experts placed by --fit $(date +%T)" >> $S
systemctl stop llama-server; sleep 10
systemctl reset-failed srv-glm 2>/dev/null
systemd-run --unit=srv-glm -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 --jinja -m $D/GLM-5.3-Flash-GSQ-RCO-3.0bit.gguf \
  --mmproj $D/GLM-5.3-Flash-mmproj-BF16.gguf --fit on --fit-target 1536,1024 --fit-ctx 131072 -c 131072 -ctk q8_0 -ctv q8_0 -fa on
L=glm-gsq-3.0bit
if timeout 2400 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-glm; do sleep 5; done' \
   && systemctl is-active -q srv-glm; then
  nvidia-smi --query-gpu=memory.used --format=csv,noheader | paste -sd+ | sed "s/^/{\"config\": \"$L\", \"vram\": \"/; s/$/\"}/" >> /root/bench/compare/speed.jsonl
  /root/models/drafts/venv/bin/python /root/bench/speed/measure_any.py $L-probe http://127.0.0.1:8033 x 1000 > /root/bench/compare/glm-probe.jsonl 2>&1
  TPS=$(python3 -c "import json; r=[json.loads(l) for l in open('/root/bench/compare/glm-probe.jsonl') if 'decode_tps' in l]; print(min(x['decode_tps'] for x in r) if r else 0)")
  echo "glm probe: $TPS tok/s at 1k $(date +%T)" >> $S
  if python3 -c "import sys; sys.exit(0 if $TPS >= 10 else 1)"; then
    timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1
    /root/models/drafts/venv/bin/python /root/bench/speed/measure_any.py $L http://127.0.0.1:8033 x 1000,32000,96000 >> /root/bench/compare/speed.jsonl 2>> /root/bench/compare/log-$L.txt
    python3 /root/bench/quality/eval.py --url http://127.0.0.1:8033 --label $L >> /root/bench/compare/quality.jsonl 2>> /root/bench/compare/log-$L.txt
    echo "glm quality done $(date +%T)" >> $S
    cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:glm --batch mc-glm-gsq-3bit-vision-1 --serial \
      > /root/bench/results/mc-glm-gsq-3bit-vision-1.log 2>&1
    echo "mc glm done $(date +%T)" >> $S
  else echo "glm too slow for the full tests; stopped after the probe" >> $S; fi
else echo "glm server failed: $(journalctl -u srv-glm --no-pager -o cat | grep -E ' E |GGML_ASSERT' | head -2 | tr '\n' ' ' | cut -c1-250) $(date +%T)" >> $S; fi
systemctl stop srv-glm; sleep 10
systemctl start llama-server
echo "test-glm done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
