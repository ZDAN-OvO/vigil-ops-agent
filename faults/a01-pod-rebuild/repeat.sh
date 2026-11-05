#!/usr/bin/env bash
# A01 实验驱动：证明临时状态故障跨容器重启持续、只在 Pod 重建后消除。
# 仅用于专用实验集群。删除 Pod 由操作者执行，用于验证机制，不计 Agent 处置。
set -u

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/vigil-lab.yaml}"
KUBECTL="${KUBECTL:-kubectl}"
NS="${NS:-vigil-experiment}"
NS_OWNER="a01-pod-rebuild"
DEPLOY="${DEPLOY:-rebuild-demo}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PF_PORT="${PF_PORT:-8082}"
PF_LOG="${PF_LOG:-${TMPDIR:-/tmp}/vigil-a01-port-forward.log}"
WAIT_SECONDS="${WAIT_SECONDS:-120}"
OBSERVE_SECONDS="${OBSERVE_SECONDS:-60}"
RECOVERY_SECONDS="${RECOVERY_SECONDS:-90}"
SAMPLE_INTERVAL="${SAMPLE_INTERVAL:-1}"

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { printf '%s %s\n' "$(now)" "$*"; }
fail() { say "FAIL $*"; exit 1; }

kget() { "$KUBECTL" -n "$NS" "$@" 2>/dev/null; }

# 输出 "名称 UID restartCount ready"。跳过正在删除的 Pod，避免删除竞态读错对象。
active_pod() {
  kget get pods -l app="$DEPLOY" -o json | python3 -I -c '
import json, sys
try:
    items = json.load(sys.stdin)["items"]
except Exception:
    sys.exit(1)
for item in items:
    meta = item.get("metadata", {})
    if meta.get("deletionTimestamp"):
        continue
    status = item.get("status", {})
    if status.get("phase") in ("Succeeded", "Failed"):
        continue
    container = (status.get("containerStatuses") or [{}])[0]
    print(meta.get("name", ""), meta.get("uid", ""), container.get("restartCount", ""),
          str(container.get("ready", "")).lower())
    break
else:
    sys.exit(1)
'
}

pod_count() { kget get pods -l app="$DEPLOY" -o name | wc -l | tr -d ' '; }
owner_rs() {
  kget get pod "$1" -o jsonpath='{range .metadata.ownerReferences[*]}{.kind}{"\t"}{.uid}{"\n"}{end}' \
    | awk -F'\t' '$1 == "ReplicaSet" { print $2 }'
}
template_json() { kget get deploy "$DEPLOY" -o jsonpath='{.spec.template}'; }
hash_of() { printf '%s' "$1" | sha256sum | cut -d' ' -f1; }
work_status() { curl -s -o /dev/null -w '%{http_code}' --max-time 5 "http://127.0.0.1:${PF_PORT}/work" 2>/dev/null || echo 000; }

start_port_forward() {
  stop_port_forward
  local name
  name=$(active_pod | cut -d' ' -f1)
  [ -n "$name" ] || fail "找不到可用的目标 Pod，无法建立转发"
  "$KUBECTL" -n "$NS" port-forward "pod/$name" "${PF_PORT}:8080" --address 127.0.0.1 >"$PF_LOG" 2>&1 &
  pf=$!
  local deadline=$(( $(date +%s) + 30 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    kill -0 "$pf" 2>/dev/null || fail "port-forward 进程退出：$(tail -n 1 "$PF_LOG" 2>/dev/null)"
    case "$(work_status)" in 200 | 500) return 0 ;; esac
    sleep 1
  done
  fail "30 秒内 port-forward 未就绪"
}
stop_port_forward() {
  if [ -n "${pf:-}" ]; then kill "$pf" 2>/dev/null; wait "$pf" 2>/dev/null; fi
  pf=""
}

# 在给定秒数内统计真实请求结果，输出 "成功 失败 其他"。
sample() {
  local secs=$1 ok=0 failed=0 other=0 deadline
  deadline=$(( $(date +%s) + secs ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    case "$(work_status)" in
      200) ok=$((ok + 1)) ;;
      500) failed=$((failed + 1)) ;;
      *) other=$((other + 1)) ;;
    esac
    sleep "$SAMPLE_INTERVAL"
  done
  echo "$ok $failed $other"
}

