#!/bin/bash
S=/root/bench/compare/status.txt
echo "mc q27bf16 start $(date +%T)" >> $S
cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:q27bf16 --batch mc-q27bf16-vision-1 --serial \
  > /root/bench/results/mc-q27bf16-vision-1.log 2>&1
echo "mc q27bf16 done $(date +%T)" >> $S
systemctl stop vllm-q27; sleep 15
