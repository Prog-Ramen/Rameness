#!/bin/bash
# After the running proportional-checks A/B: the bugfix2 baseline (Rameness vs Claude Code, 3 each), then the
# remaining A/Bs on the more discriminating task set.
cd /root/projects/Rameness
while pgrep -f "bench.ab --name" >/dev/null; do sleep 60; done
for i in 1 2 3; do
  for h in rameness claude-code; do
    python3 -m bench.minecraft.run --task bugfix2 --runs $h:qwen --timeout 2700 --batch nt-bugfix2-$i >> /root/bench/chain-newtasks.log 2>&1
  done
done
T=bugfix2,research,data
python3 -m bench.ab --name scope-discipline --tasks $T --config '{"scope_discipline": true}' > /root/bench/ab/scope-discipline.log 2>&1
python3 -m bench.ab --name thinking-full --tasks $T --config '{"thinking": {"mode": "fixed"}}' > /root/bench/ab/thinking-full.log 2>&1
python3 -m bench.ab --name edit-fuzzy --tasks $T --config '{"edit_fuzzy": true}' > /root/bench/ab/edit-fuzzy.log 2>&1
