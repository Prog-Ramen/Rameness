#!/bin/bash
# A/B: JEV progress review off vs on (v22 / v22b have it on). Same code otherwise. Qwen x2 after v22b; occamy x1
# after its v22b. Decide from the results whether the review earns its place.
cd /root/projects/Rameness
OFF='{"progress_review": {"every": 0}}'
( while ! grep -q "done in" /root/bench/chain-rameness-v22b-occamy.log 2>/dev/null; do sleep 60; done
  python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v22nr --rameness-config "$OFF" > /root/bench/chain-rameness-v22nr-occamy.log 2>&1 ) &
while ! grep -q "done in" /root/bench/chain-rameness-v22b-qwen.log 2>/dev/null; do sleep 60; done
for b in mc-rameness-v22nr mc-rameness-v22nrb; do
  python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch $b --rameness-config "$OFF" > /root/bench/chain-rameness-${b#mc-rameness-}-qwen.log 2>&1
done
wait
for b in mc-rameness-v22nr mc-rameness-v22nrb; do python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}.log 2>&1; done
