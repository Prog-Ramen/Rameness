#!/bin/bash
# Minecraft benchmark on qwen3.8-flash-next-iq3_s-gsq-rco WITH vision (projector on GPU), every harness, one at a time.
cd /root/projects/Rameness
python3 -m bench.minecraft.run --serial --runs rameness:flashnext,claude-code:flashnext,pi:flashnext,dsh:flashnext \
  --timeout 9000 --batch mc-flashnext-vision-1 > /root/bench/chain-flashnext-vision.log 2>&1
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-flashnext-vision-1 > /root/bench/judge-flashnext-vision-1.log 2>&1
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-flashnext-1 > /root/bench/judge-flashnext-1.log 2>&1
