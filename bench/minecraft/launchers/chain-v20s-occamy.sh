#!/bin/bash
# A/B for occamy sampling: v20/v20b use the model card (temp 1.0, presence 1.5); v20s the same code at temp 0.6,
# presence 0 (the settings of occamy's best run, v7). Runs after occamy v20b.
cd /root/projects/Rameness
while ! grep -q "done in" /root/bench/chain-rameness-v20b-occamy.log 2>/dev/null; do sleep 60; done
python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v20s \
  --rameness-config '{"sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "chat_template_kwargs": {"enable_thinking": true, "preserve_thinking": true}}}' \
  > /root/bench/chain-rameness-v20s-occamy.log 2>&1
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-rameness-v20s > /root/bench/judge-v20s.log 2>&1
