#!/usr/bin/env bash
# F03 实验驱动：把 Redis 访问路径切到带固定延迟的实验代理，核验业务延迟，
# 并在调查判定后由操作者清理。仅用于专用实验集群。
set -u

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/vigil-lab.yaml}"
KUBECTL="${KUBECTL:-kubectl}"
NS="${NS:-vigil-shop}"
MON="${MON:-vigil-monitoring}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PF_PORT="${PF_PORT:-8080}"
PF_LOG="${PF_LOG:-${TMPDIR:-/tmp}/vigil-f03-port-forward.log}"
THRESHOLD_MS="${THRESHOLD_MS:-300}"
# shellcheck source=faults/lib/result.sh
source "$DIR/../lib/result.sh"
WAIT_SECONDS="${WAIT_SECONDS:-300}"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { printf '%s %s\n' "$(now)" "$*"; }

metric() {
  "$KUBECTL" exec -n "$MON" deploy/prometheus -- wget -qO- \
    --post-data "query=$1" http://localhost:9090/api/v1/query 2>/dev/null \
    | python3 -I -c 'import sys,json; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")' 2>/dev/null || echo none
}
probe_duration() { metric 'avg_over_time(probe_duration_seconds{job="frontend-probe"}[2m])'; }
probe_success() { metric 'probe_success{job="frontend-probe",instance="http://frontend.vigil-shop.svc.cluster.local./"}'; }
selector() { "$KUBECTL" -n "$NS" get svc redis-cart -o jsonpath='{.spec.selector.app}'; }
cartservice_restarts() { "$KUBECTL" -n "$NS" get pods -l app=cartservice -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0; }
redis_restarts() { "$KUBECTL" -n "$NS" get pods -l app=redis-cart -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0; }
home_time() { curl -s -o /dev/null -w '%{time_total}' --max-time 10 "http://127.0.0.1:${PF_PORT}/" 2>/dev/null || echo 0; }
cart_time() { curl -s -o /dev/null -w '%{http_code}:%{time_total}' --max-time 10 "http://127.0.0.1:${PF_PORT}/cart" 2>/dev/null || echo 000:0; }

