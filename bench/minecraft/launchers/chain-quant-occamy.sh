#!/bin/bash
# Waits until the occamy server loads a model other than APEX MiniPlus, records its speed, then runs 2 benchmark
# runs (current Rameness) and judges them - to compare with the MiniPlus runs.
cd /root/projects/Rameness
until m=$(curl -s -m10 http://<lan-host>:8034/props | python3 -c "import json,sys; print(json.load(sys.stdin).get('model_path',''))" 2>/dev/null) && [ -n "$m" ] && ! echo "$m" | grep -q "APEX-I-MiniPlus"; do sleep 120; done
echo "$(date +%T) new model: $m" > /root/bench/quant-test.log
for n in 400 1500; do curl -s -m1500 http://<lan-host>:8034/v1/chat/completions -H 'content-type: application/json' -d "{\"messages\":[{\"role\":\"user\",\"content\":\"Write a complete, long JavaScript module implementing a voxel chunk mesher with greedy meshing and face culling. Output only the code.\"}],\"max_tokens\":$n,\"temperature\":0.6,\"chat_template_kwargs\":{\"enable_thinking\":false}}" | python3 -c "import json,sys; t=json.load(sys.stdin)['timings']; print('code', t['predicted_n'], 'tokens:', round(t['predicted_per_second'],1), 'tok/s; prompt', round(t['prompt_per_second']), 'tok/s')" >> /root/bench/quant-test.log; done
for b in mc-rameness-q1 mc-rameness-q1b; do
  python3 -m bench.minecraft.run --runs rameness:occamy --timeout 9000 --batch $b > /root/bench/chain-rameness-${b#mc-rameness-}-occamy.log 2>&1
  python3 -m bench.minecraft.judge /root/bench/results/minecraft/$b > /root/bench/judge-${b#mc-rameness-}-occamy.log 2>&1
done
