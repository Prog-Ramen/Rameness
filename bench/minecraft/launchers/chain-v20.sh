#!/bin/bash
# v20 = v19 + "prefer established, tested libraries for complex error-prone parts" (generic). 2 samples per model.
cd /root/projects/Rameness
run2() {  # model, log-to-wait-for
  while ! grep -q "done in" "$2" 2>/dev/null; do sleep 60; done
  for b in mc-rameness-v20 mc-rameness-v20b; do
    python3 -m bench.minecraft.run --runs rameness:$1 --timeout 9000 --batch $b > /root/bench/chain-rameness-${b#mc-rameness-}-$1.log 2>&1
  done
}
run2 occamy /root/bench/chain-rameness-v19-occamy.log &
run2 qwen /root/bench/chain-rameness-v19-qwen.log
wait
for b in mc-rameness-v20 mc-rameness-v20b; do python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}.log 2>&1; done
