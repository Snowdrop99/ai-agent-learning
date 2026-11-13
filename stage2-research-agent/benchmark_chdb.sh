#!/usr/bin/env bash

set -Eeuo pipefail

BASE_URL="${BASE_URL:-http://10.191.193.32:38080}"
CHDB_LIMIT="${CHDB_LIMIT:-700000}"
CHDB_START_TIME="${CHDB_START_TIME:-0}"
CHDB_END_TIME="${CHDB_END_TIME:-4102444800}"
API_TIMEOUT="${API_TIMEOUT:-120}"
WAIT_SECONDS="${WAIT_SECONDS:-3600}"
WRK_URL="${WRK_URL:-http://127.0.0.1/}"
WRK_THREADS="${WRK_THREADS:-2}"
WRK_CONNECTIONS="${WRK_CONNECTIONS:-10}"
API_USER="${API_USER:-}"
API_PASSWORD="${API_PASSWORD:-}"
OUTPUT_DIR="${OUTPUT_DIR:-$PWD/chdb_query_$(date '+%Y%m%d_%H%M%S')}"

DEFAULT_PROGRAMS=(
  tcp_tracer tcp_connlat tcp_drop tcp_err tcp_retrans tcp_life tcp_rtt tcp_win
  tcp_reset tcp_ttfb trace_sock
)

