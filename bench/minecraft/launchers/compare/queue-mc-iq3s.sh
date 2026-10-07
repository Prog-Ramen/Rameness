#!/bin/bash
# After the EXL3 Minecraft run: stop TabbyAPI, serve the IQ3_S on llama.cpp master + MTP (vision on) at :8033,
# run the same Minecraft benchmark, then restore the normal llama-server service.
while pgrep -f "bench.minecraft.run --runs rameness:exl3" >/dev/null; do sleep 60; done
systemctl stop tabby-exl3; sleep 10
systemd-run --unit=llama-mtp -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity \
  /bin/bash /root/bench/speed/run-newbuild.sh --spec-type draft-mtp -md /root/models/drafts/flashnext-mtp-q8_0-embd.gguf \
  --spec-draft-ngl 99 --spec-draft-n-max 3
timeout 900 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok; do sleep 5; done' || { echo "llama-mtp failed to start" >> /root/bench/compare/status.txt; exit 1; }
cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:flashnext --batch mc-iq3s-mtp-vision-1 --serial \
  > /root/bench/results/mc-iq3s-mtp-vision-1.log 2>&1
systemctl stop llama-mtp; sleep 10
systemctl start llama-server
echo "minecraft pair done $(date +%T); service $(systemctl is-active llama-server)" >> /root/bench/compare/status.txt
