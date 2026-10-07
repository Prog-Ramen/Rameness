#!/bin/bash
# bugfix2 baseline (skipping runs already done), then A/Bs in priority order. Keep only what passes.
cd /root/projects/Rameness
busy() { pgrep -f "bench.minecraft.run --task" >/dev/null || pgrep -f "bench.ab --name" >/dev/null; }
while busy; do sleep 30; done
for i in 1 2 3; do
  for h in rameness claude-code; do
    [ -f /root/bench/results/bugfix2/nt-bugfix2-$i/nt-bugfix2-$i-$h-qwen/result.json ] && continue
    python3 -m bench.minecraft.run --task bugfix2 --runs $h:qwen --timeout 2700 --batch nt-bugfix2-$i >> /root/bench/chain-newtasks.log 2>&1
  done
done
T=bugfix2,research,data
python3 -m bench.ab --name scope-discipline --tasks $T --config '{"scope_discipline": true}' > /root/bench/ab/scope-discipline.log 2>&1
python3 -m bench.ab --name review-scaled --tasks $T --config '{"review_scaled": true}' > /root/bench/ab/review-scaled.log 2>&1
python3 -m bench.ab --name thinking-full --tasks $T --config '{"thinking": {"mode": "fixed"}}' > /root/bench/ab/thinking-full.log 2>&1
python3 -m bench.ab --name edit-fuzzy --tasks $T --config '{"edit_fuzzy": true}' > /root/bench/ab/edit-fuzzy.log 2>&1
