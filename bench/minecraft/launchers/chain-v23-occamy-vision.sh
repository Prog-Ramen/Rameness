#!/bin/bash
# occamy with vision (server: projector on, DFlash off) on the current Rameness; compare with v22 / v22b (no vision).
cd /root/projects/Rameness
for b in mc-rameness-v23 mc-rameness-v23b; do
  python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch $b > /root/bench/chain-rameness-${b#mc-rameness-}-occamy.log 2>&1
  python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}-occamy.log 2>&1
done
