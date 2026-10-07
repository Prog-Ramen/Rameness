#!/bin/bash
# occamy v14: waits (inside the runner) until the server holds >= 15 tok/s over a 1500-token reply
cd /root/projects/Rameness
python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch mc-rameness-v14 > /root/bench/chain-rameness-v14-occamy.log 2>&1