wait_status() {
  local want=$1 deadline=$2
  while [ "$(date +%s)" -lt "$deadline" ]; do
    [ "$(work_status)" = "$want" ] && return 0
    sleep 1
  done
  return 1
}

read_identity() {
  local out
  out=$(active_pod) || fail "无法读取目标 Pod 身份，kubectl 失败或没有可用 Pod"
  read -r pod_name pod_uid restarts ready <<<"$out"
  [ -n "$pod_name" ] && [ -n "$pod_uid" ] && [ -n "$restarts" ] \
    || fail "Pod 身份字段为空：name=$pod_name uid=$pod_uid restarts=$restarts"
}

# 命名空间只由本脚本创建和删除；已存在但不属于 A01 时不接管、不清理。
# 读取失败不能当成“不存在”，否则权限或连接故障会绕过所有权检查。
OWNER=""
read_namespace_owner() {
  local out
  if out=$("$KUBECTL" get namespace "$NS" \
    -o jsonpath='{.metadata.annotations.vigil\.io/owner}' 2>&1); then
    OWNER="$out"
    return 0
  fi
  case "$out" in
    *NotFound*) OWNER="" ; return 1 ;;
    *) fail "无法读取命名空间 $NS 的归属：$out" ;;
  esac
}

ensure_namespace() {
  if read_namespace_owner; then
    [ "$OWNER" = "$NS_OWNER" ] || fail "命名空间 $NS 已存在且不属于 A01（owner=${OWNER:-无}），停止"
    return 0
  fi
  "$KUBECTL" create namespace "$NS" >/dev/null || fail "创建命名空间失败"
  "$KUBECTL" annotate namespace "$NS" "vigil.io/owner=$NS_OWNER" >/dev/null \
    || fail "标注命名空间失败"
  say "NAMESPACE_CREATED namespace=$NS owner=$NS_OWNER"
}

deploy() {
  ensure_namespace
  kget get deploy "$DEPLOY" >/dev/null && fail "已存在 $DEPLOY"
  "$KUBECTL" apply -f "$DIR/manifests.yaml" >/dev/null || fail "清单应用失败"
  "$KUBECTL" -n "$NS" rollout status deploy/"$DEPLOY" --timeout=120s >/dev/null || fail "Deployment 未就绪"
  read_identity
  local rs tmpl
  rs=$(owner_rs "$pod_name")
  tmpl=$(template_json)
  [ -n "$rs" ] || fail "Pod 没有 ReplicaSet 归属"
  [ -n "$tmpl" ] || fail "无法读取 Deployment 模板"
  start_port_forward
  [ "$(work_status)" = "200" ] || fail "部署后 /work 不是 200"
  say "DEPLOYED pod=$pod_name uid=$pod_uid rs=$rs template=$(hash_of "$tmpl") ready=$ready restarts=$restarts work=200"
}

inject() {
  read_identity
  local rs tmpl
  rs=$(owner_rs "$pod_name")
  tmpl=$(template_json)
  [ -n "$rs" ] && [ -n "$tmpl" ] || fail "无法读取 Pod 归属或 Deployment 模板"
  start_port_forward
  [ "$(work_status)" = "200" ] || fail "注入前 /work 不是 200"
  say "BASELINE pod=$pod_name uid=$pod_uid rs=$rs template=$(hash_of "$tmpl") restarts=$restarts work=200"
  "$KUBECTL" -n "$NS" exec "$pod_name" -- sh -c 'printf corrupt > /state/work_state' >/dev/null \
    || fail "无法写入状态文件"
  wait_status 500 $(( $(date +%s) + 60 )) || fail "注入后 /work 不是 500"
  read_identity
  say "INJECTED pod=$pod_name uid=$pod_uid restarts=$restarts work=500"
}