usage() {
  cat <<'EOF'
Usage:
  bash ./benchmark_chdb.sh [program_name ...]

If no program names are provided, query all programs in the default list.
Prints and saves a report containing each program's count and request duration.
Response data is used only temporarily to extract the count and is not saved.
Runs a bounded wrk load against the local Nginx while waiting for data collection.

Optional environment:
  BASE_URL          eBPF agent API base URL
                    (default: http://10.191.193.32:38080)
  CHDB_LIMIT        Maximum rows per query (default: 700000)
  CHDB_START_TIME   Start time as Unix seconds (default: 0)
  CHDB_END_TIME     End time as Unix seconds (default: 4102444800)
  API_TIMEOUT       curl timeout in seconds (default: 120)
  WAIT_SECONDS      Seconds to wait before querying (default: 3600)
  WRK_URL           Local Nginx URL (default: http://127.0.0.1/)
  WRK_THREADS       wrk worker threads, 1-4 (default: 2)
  WRK_CONNECTIONS   wrk connections, 1-32 (default: 10)
  API_USER          Basic-auth username (must be set with API_PASSWORD)
  API_PASSWORD      Basic-auth password
  OUTPUT_DIR        Directory for the query report
                    (default: ./chdb_query_<timestamp>)

Requirements: bash, curl, jq, wrk.
EOF
}

log() {
  printf '[%s] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"
}

fail() {
  log "ERROR: $*"
  exit 1
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

for command_name in curl jq date wrk; do
  command -v "$command_name" >/dev/null 2>&1 || fail "缺少依赖命令：$command_name"
done

[[ "$CHDB_LIMIT" =~ ^[0-9]+$ ]] || fail "CHDB_LIMIT 必须是非负整数"
[[ "$CHDB_START_TIME" =~ ^[0-9]+$ ]] || fail "CHDB_START_TIME 必须是非负整数"
[[ "$CHDB_END_TIME" =~ ^[0-9]+$ ]] || fail "CHDB_END_TIME 必须是非负整数"
[[ "$CHDB_START_TIME" -le "$CHDB_END_TIME" ]] || fail "CHDB_START_TIME 不能大于 CHDB_END_TIME"
[[ "$API_TIMEOUT" =~ ^[1-9][0-9]*$ ]] || fail "API_TIMEOUT 必须是正整数"
[[ "$WAIT_SECONDS" =~ ^[0-9]+$ ]] || fail "WAIT_SECONDS 必须是非负整数"
[[ "$WRK_THREADS" =~ ^[1-4]$ ]] || fail "WRK_THREADS 必须是 1 到 4 之间的整数"
[[ "$WRK_CONNECTIONS" =~ ^([1-9]|[12][0-9]|3[0-2])$ ]] ||
  fail "WRK_CONNECTIONS 必须是 1 到 32 之间的整数"
[[ "$WRK_URL" =~ ^https?://(localhost|127\.0\.0\.1|\[::1\])(:[0-9]{1,5})?(/[^#]*)?$ ]] ||
  fail "WRK_URL 必须是 localhost、127.0.0.1 或 [::1] 上的 HTTP(S) URL"
[[ -n "$API_USER" && -n "$API_PASSWORD" || -z "$API_USER" && -z "$API_PASSWORD" ]] ||
  fail "API_USER 和 API_PASSWORD 必须同时设置"

if (($# > 0)); then
  PROGRAMS=("$@")
else
  PROGRAMS=("${DEFAULT_PROGRAMS[@]}")
fi

for program in "${PROGRAMS[@]}"; do
  [[ "$program" =~ ^[A-Za-z0-9_]+$ ]] || fail "无效的功能名：$program"
done

BASE_URL="${BASE_URL%/}"
CHDB_URL="${BASE_URL}/api/v1/chdb"
mkdir -p -- "$OUTPUT_DIR" || fail "无法创建输出目录：$OUTPUT_DIR"

auth_args=()
if [[ -n "$API_USER" ]]; then
  auth_args=(-u "${API_USER}:${API_PASSWORD}")
fi

timestamp="$(date '+%Y%m%d_%H%M%S')"
query_failures=0
total_count=0
program_counts=()
program_durations=()
program_statuses=()
report_file="${OUTPUT_DIR%/}/chdb_report_${timestamp}.txt"
request_file=""
response_file=""
wrk_output_file="$(mktemp)"
wrk_pid=""
wrk_exit_status=0

cleanup() {
  if [[ -n "$wrk_pid" ]] && kill -0 "$wrk_pid" 2>/dev/null; then
    kill "$wrk_pid" 2>/dev/null || true
    wait "$wrk_pid" 2>/dev/null || true
  fi
  [[ -z "$request_file" ]] || rm -f -- "$request_file"
  [[ -z "$response_file" ]] || rm -f -- "$response_file"
  [[ -z "$wrk_output_file" ]] || rm -f -- "$wrk_output_file"
}
trap cleanup EXIT

wrk_duration="$WAIT_SECONDS"
if ((wrk_duration == 0)); then
  wrk_duration=1
fi

log "启动本机 Nginx 压测：$WRK_URL，${WRK_THREADS} 线程，${WRK_CONNECTIONS} 连接，持续 ${wrk_duration} 秒"
wrk -t"$WRK_THREADS" -c"$WRK_CONNECTIONS" -d"${wrk_duration}s" --latency "$WRK_URL" \
  >"$wrk_output_file" 2>&1 &
wrk_pid=$!

log "等待 ${WAIT_SECONDS} 秒后开始查询 ChDB，API：$CHDB_URL"
sleep "$WAIT_SECONDS"
if wait "$wrk_pid"; then
  wrk_exit_status=0
else
  wrk_exit_status=$?
fi
wrk_pid=""

log "开始查询 ChDB"
query_started=$SECONDS
for index in "${!PROGRAMS[@]}"; do
  program="${PROGRAMS[$index]}"
  program_counts[$index]="-"
  program_durations[$index]="-"
  program_statuses[$index]="CURL_ERROR"
  request_file="$(mktemp)"
  response_file="$(mktemp)"
  jq -n \
    --arg program_name "$program" \
    --argjson start_time "$CHDB_START_TIME" \
    --argjson end_time "$CHDB_END_TIME" \
    --argjson limit "$CHDB_LIMIT" \
    '{program_name: $program_name, start_time: $start_time, end_time: $end_time, limit: $limit, filters: {}}' \
    >"$request_file"

  curl_result=""
  if curl_result="$(curl -sS --connect-timeout 10 --max-time "$API_TIMEOUT" \
    "${auth_args[@]}" -H 'Content-Type: application/json' \
    --data-binary "@${request_file}" -o "$response_file" \
    -w '%{http_code} %{time_total}' -X POST "$CHDB_URL")"; then
    read -r http_status request_seconds <<<"$curl_result"
    if [[ "$request_seconds" =~ ^[0-9]+([.][0-9]+)?$ ]]; then
      program_durations[$index]="$request_seconds"
    fi

    if [[ "$http_status" == "200" ]]; then
      if jq -e '.success == true and (.count | type == "number" and . >= 0 and floor == .)' \
        "$response_file" >/dev/null 2>&1; then
        count="$(jq -er '.count | floor' "$response_file")"
        total_count=$((total_count + count))
        program_counts[$index]="$count"
        program_statuses[$index]="OK"
      else
        program_statuses[$index]="INVALID_RESPONSE"
      fi
    else
      program_statuses[$index]="HTTP_${http_status}"
    fi
  fi

  rm -f -- "$request_file" "$response_file"
  request_file=""
  response_file=""

  if [[ "${program_statuses[$index]}" != "OK" ]]; then
    query_failures=$((query_failures + 1))
  fi
done

query_elapsed=$((SECONDS - query_started))
{
  printf 'ChDB 查询报告\n'
  printf '生成时间：%s\n' "$(date '+%Y-%m-%d %H:%M:%S')"
  printf 'API：%s\n' "$CHDB_URL"
  printf 'wrk 目标：%s\n' "$WRK_URL"
  printf 'wrk 配置：%s 线程，%s 连接，持续 %s 秒\n' \
    "$WRK_THREADS" "$WRK_CONNECTIONS" "$wrk_duration"
  printf 'wrk 退出状态：%s\n' "$wrk_exit_status"
  printf '\nwrk 性能输出：\n'
  cat "$wrk_output_file"
  printf '\nChDB 统计：\n'
  printf '%-24s | %14s | %14s | %-16s\n' \
    '功能' '数据量(count)' '请求耗时(秒)' '状态'
  printf '%s\n' '-------------------------+----------------+----------------+------------------'
  for index in "${!PROGRAMS[@]}"; do
    printf '%-24s | %14s | %14s | %-16s\n' \
      "${PROGRAMS[$index]}" "${program_counts[$index]}" \
      "${program_durations[$index]}" "${program_statuses[$index]}"
  done
  printf '数据量总和：%s 条（仅统计成功查询）\n' "$total_count"
  printf '查询耗时：%s 秒\n' "$query_elapsed"
  printf '成功：%s 项；失败：%s 项\n' \
    "$(( ${#PROGRAMS[@]} - query_failures ))" "$query_failures"
} >"$report_file"

cat "$report_file"
log "报告已保存至 $report_file"
if ((query_failures > 0)); then
  exit 1
fi
if ((wrk_exit_status != 0)); then
  exit 1
fi
