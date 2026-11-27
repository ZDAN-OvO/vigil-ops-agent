#!/usr/bin/env bash
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$here/../lib/lab.sh"

namespace=vigil-shop
target=currencyservice
container=server
original_image=gcr.io/google-samples/microservices-demo/currencyservice@sha256:16d16968f90aff24d4170ca7c4c0ff97303612b98c509052d5c3618f6e75937a
pause_image=registry.k8s.io/pause:3.10.1
probe='probe_success{job="frontend-probe"}'
alert='ALERTS{alertname="FrontendProbeFailed",alertstate="firing",namespace="vigil-shop",target="currencyservice"}'
cpu_alert='ALERTS{alertname="VigilCpuLimitDegraded",alertstate="firing",namespace="vigil-shop",target="currencyservice"}'
ratio='vigil:currency_cpu_throttle_ratio:rate1m'
periods='container_cpu_cfs_periods_total{job="kubelet-cadvisor",namespace="vigil-shop",pod=~"currencyservice-.*",container="server"}'
throttled='container_cpu_cfs_throttled_periods_total{job="kubelet-cadvisor",namespace="vigil-shop",pod=~"currencyservice-.*",container="server"}'

field() { template_field "$namespace" "$target" "$container" "$1"; }
memory_limit() { field resources.limits.memory; }
cpu_limit() { field resources.limits.cpu; }
current_pods() {
  k -n "$namespace" get deployments,replicasets,pods -o json |
    "${PYTHON:-python3}" "$here/current-pods.py"
}

snapshot() {
  printf '%s memory=%s cpu=%s probe=%s http=%s frontend_alert=%s cpu_alert=%s throttle=%s pods=%s\n' \
    "$(utc)" "$(memory_limit)" "$(cpu_limit)" "$(metric "$probe")" \
    "$(metric 'probe_http_status_code{job="frontend-probe"}')" "$(metric "$alert")" \
    "$(metric "$cpu_alert")" "$(metric "$ratio")" "$(current_pods)"
}

patch_container() {
  k -n "$namespace" patch "deployment/$target" --type=strategic \
    -p "{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"$container\",$1}]}}}}"
}

restart_at_single_replica() {
  # 保证尚未就绪的故障版本不会被原健康副本掩盖。
  k -n "$namespace" scale "deployment/$target" --replicas=0
  k -n "$namespace" wait --for=delete pod -l "app=$target" --timeout=90s
  k -n "$namespace" scale "deployment/$target" --replicas=1
}

memory_baseline() {
  [[ $(memory_limit) == 128Mi && $(field image) == "$original_image" \
    && $(metric "$probe") == 1 && $(metric "$alert") == none ]]
}

business_recovered() {
  [[ $(metric "$probe") == 1 && $(metric "$alert") == none && $(metric "$cpu_alert") == none ]]
}

cpu_load_start() {
  k create namespace vigil-experiment --dry-run=client -o yaml | k apply -f -
  k -n vigil-experiment create configmap vigil-currency-load \
    --from-file="cpu-load.py=$here/cpu-load.py" --dry-run=client -o yaml | k apply -f -
  k apply -f "$here/cpu-load.yaml"
  deploy_status vigil-experiment vigil-currency-load
}

verify_memory() {
  local kind=$1
  local deadline=$((SECONDS + ${WAIT_SECONDS:-300}))
  while (( SECONDS < deadline )); do
    snapshot
    if [[ $(memory_limit) == 16Mi && $(metric "$probe") == 0 ]] &&
      current_pods | "${PYTHON:-python3}" -c '
import json
import sys
pods = json.load(sys.stdin)
if sys.argv[1] == "verify":
    ok = any(p["last_reason"] == "OOMKilled" and p["restarts"] >= 3 for p in pods)
else:
    ok = bool(pods) and any(p["running"] for p in pods) and all(
        p["last_reason"] != "OOMKilled" for p in pods
    )
raise SystemExit(not ok)
' "$kind" && [[ $(metric "$alert") == 1 ]]; then
      return 0
    fi
    sleep 5
  done
  printf '%s 故障未同时满足低限额、现行 Pod 状态和首页告警\n' "$(utc)" >&2
  return 1
}

case "${1:-}" in
  inject|inject-insufficient)
    check_operator
    memory_baseline
    image=$original_image
    if [[ $1 == inject-insufficient ]]; then
      image=$pause_image
    fi
    patch_container "\"image\":\"$image\",\"resources\":{\"limits\":{\"memory\":\"16Mi\"},\"requests\":{\"memory\":\"8Mi\"}}"
    restart_at_single_replica
    snapshot
    ;;
  verify|verify-insufficient) verify_memory "$1" ;;
  cpu-load)
    check_operator
    cpu_load_start
    ;;
  cpu-inject)
    check_operator
    [[ $(cpu_limit) == 200m && $(metric "$probe") == 1 && $(metric "$cpu_alert") == none ]]
    [[ $(metric "count($periods) > bool 0") == 1 && $(metric "count($throttled) > bool 0") == 1 ]]
    [[ $(metric "$ratio < bool 0.5") == 1 ]]
    # 给受限容器留出连续取证时间，保留原就绪检查。
    patch_container '"resources":{"limits":{"cpu":"5m"},"requests":{"cpu":"5m"}},"livenessProbe":{"failureThreshold":30}'
    restart_at_single_replica
    snapshot
    ;;
  cpu-verify)
    snapshot
    [[ $(cpu_limit) == 5m && $(metric "$probe") == 0 && $(metric "$cpu_alert") == 1 \
      && $(metric "$ratio >= bool 0.5") == 1 ]]
    ;;
  cpu-stop)
    check_operator
    k delete -f "$here/cpu-load.yaml" --ignore-not-found
    ;;
  relapse|end-relapse)
    check_operator
    selector=$target
    if [[ $1 == relapse ]]; then
      [[ $(memory_limit) == 128Mi && $(metric "$probe") == 1 && $(metric "$alert") == none ]]
      selector=vigil-acceptance-offline
    fi
    k -n "$namespace" patch "service/$target" --type=merge \
      -p "{\"spec\":{\"selector\":{\"app\":\"$selector\"}}}"
    ;;
  status) snapshot ;;
  restore)
    check_operator
    if [[ $(memory_limit) != 128Mi || $(field resources.requests.memory) != 64Mi \
      || $(cpu_limit) != 200m || $(field resources.requests.cpu) != 100m \
      || $(field image) != "$original_image" ]]; then
      patch_container "\"image\":\"$original_image\",\"resources\":{\"limits\":{\"memory\":\"128Mi\",\"cpu\":\"200m\"},\"requests\":{\"memory\":\"64Mi\",\"cpu\":\"100m\"}}"
    fi
    if [[ $(field livenessProbe.failureThreshold) == 30 ]]; then
      patch_container '"livenessProbe":{"failureThreshold":3}'
    fi
    if [[ $(k -n "$namespace" get deployment "$target" -o 'jsonpath={.spec.replicas}') != 1 ]]; then
      k -n "$namespace" scale "deployment/$target" --replicas=1
    fi
    deploy_status "$namespace" "$target"
    await_condition business_recovered
    snapshot
    ;;
  *)
    printf '用法：%s {inject|verify|inject-insufficient|verify-insufficient|cpu-load|cpu-inject|cpu-verify|cpu-stop|relapse|end-relapse|status|restore}\n' "$0" >&2
    exit 2
    ;;
esac
