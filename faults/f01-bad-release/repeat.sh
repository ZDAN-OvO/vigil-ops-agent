#!/usr/bin/env bash
# F01 实验驱动：真实注入坏发布、核验业务受损与告警、执行回退并验证复位。
# 仅用于专用实验集群。运行前确认 KUBECONFIG 指向 vigil-lab。
#
# 动词语义与其它场景一致（见 faults/README.md）：
#   preflight  健康基线与所有权检查，不改变集群
#   inject     注入坏发布并等到业务受损、探针归零、告警 firing
#   verify     确认故障仍然成立
#   cleanup    回退到健康 revision 并等到恢复窗口成立
#   all        依次执行上面四步；操作者演练用
set -u

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/vigil-lab.yaml}"
KUBECTL="${KUBECTL:-kubectl}"
NS="${NS:-vigil-shop}"
MON="${MON:-vigil-monitoring}"
BAD="${BAD:-vigil/productcatalogservice:canary-20261104}"
HEALTHY="${HEALTHY:-gcr.io/google-samples/microservices-demo/productcatalogservice@sha256:1d51f9e5aa923ff9c338b1b155b39da737306c0ebc230e62ec6271f2b0cb7bef}"
PF_PORT="${PF_PORT:-8080}"
PF_LOG="${PF_LOG:-${TMPDIR:-/tmp}/vigil-f01-port-forward.log}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=faults/lib/result.sh
source "$DIR/../lib/result.sh"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
epoch() { date +%s; }
say() { printf '%s %s\n' "$(now)" "$*"; }

probe_value() {
  "$KUBECTL" exec -n "$MON" deploy/prometheus -- wget -qO- --post-data 'query=probe_success{job="frontend-probe",instance="http://frontend.vigil-shop.svc.cluster.local./"}' http://localhost:9090/api/v1/query 2>/dev/null \
    | python3 -I -c 'import sys,json; r=json.load(sys.stdin)["data"]["result"]; print(r[0]["value"][1] if r else "none")' 2>/dev/null || echo none
}
alert_state() {
  "$KUBECTL" exec -n "$MON" deploy/prometheus -- wget -qO- http://localhost:9090/api/v1/alerts 2>/dev/null \
    | python3 -I -c 'import sys,json; a=json.load(sys.stdin)["data"]["alerts"]; print(a[0]["state"] if a else "none")' 2>/dev/null || echo '?'
}
home_status() {
  curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PF_PORT}/" 2>/dev/null || echo 000
}
# 首页状态要按窗口等：转发刚建立时后端可能还没就绪，单次采样会误判。
wait_home() {
  local want=$1 seconds=$2 deadline
  deadline=$(( $(date +%s) + seconds ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ "$(home_status)" = "$want" ] && return 0
    sleep 1
  done
  return 1
}
deploy_image() {
  "$KUBECTL" get deploy productcatalogservice -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}'
}
bad_rs_replicas() {
  "$KUBECTL" get rs -n "$NS" -l app=productcatalogservice -o json 2>/dev/null \
    | BAD_IMAGE="$BAD" python3 -I -c 'import os,sys,json
bad = os.environ["BAD_IMAGE"]
for r in json.load(sys.stdin)["items"]:
    if r["spec"]["template"]["spec"]["containers"][0]["image"] == bad:
        print(r["spec"].get("replicas", 0)); break
else: print(0)' 2>/dev/null || echo 0
}
rollback_if_bad() {
  if [ "$(deploy_image)" = "$BAD" ]; then
    say "SAFETY rollback"
    "$KUBECTL" rollout undo deployment/productcatalogservice -n "$NS" >/dev/null 2>&1 || true
    "$KUBECTL" rollout status deployment/productcatalogservice -n "$NS" --timeout=180s >/dev/null 2>&1 || true
  fi
}
# 失败处理：只读动词（preflight/verify/recovered）不得改集群，恢复只交给 cleanup。
# 只有 inject 显式打开 FAIL_ROLLBACK，避免"检查失败顺手回退"把故障现场清掉。
fail() {
  say "FAIL: $*"
  stop_port_forward
  if [ "${FAIL_ROLLBACK:-0}" = "1" ]; then
    rollback_if_bad
  fi
  say "STATE image=$(deploy_image) probe=$(probe_value) alerts=$(alert_state)"
  result "$VERB" fail "reason=$1"
  exit 1
}

