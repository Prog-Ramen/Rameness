#!/bin/bash
# after the v12/v13 chain: v14 on Qwen (stand-in principle; delegation off)
while pgrep -f chain-v12.sh >/dev/null; do sleep 30; done
cd /root/projects/Rameness
python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch mc-rameness-v14 > /root/bench/chain-rameness-v14-qwen.log 2>&1
