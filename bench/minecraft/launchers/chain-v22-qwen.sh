#!/bin/bash
# v22 = v21 + "check from a fresh start, the way the user first meets it" (prompt + finish review). 2 Qwen samples.
cd /root/projects/Rameness
for b in mc-rameness-v22 mc-rameness-v22b; do
  python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch $b > /root/bench/chain-rameness-${b#mc-rameness-}-qwen.log 2>&1
  python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}-qwen.log 2>&1
done