verify() {
  local uid_before restarts_before ok failed other
  read_identity
  uid_before=$pod_uid
  restarts_before=$restarts
  start_port_forward
  [ "$(work_status)" = "500" ] || fail "观测前 /work 不是 500"
  read -r ok failed other <<<"$(sample "$OBSERVE_SECONDS")"
  local after_uid after_restarts
  read_identity
  after_uid=$pod_uid
  after_restarts=$restarts
  say "OBSERVE seconds=$OBSERVE_SECONDS ok=$ok fail=$failed other=$other uid=$after_uid restarts=$after_restarts"
  [ "$failed" -gt 0 ] || fail "观察窗内没有失败请求"
  [ "$ok" -eq 0 ] || fail "观察窗内出现成功请求"
  [ "$other" -eq 0 ] || fail "观察窗内出现连接异常，采样不完整"
  [ "$after_uid" = "$uid_before" ] || fail "观察期间 Pod UID 变化"
  [ "$after_restarts" = "$restarts_before" ] || fail "观察期间容器重启次数变化"
  say "VERIFIED_FAULT uid=$after_uid restarts=$after_restarts work=500"
}

container_restart() {
  local uid_before restarts_before ok failed other
  read_identity
  uid_before=$pod_uid
  restarts_before=$restarts
  start_port_forward
  [ "$(work_status)" = "500" ] || fail "容器重启前故障不成立"
  say "RESTART_CONTAINER before pod=$pod_name uid=$uid_before restarts=$restarts_before work=500"
  # 容器内 PID 1 收不到同命名空间的致命信号，用应用自身的退出接口模拟进程崩溃。
  curl -s -o /dev/null --max-time 5 "http://127.0.0.1:${PF_PORT}/crash" || true
  local deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if active_pod >/dev/null 2>&1; then
      read_identity
      if [ "$pod_uid" = "$uid_before" ] && [ "$restarts" -gt "$restarts_before" ] && [ "$ready" = "true" ]; then
        break
      fi
    fi
    sleep 1
  done
  read_identity
  say "RESTART_CONTAINER after pod=$pod_name uid=$pod_uid restarts=$restarts ready=$ready"
  [ "$pod_uid" = "$uid_before" ] || fail "Pod 被重建，不是容器重启"
  [ "$restarts" -gt "$restarts_before" ] || fail "容器没有重启"
  start_port_forward
  wait_status 500 $(( $(date +%s) + 60 )) || fail "容器重启后故障消失"
  read -r ok failed other <<<"$(sample 20)"
  say "VERIFIED_CONTAINER_RESTART uid=$pod_uid restarts=$restarts ok=$ok fail=$failed other=$other"
  [ "$failed" -gt 0 ] && [ "$ok" -eq 0 ] || fail "容器重启后请求未持续失败"
}

