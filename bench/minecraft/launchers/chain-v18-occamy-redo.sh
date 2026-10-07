#!/bin/bash
# occamy v18 crashed on a concurrent reinstall; rerun it after occamy v18b finishes
cd /root/projects/Rameness
while ! grep -q "done in" /root/bench/chain-rameness-v18b-occamy.log 2>/dev/null; do sleep 60; done
python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v18 > /root/bench/chain-rameness-v18-occamy.log 2>&1
python3 -m bench.minecraft.judge /root/bench/results/minecraft/mc-rameness-v18 > /root/bench/judge-v18-occamy.log 2>&1
