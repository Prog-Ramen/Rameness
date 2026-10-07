#!/bin/bash
# occamy v23b: second vision sample, now with the self-kill guard (v23 died at 116 min to its own `pkill -f serve`).
cd /root/projects/Rameness
python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v23b > /root/bench/chain-rameness-v23b-occamy.log 2>&1
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-rameness-v23b > /root/bench/judge-v23b-occamy.log 2>&1
