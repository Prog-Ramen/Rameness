#!/bin/bash
# After the TnzGit recipe's Minecraft run: Swift 1.5 (ukisai, Qwen3.8-27B fine-tune) Q4_K_M on llama.cpp master + MTP,
# same settings as the Unsloth UD-Q4_K_M run (CMP only, 262k, q8 KV, xhigh, vision), then restore llama-server.
S=/root/bench/compare/status.txt; B=/root/models/llama.cpp/llama-mtp-fmad/build/bin; D=/root/models/swift15
# (started directly on the user's go-ahead after the SSD install)
systemctl stop llama-server; sleep 10
start() {  # extra speculative args...
  systemctl reset-failed srv-swift 2>/dev/null
  systemd-run --unit=srv-swift -p WorkingDirectory=/root/models -p LimitMEMLOCK=infinity -E LD_LIBRARY_PATH=$B -E CUDA_VISIBLE_DEVICES=0 \
    $B/llama-server --host 127.0.0.1 --port 8033 -np 1 -fa on --temp 1.0 --top-p 0.95 --top-k 20 --min-p 0 \
    --presence-penalty 0 --repeat-penalty 1.0 --chat-template-kwargs '{"reasoning_effort":"xhigh"}' \
    -m $D/Swift-1.5-Qwen3.8-27B-Q4_K_M.gguf --mmproj $D/mmproj-Swift-1.5-Qwen3.8-27B-F16.gguf -ngl 99 \
    -c 262144 -ctk q8_0 -ctv q8_0 --spec-type draft-mtp --spec-draft-n-max 3 "$@"
  timeout 1200 bash -c 'until curl -s -m3 127.0.0.1:8033/health | grep -q ok || ! systemctl is-active -q srv-swift; do sleep 5; done'
  systemctl is-active -q srv-swift
}
if start; then echo "swift15: built-in MTP head $(date +%T)" >> $S
elif systemctl stop srv-swift 2>/dev/null; start -md /root/models/unsloth/q27/MTP/mtp-Qwen3.8-27B-Q4_0.gguf --spec-draft-ngl 99; then
  echo "swift15: Unsloth base MTP head (built-in did not load) $(date +%T)" >> $S
else echo "swift15 server failed: $(journalctl -u srv-swift --since '-30min' --no-pager -o cat | grep -E ' E ' | head -2 | tr '\n' ' ' | cut -c1-250) $(date +%T)" >> $S
  systemctl stop srv-swift; systemctl start llama-server; exit 1; fi
timeout 600 python3 /root/exl3/smoke_any.py http://127.0.0.1:8033 x >> $S 2>&1
echo "mc swift15 start $(date +%T)" >> $S
cd /root/projects/Rameness && python3 -m bench.minecraft.run --runs rameness:swift15 --batch mc-swift15-q4km-vision-1 --serial \
  > /root/bench/results/mc-swift15-q4km-vision-1.log 2>&1
echo "mc swift15 done $(date +%T)" >> $S
systemctl stop srv-swift; sleep 10
systemctl start llama-server
echo "swift15 finished; service $(systemctl is-active llama-server) $(date +%T)" >> $S
