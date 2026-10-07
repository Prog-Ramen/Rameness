#!/bin/bash
# v17 = lenient anchors + "use the file tools, not the shell" (generic). occamy now; Qwen after its v16 finishes.
cd /root/projects/Rameness
python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v17 > /root/bench/chain-rameness-v17-occamy.log 2>&1 &
while ! grep -q "done in" /root/bench/chain-rameness-v16-qwen.log 2>/dev/null; do sleep 30; done
python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch mc-rameness-v17 > /root/bench/chain-rameness-v17-qwen.log 2>&1
wait
