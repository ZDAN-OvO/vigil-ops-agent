#!/usr/bin/env bash
# F02 实验驱动：降低内存限额、核验持续 OOM 与业务受损，最后由操作者恢复规格。
# 仅用于专用实验集群。这不是 Agent 处置，也不代替 Agent 的回退与验证。
set -u

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/vigil-lab.yaml}"
KUBECTL="${KUBECTL:-kubectl}"
NS="${NS:-vigil-shop}"
MON="${MON:-vigil-monitoring}"
DEPLOY="${DEPLOY:-currencyservice}"
CONTAINER="${CONTAINER:-server}"
HEALTHY_LIMIT="${HEALTHY_LIMIT:-128Mi}"
HEALTHY_REQUEST="${HEALTHY_REQUEST:-64Mi}"
FAULT_LIMIT="${FAULT_LIMIT:-16Mi}"
FAULT_REQUEST="${FAULT_REQUEST:-8Mi}"
MIN_RESTARTS="${MIN_RESTARTS:-3}"
WAIT_SECONDS="${WAIT_SECONDS:-300}"
PF_PORT="${PF_PORT:-8080}"
PF_LOG="${PF_LOG:-${TMPDIR:-/tmp}/vigil-f02-port-forward.log}"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { printf '%s %s\n' "$(now)" "$*"; }

start_port_forward() {
  "$KUBECTL" port-forward -n "$NS" svc/frontend "${PF_PORT}:80" --address 127.0.0.1 >"$PF_LOG" 2>&1 &
  pf=$!
  for _ in $(seq 1 30); do
    [ "$(home_status)" = "200" ] || [ "$(home_status)" = "500" ] && return 0
    sleep 1
  done
  return 1
}
stop_port_forward() { [ -n "${pf:-}" ] && kill "$pf" 2>/dev/null || true; }

probe_value() {
  "$KUBECTL" exec -n "$MON" deploy/prometheus -- wget -qO- --post-data 'query=probe_success{job="frontend-probe",instance="http://frontend.vigil-shop.svc.cluster.local./"}' http://localhost:9090/api/v1/query 2>/dev/null \
    | python3 -I -c 'import sys,json; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")' 2>/dev/null || echo none
}
alert_state() {
  "$KUBECTL" exec -n "$MON" deploy/prometheus -- wget -qO- http://localhost:9090/api/v1/alerts 2>/dev/null \
    | python3 -I -c 'import sys,json; a=json.load(sys.stdin)["data"]["alerts"]; print(a[0]["state"] if a else "none")' 2>/dev/null || echo '?'
}
home_status() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PF_PORT}/" 2>/dev/null || echo 000; }
memory_limit() { "$KUBECTL" -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.spec.template.spec.containers[0].resources.limits.memory}'; }
revision() { "$KUBECTL" -n "$NS" get deploy "$DEPLOY" -o jsonpath='{.metadata.annotations.deployment\.kubernetes\.io/revision}'; }
pod_line() {
  "$KUBECTL" -n "$NS" get pods -l "app=$DEPLOY" -o jsonpath='{range .items[*]}{.metadata.name}{" restarts="}{.status.containerStatuses[0].restartCount}{" ready="}{.status.containerStatuses[0].ready}{" last="}{.status.containerStatuses[0].lastState.terminated.reason}{"/"}{.status.containerStatuses[0].lastState.terminated.exitCode}{" state="}{.status.containerStatuses[0].state.waiting.reason}{"\n"}{end}'
}
restarts() {
  "$KUBECTL" -n "$NS" get pods -l "app=$DEPLOY" -o jsonpath='{.items[0].status.containerStatuses[0].restartCount}' 2>/dev/null || echo 0
}
last_reason() {
  "$KUBECTL" -n "$NS" get pods -l "app=$DEPLOY" -o jsonpath='{.items[0].status.containerStatuses[0].lastState.terminated.reason}' 2>/dev/null || echo none
}