start_port_forward() {
  "$KUBECTL" -n "$NS" port-forward svc/frontend "${PF_PORT}:80" --address 127.0.0.1 >"$PF_LOG" 2>&1 &
  pf=$!
  for _ in $(seq 1 30); do [ "$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 "http://127.0.0.1:${PF_PORT}/" 2>/dev/null)" = "200" ] && return 0; sleep 1; done
  return 1
}
stop_port_forward() { [ -n "${pf:-}" ] && kill "$pf" 2>/dev/null || true; }

preflight() {
  [ "$(selector)" = "redis-cart" ] || { say "FAIL redis-cart 选择器已是 $(selector)"; result preflight fail "reason=selector" "selector=$(selector)"; exit 1; }
  "$KUBECTL" -n "$NS" get deploy redis-access-proxy >/dev/null 2>&1 && { say "FAIL 代理已存在"; result preflight fail reason=proxy_present; exit 1; }
  start_port_forward || { say "FAIL 前端不可访问"; result preflight fail reason=frontend; exit 1; }
  # 2 分钟均值会带上别的场景刚恢复时的高延迟，等它落回基线再开始，最多 3 分钟。
  deadline=$(( $(date +%s) + 180 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    avg=$(probe_duration)
    python3 -I -c "import sys; sys.exit(0 if float('$avg')*1000 < 100 else 1)" 2>/dev/null && break
    say "SETTLE probe_duration_2m=$avg 等待回落到基线"
    sleep 15
  done
  avg=$(probe_duration); ok=$(probe_success); cart=$(cart_time)
  stop_port_forward
  python3 -I -c "import sys; sys.exit(0 if '$ok'=='1' and '$cart'.startswith('200:') and float('$avg')*1000 < 100 else 1)" \
    || { say "FAIL 基线不健康"; result preflight fail "reason=baseline" "probe=$ok" "cart=$cart" "latency_ms=$(python3 -I -c "print(round(float('$avg')*1000))" 2>/dev/null || echo unknown)"; exit 1; }
  say "PREFLIGHT probe=$(probe_success) selector=$(selector) latency_ms=$(python3 -I -c "print(round(float('$avg')*1000))")"
  result preflight ok "selector=redis-cart" "probe=1" "cart=$cart" "latency_ms=$(python3 -I -c "print(round(float('$avg')*1000))")"
}

inject() {
  [ "$(selector)" = "redis-cart" ] || { say "FAIL redis-cart 选择器已是 $(selector)"; exit 1; }
  "$KUBECTL" -n "$NS" get deploy redis-access-proxy >/dev/null 2>&1 && { say "FAIL 代理已存在"; exit 1; }
  say "BASELINE probe_duration=$(probe_duration) probe=$(probe_success) selector=$(selector)"
  "$KUBECTL" apply -f "$DIR/manifests.yaml" >/dev/null || exit 1
  "$KUBECTL" -n "$NS" rollout status deploy/redis-access-proxy --timeout=120s >/dev/null || exit 1
  "$KUBECTL" -n "$NS" patch svc redis-cart -p '{"spec":{"selector":{"app":"redis-access-proxy"}}}' >/dev/null || exit 1
  # 既有 Redis 长连接不会随 Service 选择器迁移，重启 cartservice 让新连接经过代理。
  "$KUBECTL" -n "$NS" delete pod -l app=cartservice >/dev/null 2>&1 || true
  "$KUBECTL" -n "$NS" rollout status deploy/cartservice --timeout=120s >/dev/null || exit 1
  say "INJECTED selector=$(selector) proxy=$(kubectl -n "$NS" get pods -l app=redis-access-proxy -o jsonpath='{.items[0].metadata.name}')"
  "$KUBECTL" -n "$NS" get endpoints redis-cart
  result inject ok "selector=$(selector)" "proxy=present"
}

verify() {
  start_port_forward || { say "FAIL 前端不可访问"; exit 1; }
  deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    avg=$(probe_duration); ok=$(probe_success); home=$(home_time); cart=$(cart_time)
    say "OBSERVE probe_duration_2m=$avg probe_success=$ok home_s=$home cart=$cart cartservice_restarts=$(cartservice_restarts) redis_restarts=$(redis_restarts)"
    if python3 -I -c "import sys; sys.exit(0 if float('$avg')*1000 >= $THRESHOLD_MS and '$ok'=='1' else 1)" 2>/dev/null; then
      say "VERIFIED latency_ms=$(python3 -I -c "print(round(float('$avg')*1000))") probe_success=1 selector=$(selector) cleanup_pending=1"
      stop_port_forward
      result verify ok "latency_ms=$(python3 -I -c "print(round(float('$avg')*1000))")" "probe=$ok" "selector=$(selector)" "home_s=$home"
      exit 0
    fi
    sleep 10
  done
  say "FAIL 延迟未达到 ${THRESHOLD_MS}ms 或探针失败"
  stop_port_forward
  result verify fail "reason=latency" "latency_ms=${avg:-unknown}" "probe=${ok:-unknown}"
  exit 1
}

# 只读检查：选择器已回原样、延迟回到基线；不改任何东西。
recovered() {
  start_port_forward || { result recovered fail reason=frontend; exit 1; }
  avg=$(probe_duration); ok=$(probe_success); cart=$(cart_time)
  stop_port_forward
  selector=$(selector)
  say "RECOVERED_CHECK selector=$selector probe=$ok probe_duration_2m=$avg cart=$cart"
  if [ "$selector" != "redis-cart" ] || [ "$ok" != "1" ] || ! echo "$cart" | grep -q '^200:' \
    || ! python3 -I -c "import sys; sys.exit(0 if float('$avg')*1000 < 100 else 1)" 2>/dev/null; then
    result recovered fail "reason=not_recovered" "selector=$selector" "probe=$ok" "cart=$cart" "latency_ms=$(python3 -I -c "print(round(float('$avg')*1000))" 2>/dev/null || echo unknown)"
    exit 1
  fi
  result recovered ok "selector=$selector" "probe=$ok" "cart=$cart" "latency_ms=$(python3 -I -c "print(round(float('$avg')*1000))")"
}

cleanup() {
  say "CLEANUP start selector=$(selector) probe_duration=$(probe_duration)"
  "$KUBECTL" -n "$NS" patch svc redis-cart -p '{"spec":{"selector":{"app":"redis-cart"}}}' >/dev/null || exit 1
  "$KUBECTL" delete -f "$DIR/manifests.yaml" --ignore-not-found >/dev/null || exit 1
  start_port_forward || { say "FAIL 前端不可访问"; exit 1; }
  deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    avg=$(probe_duration); ok=$(probe_success); home=$(home_time); cart=$(cart_time)
    say "RECOVER probe_duration_2m=$avg probe_success=$ok home_s=$home cart=$cart"
    if python3 -I -c "import sys; sys.exit(0 if float('$avg')*1000 < 100 and '$ok'=='1' and '$cart'.startswith('200:') else 1)" 2>/dev/null; then
      say "CLEANED selector=$(selector) probe_duration_ms=$(python3 -I -c "print(round(float('$avg')*1000))") redis_restarts=$(redis_restarts)"
      stop_port_forward
      result cleanup ok "selector=redis-cart" "probe=$ok" "cart=$cart" "latency_ms=$(python3 -I -c "print(round(float('$avg')*1000))")"
      exit 0
    fi
    sleep 10
  done
  say "FAIL 清理后未回到基线"
  stop_port_forward
  result cleanup fail "reason=not_cleaned" "probe=${ok:-unknown}" "cart=${cart:-unknown}"
  exit 1
}

trap stop_port_forward EXIT
case "${1:-}" in
  preflight) preflight ;;
  recovered) recovered ;;
  inject) inject ;;
  verify) verify ;;
  cleanup) cleanup ;;
  *) echo "用法：$0 {preflight|recovered|inject|verify|cleanup}" >&2; exit 2 ;;
esac
