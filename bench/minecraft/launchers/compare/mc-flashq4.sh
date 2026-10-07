#!/bin/bash
# Flash-Next Unsloth UD-Q4_K_XL Minecraft run on the already-started srv-flashq4 (no MTP: the Unsloth MTP head fails at
# slot init together with --n-cpu-moe), then restore llama-server.
S=/root/bench/compare/status.txt
echo "mc flashq4 start (no MTP, --n-cpu-moe 18) $(date +%T)" >> $S
cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:flashq4 --batch mc-flash-ud-q4kxl-vision-1 --serial \
  > /root/bench/results/mc-flash-ud-q4kxl-vision-1.log 2>&1
echo "mc flashq4 done $(date +%T)" >> $S
systemctl stop srv-flashq4; sleep 10
systemctl start llama-server
echo "flashq4 rerun done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
