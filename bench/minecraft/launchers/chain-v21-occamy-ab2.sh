#!/bin/bash
# occamy sampling A/B, second sample of each arm on the same v21 code: v21sb (temp 0.6, presence 0), v21b (model card).
cd /root/projects/Rameness
S='{"sampling": {"temperature": 0.6, "top_p": 0.95, "top_k": 20, "min_p": 0.0, "presence_penalty": 0.0, "chat_template_kwargs": {"enable_thinking": true, "preserve_thinking": true}}}'
python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v21sb --rameness-config "$S" > /root/bench/chain-rameness-v21sb-occamy.log 2>&1
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-rameness-v21sb > /root/bench/judge-v21sb.log 2>&1
python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v21b > /root/bench/chain-rameness-v21b-occamy.log 2>&1
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-rameness-v21b > /root/bench/judge-v21b-occamy.log 2>&1
