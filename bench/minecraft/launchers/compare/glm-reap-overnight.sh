#!/bin/bash
# Overnight: GLM-5.3-Flash REAP50 IQ3_M on the author's llama.cpp fork (<|user|> as end-of-turn), quality eval (resumes),
# then Minecraft (bench proxy parses GLM tool calls and adds media markers so images work). Restores llama-server at the end.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-reap50/build/bin; D=/root/models/glm-reap; L=glm-reap50-iq3m
echo "glm-reap overnight start $(date +%T)" >> $S
systemctl stop llama-server; sleep 10
systemctl reset-failed srv-glm 2>/dev/null
systemd-run --unit=srv-glm -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B \
  $B/llama-server --host 127.0.0.1 --port 8033 -np 1 --jinja -m $D/GLM-5.3-Flash-REAP50-IQ3_M.gguf \
  --mmproj $D/mmproj-GLM-5.3-Flash-REAP50-F16.gguf --fit on --fit-target 1536,1024 --fit-ctx 131072 -c 131072 -ctk q8_0 -ctv q8_0 -fa on \
  --override-kv tokenizer.ggml.eot_token_id=int:154827 --chat-template-file $D/tmpl/official.jinja
if timeout 1500 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-glm; do sleep 5; done' \
   && systemctl is-active -q srv-glm; then
  python3 /root/bench/quality/eval.py --url http://127.0.0.1:8033 --label $L >> /root/bench/compare/quality.jsonl 2>> /root/bench/compare/log-$L.txt
  echo "glm-reap eval: $(tail -1 /root/bench/compare/quality.jsonl) $(date +%T)" >> $S
  cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:glm-reap --batch mc-glm-reap50-iq3m-vision-1 --serial \
    > /root/bench/results/mc-glm-reap50-iq3m-vision-1.log 2>&1
  echo "mc glm-reap done $(date +%T)" >> $S
else echo "glm-reap overnight: server failed $(date +%T)" >> $S; fi
systemctl stop srv-glm; sleep 10
systemctl start llama-server
echo "glm-reap overnight done; service $(systemctl is-active llama-server) $(date +%T)" >> $S