# 转发可用不等于业务健康：注入后首页就是 500。这里只等端口通，不把 500 当失败。
start_port_forward() {
  stop_port_forward
  : >"$PF_LOG"
  "$KUBECTL" port-forward -n "$NS" svc/frontend "${PF_PORT}:80" --address 127.0.0.1 >"$PF_LOG" 2>&1 &
  pf=$!
  for _ in $(seq 1 30); do
    kill -0 "$pf" 2>/dev/null || return 1
    grep -q "Forwarding from" "$PF_LOG" 2>/dev/null && return 0
    sleep 1
  done
  return 1
}
stop_port_forward() { [ -n "${pf:-}" ] && kill "$pf" 2>/dev/null || true; pf=""; }

preflight() {
  VERB=preflight
  say "PRE image=$(deploy_image) probe=$(probe_value) alerts=$(alert_state)"
  [ "$(deploy_image)" = "$HEALTHY" ] || fail "productcatalog 不在健康镜像"
  [ "$(probe_value)" = "1" ] || fail "probe_success 不是 1"
  [ "$(alert_state)" = "none" ] || fail "存在活动告警"
  [ "$(bad_rs_replicas)" = "0" ] || fail "坏 ReplicaSet 未缩到 0"
  start_port_forward || fail "前端不可访问"
  wait_home 200 60 || { stop_port_forward; fail "注入前首页不是 200"; }
  say "BASELINE image=$(deploy_image) probe=$(probe_value) alerts=$(alert_state) home=200"
  stop_port_forward
  result preflight ok "image=$HEALTHY" "probe=1" "alerts=none" "home=200"
}

inject() {
  VERB=inject
  FAIL_ROLLBACK=1
  probe_value >/dev/null
  [ "$(deploy_image)" = "$HEALTHY" ] || fail "注入前不是健康镜像"
  t0=$(epoch)
  say "INJECT start"
  "$KUBECTL" set image deployment/productcatalogservice -n "$NS" server="$BAD" >/dev/null
  "$KUBECTL" rollout status deployment/productcatalogservice -n "$NS" --timeout=180s >/dev/null || fail "坏发布未完成滚动"
  say "ROLLOUT_DONE elapsed=$(( $(epoch) - t0 ))s"
  newpod=""
  for _ in $(seq 1 60); do
    newpod=$("$KUBECTL" get pods -n "$NS" -l app=productcatalogservice --no-headers -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[0].image | awk -v img="$BAD" '$2==img{print $1; exit}')
    [ -n "$newpod" ] && break
    sleep 1
  done
  [ -n "$newpod" ] || fail "找不到坏发布的 Pod"
  newuid=$("$KUBECTL" get pod -n "$NS" "$newpod" -o jsonpath='{.metadata.uid}')
  say "NEW_POD $newpod uid=$newuid image=$BAD"

  start_port_forward || fail "前端不可访问"
  wait_home 500 90 || { stop_port_forward; fail "首页没有返回 500"; }
  t500=$(epoch)
  stop_port_forward
  say "HOME_500 elapsed=$((t500-t0))s"

  tp0=""
  for _ in $(seq 1 90); do
    if [ "$(probe_value)" = "0" ]; then tp0=$(epoch); break; fi
    sleep 1
  done
  [ -n "$tp0" ] || fail "probe_success 没有归零"
  say "PROBE_0 elapsed=$((tp0-t0))s"

  tfire=""
  for _ in $(seq 1 180); do
    if [ "$(alert_state)" = "firing" ]; then tfire=$(epoch); break; fi
    sleep 1
  done
  [ -n "$tfire" ] || fail "告警没有 firing"
  say "ALERT_FIRING elapsed=$((tfire-t0))s state=$(alert_state)"
  result inject ok "image=$BAD" "pod=$newpod" "probe=0" "alert=firing" "elapsed_s=$((tfire-t0))"
}

verify() {
  VERB=verify
  start_port_forward || fail "前端不可访问"
  wait_home 500 60
  code=$(home_status)
  stop_port_forward
  probe=$(probe_value)
  alert=$(alert_state)
  say "OBSERVE image=$(deploy_image) home=$code probe=$probe alert=$alert"
  if [ "$code" != "500" ] || [ "$probe" != "0" ] || [ "$alert" != "firing" ]; then
    fail "故障不成立"
  fi
  result verify ok "image=$(deploy_image)" "home=500" "probe=0" "alert=firing"
}

