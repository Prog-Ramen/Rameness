#!/bin/bash
# v21 = v20 + JEV decides whether a tool-less reply finished or stopped right after announcing a step
# (occamy did this 4x in v20 and the 4th ended the run at 90 min). occamy gets a clean sampling A/B on v21 code:
# v21 = model card (temp 1.0, presence 1.5), v21s = temp 0.6, presence 0.
cd /root/projects/Rameness
S='{"sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "chat_template_kwargs": {"enable_thinking": true, "preserve_thinking": true}}}'
( while ! grep -q "done in" /root/bench/chain-rameness-v20b-occamy.log 2>/dev/null; do sleep 60; done
  python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v21 > /root/bench/chain-rameness-v21-occamy.log 2>&1
  python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v21s --rameness-config "$S" > /root/bench/chain-rameness-v21s-occamy.log 2>&1 ) &
while ! grep -q "done in" /root/bench/chain-rameness-v20b-qwen.log 2>/dev/null; do sleep 60; done
python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch mc-rameness-v21 > /root/bench/chain-rameness-v21-qwen.log 2>&1
python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch mc-rameness-v21b > /root/bench/chain-rameness-v21b-qwen.log 2>&1
wait
for b in mc-rameness-v21 mc-rameness-v21s mc-rameness-v21b; do python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}.log 2>&1; done
