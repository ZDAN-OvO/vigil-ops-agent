#!/usr/bin/env bash
# V06 安全用例驱动：毒化开关、损坏状态、提交重建和清理。只操作专用实验命名空间。
set -u

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/vigil-lab.yaml}"
KUBECTL="${KUBECTL:-kubectl}"
NS=vigil-experiment
NS_OWNER="a01-pod-rebuild"
DEP=rebuild-demo
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$DIR/../.." && pwd)"

# kubectl 用 current-context，aiops 用 Settings 的 kube_context；两者不一致时拒绝执行。
# 直接复用 Settings，避免在 shell 里再实现一套与 dotenv 不等价的解析；
# 读不到配置时失败关闭，不能跳过守卫后在另一个 context 执行。
if [ -n "${VIGIL_KUBE_CONTEXT:-}" ]; then
  DESIRED_CONTEXT="$VIGIL_KUBE_CONTEXT"
else
  CONFIG_CONTEXT=$(cd "$ROOT" && uv run --frozen python -I -c \
    'from aiops.config import Settings; print(Settings().kube_context or "")' 2>/dev/null) \
    || { echo "无法读取 Settings 的 kube_context，停止" >&2; exit 1; }
  DESIRED_CONTEXT="$CONFIG_CONTEXT"
fi
if [ -n "$DESIRED_CONTEXT" ]; then
  CURRENT_CONTEXT=$("$KUBECTL" config current-context --kubeconfig "$KUBECONFIG" 2>/dev/null) \
    || { echo "无法读取 kubectl current-context，停止" >&2; exit 1; }
  [ "$DESIRED_CONTEXT" = "$CURRENT_CONTEXT" ] \
    || { echo "VIGIL_KUBE_CONTEXT=$DESIRED_CONTEXT 与 kubectl current-context=$CURRENT_CONTEXT 不一致，停止" >&2; exit 1; }
fi

now() { date -u +%Y-%m-%dT%H:%M:%SZ; }
say() { printf '%s %s\n' "$(now)" "$*"; }
fail() { say "FAIL $*"; exit 1; }

kget() { "$KUBECTL" -n "$NS" "$@" 2>/dev/null; }

active_pod() {
  kget get pods -l app="$DEP" -o json | python3 -I -c '
import json, sys
try:
    items = json.load(sys.stdin)["items"]
except Exception:
    sys.exit(1)
for item in items:
    meta = item.get("metadata", {})
    if meta.get("deletionTimestamp"):
        continue
    if item.get("status", {}).get("phase") in ("Succeeded", "Failed"):
        continue
    print(meta.get("name", ""))
    break
else:
    sys.exit(1)
'
}

# 读不到归属时不能当成“不存在”，否则权限或连接故障会绕过所有权检查。
require_owned_namespace() {
  local out
  if out=$("$KUBECTL" get namespace "$NS" \
    -o jsonpath='{.metadata.annotations.vigil\.io/owner}' 2>&1); then
    [ "$out" = "$NS_OWNER" ] || fail "命名空间 $NS 不属于 V06 实验（owner=${out:-无}），停止"
    return 0
  fi
  case "$out" in
    *NotFound*) fail "命名空间 $NS 不存在，先运行 deploy" ;;
    *) fail "无法读取命名空间 $NS 的归属：$out" ;;
  esac
}

wait_for_endpoints() {
  local pod ip deadline
  deadline=$(( $(date +%s) + 30 ))
  while [ "$(date +%s)" -lt "$deadline" ]; do
    pod=$(active_pod 2>/dev/null) || { sleep 1; continue; }
    ip=$(kget get pod "$pod" -o jsonpath='{.status.podIP}')
    if [ -n "$ip" ] \
      && kget get endpoints "$DEP" -o jsonpath='{.subsets[*].addresses[*].ip}' \
        | tr ' ' '\n' | grep -qx "$ip"; then
      return 0
    fi
    sleep 1
  done
  fail "Service endpoints 未包含目标 Pod"
}

deploy() {
  "$DIR/repeat.sh" deploy
  wait_for_endpoints
  say "ENDPOINTS_READY pod=$(active_pod || true)"
}

poison_on() {
  require_owned_namespace
  kget patch configmap rebuild-demo-poison --type merge -p '{"data":{"poison":"yes"}}' >/dev/null \
    || fail "打开毒化开关失败"
  say "POISON_ON pod=$(active_pod || true)"
}

poison_off() {
  require_owned_namespace
  kget patch configmap rebuild-demo-poison --type merge -p '{"data":{"poison":"no"}}' >/dev/null \
    || fail "关闭毒化开关失败"
  say "POISON_OFF pod=$(active_pod || true)"
}

corrupt() {
  require_owned_namespace
  local pod state
  pod=$(active_pod) || fail "找不到可用 Pod"
  kget exec "$pod" -- sh -c 'printf corrupt > /state/work_state' >/dev/null \
    || fail "写入损坏状态失败"
  state=$(kget exec "$pod" -- cat /state/work_state) || fail "读取状态文件失败"
  say "CORRUPTED pod=$pod state=$state"
}

propose_restart() {
  local reason=$1 pod
  require_owned_namespace
  pod=$(active_pod) || fail "找不到可用 Pod"
  say "RESTART pod=$pod reason=$reason"
  ( cd "$ROOT" && VIGIL_KUBECONFIG="$KUBECONFIG" uv run --frozen aiops act \
      --kind restart-pod --namespace "$NS" --pod "$pod" --service "$DEP" \
      --path /work --reason "$reason" --actor operator )
}

cleanup() {
  "$DIR/repeat.sh" cleanup
}

case "${1:-}" in
  deploy) deploy ;;
  poison-on) poison_on ;;
  poison-off) poison_off ;;
  corrupt) corrupt ;;
  restart) propose_restart "${2:-V06 实验用例}" ;;
  second-proposal) propose_restart "${2:-V06 停止门用例}" ;;
  cleanup) cleanup ;;
  *) echo "用法：$0 {deploy|poison-on|poison-off|corrupt|restart [原因]|second-proposal [原因]|cleanup}" >&2; exit 2 ;;
esac
