#!/bin/bash
# GLM REAP50 IQ3_M on the author's fork, already running as srv-glm with <|user|> as end-of-turn.
# Quality eval, then Minecraft (bench proxy parses GLM tool-call text), then restore llama-server.
S=/root/bench/compare/status.txt; L=glm-reap50-iq3m
echo "glm-reap fixed run start (eot fix + tool adapter) $(date +%T)" >> $S
python3 /root/bench/quality/eval.py --url http://127.0.0.1:8033 --label $L >> /root/bench/compare/quality.jsonl 2>> /root/bench/compare/log-$L.txt
echo "glm-reap eval: $(tail -1 /root/bench/compare/quality.jsonl) $(date +%T)" >> $S
cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:glm-reap --batch mc-glm-reap50-iq3m-vision-1 --serial \
  > /root/bench/results/mc-glm-reap50-iq3m-vision-1.log 2>&1
echo "mc glm-reap done $(date +%T)" >> $S
systemctl stop srv-glm; sleep 10
systemctl start llama-server
echo "glm-reap fixed run done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
