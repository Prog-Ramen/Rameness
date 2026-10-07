#!/bin/bash
# Minecraft benchmark on EXL3 4.05bpw (TabbyAPI + MTP, vision), then restore llama-server.
S=/root/bench/compare/status.txt
systemctl stop llama-server; sleep 5
systemctl reset-failed tabby-405 2>/dev/null
systemd-run --unit=tabby-405 -p WorkingDirectory=/root/exl3/tabby -p LimitMEMLOCK=infinity /root/exl3/tabby/venv/bin/python main.py --config config-405.yml
if timeout 2400 bash -c 'until curl -sf -m3 127.0.0.1:5000/health >/dev/null; do sleep 5; done'; then
  sed -i 's/Qwen3.8-Flash-Next-exl3-3.05bpw/Qwen3.8-Flash-Next-exl3-4.05bpw/' /root/exl3/smoke.py
  timeout 600 python3 /root/exl3/smoke.py >> $S 2>&1
  echo "mc exl3-405 start $(date +%T)" >> $S
  cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:exl3-405 --batch mc-exl3-405-vision-1 --serial \
    > /root/bench/results/mc-exl3-405-vision-1.log 2>&1
  python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-exl3-405-vision-1 >> /root/bench/results/mc-exl3-405-vision-1.log 2>&1
  echo "mc exl3-405 done $(date +%T)" >> $S
else
  echo "tabby-405 failed to start $(date +%T)" >> $S
fi
systemctl stop tabby-405; sleep 10
systemctl start llama-server
