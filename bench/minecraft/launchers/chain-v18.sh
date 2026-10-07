#!/bin/bash
# v18 = v17 + vision (read_file shows images to models that can see; Qwen server now has its projector on CPU)
#       + answer->agent escalation. Two samples per model (single runs proved too noisy to judge a change).
cd /root/projects/Rameness
( for b in mc-rameness-v18 mc-rameness-v18b; do
    python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch $b > /root/bench/chain-rameness-${b#mc-rameness-}-occamy.log 2>&1
  done ) &
for b in mc-rameness-v18 mc-rameness-v18b; do
  python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch $b > /root/bench/chain-rameness-${b#mc-rameness-}-qwen.log 2>&1
done
wait
for b in mc-rameness-v18 mc-rameness-v18b; do python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}.log 2>&1; done
