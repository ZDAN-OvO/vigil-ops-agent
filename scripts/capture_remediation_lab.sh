#!/usr/bin/env bash
# 保存资源身份、指定字段与带原始时间戳的 Prometheus 样本。
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$here/../faults/lib/lab.sh"

case "${1:-}" in
  f02)
    ns=vigil-shop
    target=currencyservice
    container=server
    ;;
  f04)
    ns=vigil-experiment
    target=vigil-replica-fixture
    container=app
    ;;
  f05)
    ns=vigil-experiment
    target=vigil-config-fixture
    container=app
    ;;
  *) printf '用法：%s {f02|f04|f05} 新建证据目录\n' "$0" >&2; exit 2 ;;
esac
output=${2:?指定新建证据目录}
mkdir "$output"
captured_at=$(utc)
printf '%s\n' "$captured_at" >"$output/captured-at.txt"

k -n "$ns" get "deployment/$target" -o json |
  "${PYTHON:-python3}" -c '
import json
import sys
obj = json.load(sys.stdin)
metadata, spec = obj["metadata"], obj["spec"]
container = next(c for c in spec["template"]["spec"]["containers"] if c["name"] == sys.argv[1])
json.dump({
    "metadata": {key: metadata.get(key) for key in (
        "name", "namespace", "uid", "resourceVersion", "generation"
    )},
    "revision": metadata.get("annotations", {}).get("deployment.kubernetes.io/revision"),
    "replicas": spec.get("replicas"),
    "resources": container.get("resources"),
    "image": container.get("image"),
    "environment": {var["name"]: var.get("value") for var in container.get("env", [])
                    if var["name"] in {"VIGIL_MODE", "VIGIL_PROFILE"}},
    "status": obj.get("status"),
}, sys.stdout, indent=2)
' "$container" >"$output/deployment.json"
k -n "$ns" get pods -l "app=$target" -o json |
  "${PYTHON:-python3}" -c '
import json
import sys
items = json.load(sys.stdin)["items"]
json.dump([{
    "name": pod["metadata"]["name"],
    "uid": pod["metadata"]["uid"],
    "owners": pod["metadata"].get("ownerReferences", []),
    "deleting_at": pod["metadata"].get("deletionTimestamp"),
    "statuses": pod.get("status", {}).get("containerStatuses", []),
} for pod in items], sys.stdout, indent=2)
' >"$output/pods.json"

save_query() {
  prom_query "$2" "$captured_at" >"$output/$1.json"
}

case "$1" in
  f02)
    save_query probe 'probe_success{job="frontend-probe"}[20m]'
    save_query http 'probe_http_status_code{job="frontend-probe"}[20m]'
    save_query throttle_ratio 'vigil:currency_cpu_throttle_ratio:rate1m[20m]'
    for metric in container_cpu_cfs_throttled_periods_total container_cpu_cfs_periods_total; do
      save_query "$metric" "$metric{job=\"kubelet-cadvisor\",namespace=\"$ns\",pod=~\"$target-.*\",container=\"$container\"}[20m]"
    done
    k -n vigil-experiment logs deployment/vigil-currency-load --timestamps --since=20m \
      >"$output/cpu-load.log" 2>"$output/cpu-load.stderr" || true
    ;;
  f04)
    save_query probe 'probe_success{job="vigil-replica-probe"}[20m]'
    save_query saturation 'vigil_replica_fixture_rejected{job="vigil-replica-state"}[20m]'
    save_query endpoints 'vigil_replica_fixture_endpoints{job="vigil-replica-state"}[20m]'
    save_query batch_time 'vigil_replica_fixture_batch_timestamp_seconds{job="vigil-replica-state"}[20m]'
    ;;
  f05)
    k -n "$ns" get "configmap/$target" \
      -o 'custom-columns=UID:.metadata.uid,RESOURCE_VERSION:.metadata.resourceVersion,MODE:.data.mode' \
      >"$output/configmap.txt"
    save_query probe 'probe_success{job="vigil-config-probe"}[20m]'
    save_query loaded_mode 'vigil_config_fixture_loaded_mode{job="vigil-config-state"}[20m]'
    ;;
esac
save_query alerts "ALERTS{namespace=\"$ns\",target=\"$target\"}[20m]"
printf '%s 已保存证据到 %s\n' "$captured_at" "$output"
