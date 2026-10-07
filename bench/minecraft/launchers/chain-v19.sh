#!/bin/bash
# v19 = v18 + "keep the verified-working version; diff against it when a change breaks it" (generic).
cd /root/projects/Rameness
( while ! grep -q "done in" /root/bench/chain-rameness-v18-occamy.log 2>/dev/null; do sleep 60; done
  python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v19 > /root/bench/chain-rameness-v19-occamy.log 2>&1 ) &
while ! grep -q "done in" /root/bench/chain-rameness-v18b-qwen.log 2>/dev/null; do sleep 60; done
python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch mc-rameness-v19 > /root/bench/chain-rameness-v19-qwen.log 2>&1
wait
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-rameness-v19 > /root/bench/judge-v19.log 2>&1
