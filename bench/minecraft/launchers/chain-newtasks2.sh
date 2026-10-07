#!/bin/bash
# After the bugfix/research comparison: the data task, Rameness vs Claude Code on Qwen, 2 runs each, interleaved.
cd /root/projects/Rameness
while pgrep -f "chain-newtasks.sh" >/dev/null; do sleep 60; done
for i in 1 2; do
  for h in rameness claude-code; do
    python3 -m bench.minecraft.run --task data --runs $h:qwen --timeout 2700 --batch nt-data-$i >> /root/bench/chain-newtasks.log 2>&1
  done
done
