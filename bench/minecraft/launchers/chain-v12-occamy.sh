#!/bin/bash
# occamy v12 rerun (the first attempt hit a degraded server at 2-3 tok/s); waits for >= 15 tok/s first.
cd /root/projects/Rameness
python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v12 > /root/bench/chain-rameness-v12-occamy.log 2>&1
