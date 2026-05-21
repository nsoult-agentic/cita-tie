#!/bin/bash
# Analyze [METRICS] log lines from cita-tie container logs.
# Usage: docker logs cita-tie 2>&1 | ./analyze-metrics.sh
#   or:  ./analyze-metrics.sh < logfile.txt
#
# Outputs: result distribution, rate-limits/hour, avg cycle duration,
#          cycles before first rate-limit per run.

set -euo pipefail

echo "=== CITA-TIE EXPERIMENT ANALYSIS ==="
echo ""

# Extract METRICS lines
metrics=$(grep '\[METRICS\]' || true)

if [ -z "$metrics" ]; then
    echo "No [METRICS] lines found in input."
    exit 1
fi

total=$(echo "$metrics" | wc -l)
echo "Total cycles logged: $total"
echo ""

# Result distribution
echo "--- Result Distribution ---"
echo "$metrics" | grep -oP 'result=\K[A-Z_]+' | sort | uniq -c | sort -rn
echo ""

# Rate limits per hour
rl_count=$(echo "$metrics" | grep -c 'result=RATE_LIMITED' || echo 0)
if [ "$total" -gt 0 ]; then
    first_ts=$(echo "$metrics" | head -1 | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')
    last_ts=$(echo "$metrics" | tail -1 | grep -oP '^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}')
    if [ -n "$first_ts" ] && [ -n "$last_ts" ]; then
        first_epoch=$(date -d "$first_ts" +%s 2>/dev/null || echo 0)
        last_epoch=$(date -d "$last_ts" +%s 2>/dev/null || echo 0)
        duration_hrs=$(echo "scale=2; ($last_epoch - $first_epoch) / 3600" | bc 2>/dev/null || echo 0)
        if [ "$(echo "$duration_hrs > 0" | bc 2>/dev/null)" = "1" ]; then
            rl_per_hr=$(echo "scale=2; $rl_count / $duration_hrs" | bc)
            echo "--- Rate Limits ---"
            echo "Total rate limits: $rl_count"
            echo "Time span: ${duration_hrs}h"
            echo "Rate limits/hour: $rl_per_hr"
        else
            echo "--- Rate Limits ---"
            echo "Total rate limits: $rl_count"
            echo "Time span too short to compute per-hour rate"
        fi
    fi
fi
echo ""

# Average cycle duration
echo "--- Cycle Duration ---"
echo "$metrics" | grep -oP 'duration_s=\K[0-9.]+' | awk '{sum+=$1; count++} END {if(count>0) printf "Average: %.1fs (n=%d)\n", sum/count, count; else print "No data"}'
echo ""

# Cycles before first rate limit per run
echo "--- Cycles Before First Rate Limit (per run) ---"
echo "$metrics" | awk '
/result=RATE_LIMITED/ {
    if (!first_rl_in_run) {
        print "Run hit RL at cycle " cycle_in_run
        first_rl_in_run = 1
    }
}
/\[Attempt 1\// { cycle_in_run = 0; first_rl_in_run = 0 }
{ cycle_in_run++ }
'
echo ""
echo "=== END ANALYSIS ==="
