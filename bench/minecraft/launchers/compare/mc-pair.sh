#!/bin/bash
# Minecraft benchmark, EXL3 (TabbyAPI) first, then IQ3_S (llama.cpp master + MTP); restores llama-server at the end.
S=/root/bench/compare/status.txt
cd /root/projects/Rameness
systemctl stop llama-server; sleep 5
systemctl start tabby-exl3-run 2>/dev/null || systemd-run --unit=tabby-exl3 -p WorkingDirectory=/root/exl3/tabby -p LimitMEMLOCK=infinity /root/exl3/tabby/venv/bin/python main.py --config config.yml
timeout 900 bash -c 'until curl -sf -m3 127.0.0.1:5000/health >/dev/null; do sleep 5; done' || { echo "tabby failed $(date +%T)" >> $S; exit 1; }
echo "mc exl3 start $(date +%T)" >> $S
python3 -m bench.minecraft.run --runs rameness:exl3 --batch mc-exl3-vision-1 --serial > /root/bench/results/mc-exl3-vision-1.log 2>&1
echo "mc exl3 done $(date +%T)" >> $S
systemctl stop tabby-exl3; sleep 10
systemd-run --unit=llama-mtp -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity \
  /bin/bash /root/bench/speed/run-newbuild.sh --spec-type draft-mtp -md /root/models/drafts/flashnext-mtp-q8_0-embd.gguf \
  --spec-draft-ngl 99 --spec-draft-n-max 3
timeout 900 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok; do sleep 5; done' || { echo "llama-mtp failed $(date +%T)" >> $S; systemctl stop llama-mtp; systemctl start llama-server; exit 1; }
echo "mc iq3s start $(date +%T)" >> $S
python3 -m bench.minecraft.run --runs rameness:flashnext --batch mc-iq3s-mtp-vision-1 --serial > /root/bench/results/mc-iq3s-mtp-vision-1.log 2>&1
systemctl stop llama-mtp; sleep 10
systemctl start llama-server
echo "minecraft pair done $(date +%T); service $(systemctl is-active llama-server)" >> $S
