#!/usr/bin/env bash
# run_llm_demo.sh, faked: --run_mode stop stops the stand-in 7B daemon.
echo "run_llm_demo.sh $*" >> "$FAKE_LOG"
[[ " $* " == *" stop "* ]] && pkill -x test_llm
exit 0
