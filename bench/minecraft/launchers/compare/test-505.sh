#!/bin/bash
# EXL3 5.05bpw (TabbyAPI + MTP, CMP 170HX + RTX 5070 Ti, 96 cold experts/layer on CPU): speed + quality + Minecraft.
S=/root/bench/compare/status.txt; cd /root/bench/compare
L=exl3-505-tabby-mtp; M=Qwen3.8-Flash-Next-exl3-5.05bpw
echo "$L tests start $(date +%T)" >> $S
nvidia-smi --query-gpu=memory.used --format=csv,noheader | paste -sd+ | sed "s/^/{\"config\": \"$L\", \"vram\": \"/; s/$/\"}/" >> speed.jsonl
/root/models/drafts/venv/bin/python /root/bench/speed/measure_any.py $L http://127.0.0.1:5000 $M 1000,32000,96000,160000 \
  '{"ban_eos_token": true, "template_vars": {"enable_thinking": false}}' >> speed.jsonl 2>> log-$L.txt
python3 /root/bench/quality/eval.py --url http://127.0.0.1:5000 --model $M --label $L \
  --extra '{"template_vars": {"enable_thinking": false}}' >> quality.jsonl 2>> log-$L.txt
echo "$L quality done $(date +%T)" >> $S
cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:exl3-505 --batch mc-exl3-505-vision-1 --serial \
  > /root/bench/results/mc-exl3-505-vision-1.log 2>&1
echo "mc exl3-505 done $(date +%T)" >> $S
systemctl stop tabby-505; sleep 10
systemctl start llama-server
echo "test-505 done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
