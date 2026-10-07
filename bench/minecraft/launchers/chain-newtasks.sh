#!/bin/bash
# Non-Minecraft evaluation: bugfix + research, Rameness vs Claude Code on Qwen3.8-27B, 2 runs each, interleaved.
cd /root/projects/Rameness
for task in bugfix research; do
  for i in 1 2; do
    for h in rameness claude-code; do
      python3 -m bench.minecraft.run --task $task --runs $h:qwen --timeout 2700 --batch nt-$task-$i >> /root/bench/chain-newtasks.log 2>&1
    done
  done
done
