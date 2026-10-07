#!/bin/bash
# occamy v22 = v21 + fresh-start rule + occamy default sampling temp 0.6 / presence 0 (from the A/B). 2 samples.
cd /root/projects/Rameness
for b in mc-rameness-v22 mc-rameness-v22b; do
  python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch $b > /root/bench/chain-rameness-${b#mc-rameness-}-occamy.log 2>&1
  python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}-occamy.log 2>&1
done