inject() {
  [ "$(memory_limit)" = "$HEALTHY_LIMIT" ] || { say "FAIL 当前限额 $(memory_limit) 不是 $HEALTHY_LIMIT"; exit 1; }
  [ "$(probe_value)" = "1" ] || { say "FAIL 探针不是 1"; exit 1; }
  [ "$(alert_state)" = "none" ] || { say "FAIL 有活动告警"; exit 1; }
  say "BASELINE revision=$(revision) limit=$(memory_limit) probe=$(probe_value) alerts=$(alert_state) restarts=$(restarts)"
  say "INJECT patch limit=$FAULT_LIMIT request=$FAULT_REQUEST"
  "$KUBECTL" -n "$NS" patch deploy/"$DEPLOY" --type=strategic -p \
    '{"spec":{"template":{"spec":{"containers":[{"name":"'"$CONTAINER"'","resources":{"limits":{"memory":"'"$FAULT_LIMIT"'"},"requests":{"memory":"'"$FAULT_REQUEST"'"}}}]}}}}' >/dev/null || exit 1
  # 缩到 0 再拉起，避免滚动更新在未就绪时保留旧模板 Pod、掩盖故障规格。
  "$KUBECTL" -n "$NS" scale deploy/"$DEPLOY" --replicas=0 >/dev/null || exit 1
  "$KUBECTL" -n "$NS" wait --for=delete pod -l "app=$DEPLOY" --timeout=60s >/dev/null 2>&1 || true
  "$KUBECTL" -n "$NS" scale deploy/"$DEPLOY" --replicas=1 >/dev/null || exit 1
  say "INJECTED revision=$(revision) limit=$(memory_limit)"
}

verify() {
  start_port_forward || { say "FAIL 前端不可访问"; exit 1; }
  deadline=$(( $(date +%s) + WAIT_SECONDS ))
  ok_oom=0; ok_business=0; ok_alert=0
  while [ "$(date +%s)" -lt "$deadline" ]; do
    r=$(restarts); reason=$(last_reason); home=$(home_status); probe=$(probe_value); alert=$(alert_state)
    say "OBSERVE restarts=$r last=$reason home=$home probe=$probe alert=$alert | $(pod_line)"
    [ "${r:-0}" -ge "$MIN_RESTARTS" ] && [ "$reason" = "OOMKilled" ] && ok_oom=1
    [ "$home" = "500" ] && [ "$probe" = "0" ] && ok_business=1
    [ "$alert" = "firing" ] && ok_alert=1
    if [ "$ok_oom" = 1 ] && [ "$ok_business" = 1 ] && [ "$ok_alert" = 1 ]; then
      say "VERIFIED sustained_oom=1 business_failure=1 alert_firing=1 revision=$(revision) limit=$(memory_limit)"
      stop_port_forward
      exit 0
    fi
    sleep 10
  done
  say "FAIL 未同时满足持续 OOM、业务受损与告警：oom=$ok_oom business=$ok_business alert=$ok_alert"
  stop_port_forward
  exit 1
}

restore() {
  say "RESTORE start revision=$(revision) limit=$(memory_limit) restarts=$(restarts)"
  "$KUBECTL" -n "$NS" patch deploy/"$DEPLOY" --type=strategic -p \
    '{"spec":{"template":{"spec":{"containers":[{"name":"'"$CONTAINER"'","resources":{"limits":{"memory":"'"$HEALTHY_LIMIT"'"},"requests":{"memory":"'"$HEALTHY_REQUEST"'"}}}]}}}}' >/dev/null || exit 1
  "$KUBECTL" -n "$NS" rollout status deploy/"$DEPLOY" --timeout=180s >/dev/null || { say "FAIL 恢复滚动未完成"; exit 1; }
  start_port_forward || { say "FAIL 前端不可访问"; exit 1; }
  deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    home=$(home_status); probe=$(probe_value); alert=$(alert_state)
    say "RECOVER home=$home probe=$probe alert=$alert restarts=$(restarts)"
    if [ "$home" = "200" ] && [ "$probe" = "1" ] && [ "$alert" = "none" ]; then
      say "RESTORED revision=$(revision) limit=$(memory_limit) restarts=$(restarts) | $(pod_line)"
      stop_port_forward
      exit 0
    fi
    sleep 10
  done
  say "FAIL 恢复窗口内未回到基线"
  stop_port_forward
  exit 1
}

trap stop_port_forward EXIT
case "${1:-}" in
  inject) inject ;;
  verify) verify ;;
  restore) restore ;;
  *) echo "用法：$0 {inject|verify|restore}" >&2; exit 2 ;;
esac
