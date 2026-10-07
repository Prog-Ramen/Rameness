#!/bin/bash
# v12: current defaults (anchors, read stubs, note ledger) on Qwen and occamy in parallel (separate servers);
# then v13 on Qwen: same + JEV-gated sub-agents.
cd /root/projects/Rameness
python3 -m bench.minecraft.run --runs rameness:qwen,rameness:occamy --timeout 9000 --batch mc-rameness-v12 > /root/bench/chain-rameness-v12.log 2>&1
python3 -m bench.minecraft.run --runs rameness:qwen --timeout 9000 --batch mc-rameness-v13 --rameness-config '{"delegation": true}' > /root/bench/chain-rameness-v13-qwen.log 2>&1