rebuild() {
  local old_name old_uid old_restarts rs_before tmpl_before started deadline elapsed
  read_identity
  old_name=$pod_name
  old_uid=$pod_uid
  old_restarts=$restarts
  rs_before=$(owner_rs "$old_name")
  tmpl_before=$(template_json)
  [ -n "$rs_before" ] || fail "目标 Pod 没有 ReplicaSet 归属"
  [ -n "$tmpl_before" ] || fail "无法读取 Deployment 模板"
  start_port_forward
  [ "$(work_status)" = "500" ] || fail "重建前故障不成立"
  say "REBUILD before pod=$old_name uid=$old_uid rs=$rs_before template=$(hash_of "$tmpl_before") restarts=$old_restarts work=500"
  stop_port_forward
  started=$(date +%s)
  deadline=$(( started + RECOVERY_SECONDS ))
  "$KUBECTL" -n "$NS" delete pod "$old_name" --wait=false >/dev/null || fail "删除 Pod 失败"
  local new_uid=""
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if active_pod >/dev/null 2>&1; then
      read_identity
      if [ "$pod_uid" != "$old_uid" ] && [ "$ready" = "true" ]; then
        new_uid=$pod_uid
        break
      fi
    fi
    sleep 1
  done
  [ -n "$new_uid" ] || fail "${RECOVERY_SECONDS} 秒内没有出现就绪的新 Pod"
  start_port_forward
  wait_status 200 "$deadline" || fail "新 Pod 未在恢复窗口内恢复业务请求"
  elapsed=$(( $(date +%s) - started ))
  [ "$elapsed" -le "$RECOVERY_SECONDS" ] || fail "恢复耗时 ${elapsed}s 超过窗口 ${RECOVERY_SECONDS}s"
  local rs_after tmpl_after
  read_identity
  rs_after=$(owner_rs "$pod_name")
  tmpl_after=$(template_json)
  [ -n "$rs_after" ] || fail "新 Pod 没有 ReplicaSet 归属"
  [ -n "$tmpl_after" ] || fail "无法读取重建后的 Deployment 模板"
  say "REBUILT pod=$pod_name uid=$pod_uid rs=$rs_after template=$(hash_of "$tmpl_after") ready=$ready restarts=$restarts work=200 elapsed_s=$elapsed"
  [ "$pod_uid" = "$new_uid" ] || fail "读到的 Pod 不是刚重建的那一个"
  [ "$rs_after" = "$rs_before" ] || fail "目标 Pod 的 ReplicaSet 变了"
  [ "$(hash_of "$tmpl_after")" = "$(hash_of "$tmpl_before")" ] || fail "Deployment 模板变了"
  say "VERIFIED_REBUILD old_uid=$old_uid new_uid=$new_uid template_unchanged=1 rs_unchanged=1 work=200 elapsed_s=$elapsed"
}

# 任何删除请求之前先确认命名空间归属；不属于 A01 时连其中的资源也不碰。
require_owned_namespace() {
  read_namespace_owner || return 0
  [ "$OWNER" = "$NS_OWNER" ] || fail "命名空间 $NS 不属于 A01（owner=${OWNER:-无}），拒绝删除其中资源"
}

delete_namespace_if_owned() {
  local deadline
  if ! read_namespace_owner; then
    say "NAMESPACE_ABSENT namespace=$NS"
    return 0
  fi
  if [ "$OWNER" != "$NS_OWNER" ]; then
    say "NAMESPACE_KEPT namespace=$NS owner=${OWNER:-无}"
    return 0
  fi
  "$KUBECTL" delete namespace "$NS" --wait=false >/dev/null || fail "删除命名空间失败"
  deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! "$KUBECTL" get namespace "$NS" >/dev/null 2>&1; then
      say "NAMESPACE_REMOVED namespace=$NS"
      return 0
    fi
    sleep 1
  done
  fail "命名空间删除未完成：$NS"
}

cleanup() {
  say "CLEANUP start pods=$(pod_count)"
  stop_port_forward
  require_owned_namespace
  "$KUBECTL" delete -f "$DIR/manifests.yaml" --ignore-not-found >/dev/null || fail "删除清单失败"
  local deadline=$(( $(date +%s) + WAIT_SECONDS ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    if ! kget get deploy "$DEPLOY" >/dev/null && [ "$(pod_count)" -eq 0 ]; then
      say "CLEANED deployment=$DEPLOY service=$DEPLOY pods=0"
      delete_namespace_if_owned
      return 0
    fi
    sleep 1
  done
  fail "清理未完成，仍有 pods=$(pod_count) 个"
}

trap stop_port_forward EXIT
# 清单里写死命名空间；允许覆盖 NS 会让所有权检查和实际删除目标不一致。
[ "$NS" = "vigil-experiment" ] || fail "NS 必须与 manifests.yaml 一致：vigil-experiment"
case "${1:-}" in
  deploy) deploy ;;
  inject) inject ;;
  verify) verify ;;
  container-restart) container_restart ;;
  rebuild) rebuild ;;
  cleanup) cleanup ;;
  *) echo "用法：$0 {deploy|inject|verify|container-restart|rebuild|cleanup}" >&2; exit 2 ;;
esac
