#!/bin/bash
# A/B on occamy (vision on): describe-then-judge + final look (ON) vs both off (OFF), same code, interleaved.
cd /root/projects/Rameness
OFF='{"vision_describe_first": false, "vision_final_look": false}'
run() {  # batch, config
  if [ -n "$2" ]; then
    python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch $1 --rameness-config "$2" > /root/bench/chain-rameness-${1#mc-rameness-}-occamy.log 2>&1
  else
    python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch $1 > /root/bench/chain-rameness-${1#mc-rameness-}-occamy.log 2>&1
  fi
  python3 -m bench.minecraft.judge /root/bench/results/minecraft/$1 > /root/bench/judge-${1#mc-rameness-}-occamy.log 2>&1
}
run mc-rameness-v24on
run mc-rameness-v24off "$OFF"
run mc-rameness-v24onb
run mc-rameness-v24offb "$OFF"