# 只读检查：业务是否已经恢复，不做任何修改；用于评估臂运行后的判定。
recovered() {
  VERB=recovered
  start_port_forward || { result recovered fail reason=frontend; exit 1; }
  wait_home 200 60
  code=$(home_status)
  stop_port_forward
  probe=$(probe_value)
  alert=$(alert_state)
  image=$(deploy_image)
  say "RECOVERED_CHECK image=$image home=$code probe=$probe alert=$alert"
  if [ "$image" != "$HEALTHY" ] || [ "$code" != "200" ] || [ "$probe" != "1" ] || [ "$alert" != "none" ]; then
    result recovered fail "reason=not_recovered" "image=$image" "home=$code" "probe=$probe" "alert=$alert"
    exit 1
  fi
  result recovered ok "image=$image" "home=200" "probe=1" "alerts=none"
}

cleanup() {
  VERB=cleanup
  trb=$(epoch)
  image=$(deploy_image)
  if [ "$image" = "$BAD" ]; then
    say "ROLLBACK start image=$image"
    "$KUBECTL" rollout undo deployment/productcatalogservice -n "$NS" >/dev/null
    "$KUBECTL" rollout status deployment/productcatalogservice -n "$NS" --timeout=180s >/dev/null || fail "健康 revision 未完成滚动"
    say "ROLLBACK_DONE elapsed=$(( $(epoch) - trb ))s image=$(deploy_image)"
  elif [ "$image" = "$HEALTHY" ]; then
    # 已经回退过（注入失败兜底、或操作者先复位）：再 undo 会把服务切回坏版本。
    say "ROLLBACK_SKIPPED image=$image 已是健康镜像"
  else
    fail "当前镜像既不是坏版本也不是健康版本：$image"
  fi
  healthy_pod=""
  for _ in $(seq 1 60); do
    healthy_pod=$("$KUBECTL" get pods -n "$NS" -l app=productcatalogservice --no-headers -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[0].image | awk -v img="$HEALTHY" '$2==img{print $1; exit}')
    [ -n "$healthy_pod" ] && break
    sleep 1
  done
  [ -n "$healthy_pod" ] || fail "找不到健康镜像的 Pod"
  healthy_uid=$("$KUBECTL" get pod -n "$NS" "$healthy_pod" -o jsonpath='{.metadata.uid}')
  say "HEALTHY_POD $healthy_pod uid=$healthy_uid"

  start_port_forward || fail "前端不可访问"
  first_ok=""
  window_start=""
  trec=""
  for _ in $(seq 1 120); do
    v=$(probe_value)
    s=$(home_status)
    if [ "$v" = "1" ] && [ "$s" = "200" ]; then
      if [ -z "$window_start" ]; then
        window_start=$(epoch)
        first_ok=$(now)
        say "RECOVERY_FIRST probe=1 home=200"
      fi
      if [ $(( $(epoch) - window_start )) -ge 60 ]; then trec=$(epoch); break; fi
    else
      if [ -n "$window_start" ]; then say "RECOVERY_RESET probe=$v home=$s"; fi
      window_start=""
    fi
    sleep 5
  done
  stop_port_forward
  [ -n "$trec" ] || fail "恢复窗口未成立"
  say "RECOVERY_WINDOW_DONE elapsed_since_rollback=$((trec-trb))s"

  tclear=""
  for _ in $(seq 1 90); do
    if [ "$(alert_state)" = "none" ]; then tclear=$(epoch); break; fi
    sleep 1
  done
  [ -n "$tclear" ] || fail "告警没有清除"
  say "ALERT_CLEARED elapsed_since_rollback=$((tclear-trb))s"

  say "FINAL image=$(deploy_image) generation=$("$KUBECTL" get deploy productcatalogservice -n "$NS" -o jsonpath='{.metadata.generation}') bad_rs_replicas=$(bad_rs_replicas) probe=$(probe_value) alerts=$(alert_state)"
  "$KUBECTL" get pods -n "$NS" -l app=productcatalogservice -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,IMAGE:.spec.containers[0].image,READY:.status.containerStatuses[0].ready
  result cleanup ok "image=$(deploy_image)" "probe=1" "alerts=none" "recovery_s=$((trec-trb))"
}

trap 'stop_port_forward' EXIT
VERB="${1:-all}"
case "${1:-all}" in
  preflight) preflight ;;
  recovered) recovered ;;
  inject) inject ;;
  verify) verify ;;
  cleanup) cleanup ;;
  all)
    preflight && inject && verify
    say "TOTAL elapsed done"
    cleanup
    ;;
  *) echo "用法：$0 {preflight|recovered|inject|verify|cleanup|all}" >&2; exit 2 ;;
esac
