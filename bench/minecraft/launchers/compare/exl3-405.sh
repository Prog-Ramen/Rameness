#!/bin/bash
# After the Minecraft pair: download EXL3 4.05bpw (h6/ng6), serve it on TabbyAPI + MTP with 96 cold experts/layer
# on the CPU, measure speed + quality like the others, then restore llama-server.
S=/root/bench/compare/status.txt; cd /root/bench/compare
while systemctl is-active -q mc-pair; do sleep 60; done
/root/vllm/.venv/bin/python -c "
from huggingface_hub import snapshot_download
snapshot_download(repo_id='turboderp/Qwen3.8-Flash-Next-exl3', revision='4.05bpw_h6_ng6',
                  local_dir='/root/exl3/models/Qwen3.8-Flash-Next-exl3-4.05bpw', max_workers=4)" > exl3-405-download.log 2>&1 \
  || { echo "exl3 4.05 download failed $(date +%T)" >> $S; exit 1; }
echo "exl3 4.05 downloaded $(date +%T)" >> $S
systemctl stop llama-server; sleep 5
systemd-run --unit=tabby-405 -p WorkingDirectory=/root/exl3/tabby -p LimitMEMLOCK=infinity \
  /root/exl3/tabby/venv/bin/python main.py --config config-405.yml
if timeout 2400 bash -c 'until curl -sf -m3 127.0.0.1:5000/health >/dev/null; do sleep 5; done'; then
  L=exl3-405-tabby-mtp
  nvidia-smi --query-gpu=memory.used --format=csv,noheader | sed "s/^/{\"config\": \"$L\", \"vram\": \"/; s/$/\"}/" >> speed.jsonl
  /root/models/drafts/venv/bin/python /root/bench/speed/measure_any.py $L http://127.0.0.1:5000 Qwen3.8-Flash-Next-exl3-4.05bpw \
    1000,32000,96000,160000 '{"ban_eos_token": true, "template_vars": {"enable_thinking": false}}' >> speed.jsonl 2>> log-$L.txt
  python3 /root/bench/quality/eval.py --url http://127.0.0.1:5000 --model Qwen3.8-Flash-Next-exl3-4.05bpw --label $L \
    --extra '{"template_vars": {"enable_thinking": false}}' >> quality.jsonl 2>> log-$L.txt
  echo "$L done $(date +%T)" >> $S
else
  echo "exl3 4.05 failed to start: $(journalctl -u tabby-405 --no-pager | grep -iE 'error|memory' | tail -2 | tr '\n' ' ' | cut -c1-300)" >> $S
fi
systemctl stop tabby-405; sleep 10
systemctl start llama-server
