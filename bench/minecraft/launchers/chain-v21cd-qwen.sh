#!/bin/bash
# Two more Qwen samples of the current code (v21) so the version comparison rests on 4 runs, then judge.
cd /root/projects/Rameness
for b in mc-rameness-v21c mc-rameness-v21d; do
  python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch $b > /root/bench/chain-rameness-${b#mc-rameness-}-qwen.log 2>&1
  python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}.log 2>&1
done
