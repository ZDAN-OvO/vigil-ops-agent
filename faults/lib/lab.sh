# 由实验夹具载入。集群写入始终使用指定实验凭据。
set -euo pipefail

kubeconfig=${VIGIL_LAB_KUBECONFIG:-${KUBECONFIG:-}}
: "${kubeconfig:?请指定实验操作者的 VIGIL_LAB_KUBECONFIG 或 KUBECONFIG}"
context_args=()
if [[ -n ${VIGIL_LAB_CONTEXT:-} ]]; then
  context_args=(--context "$VIGIL_LAB_CONTEXT")
fi

k() {
  "${KUBECTL:-kubectl}" --kubeconfig "$kubeconfig" "${context_args[@]}" "$@"
}

utc() { date -u +%Y-%m-%dT%H:%M:%SZ; }

check_operator() {
  local actor
  actor=$(k auth whoami -o jsonpath='{.status.userInfo.username}')
  if [[ -z $actor || $actor == system:serviceaccount:vigil-monitoring:vigil-remediation ]]; then
    printf '%s 需要独立的实验操作者身份\n' "$(utc)" >&2
    return 1
  fi
  printf '%s operator=%s\n' "$(utc)" "$actor"
}

prom_query() {
  local when=()
  if [[ -n ${2:-} ]]; then
    when=(--data-urlencode "time=$2")
  fi
  curl -fsS --max-time 10 --get --data-urlencode "query=$1" "${when[@]}" \
    "${VIGIL_LAB_PROMETHEUS_URL:-http://127.0.0.1:9090}/api/v1/query"
}

metric() {
  prom_query "$1" | "${PYTHON:-python3}" -c '
import json
import sys
response = json.load(sys.stdin)
series = response["data"]["result"]
print(series[0]["value"][1] if len(series) == 1 else "none")
'
}

install_app() {
  local configmap=$1
  local directory=$2
  k create namespace vigil-experiment --dry-run=client -o yaml | k apply -f -
  k -n vigil-experiment create configmap "$configmap" --from-file="app.py=$directory/app.py" \
    --dry-run=client -o yaml | k apply -f -
  k apply -f "$directory/manifests.yaml"
}

deploy_status() {
  k -n "$1" rollout status "deployment/$2" --timeout=180s
}

await_condition() {
  local deadline=$((SECONDS + ${WAIT_SECONDS:-300}))
  until "$@"; do
    if (( SECONDS >= deadline )); then
      printf '%s 等待 %s 超时\n' "$(utc)" "$1" >&2
      return 1
    fi
    sleep 5
  done
}

template_field() {
  k -n "$1" get deployment "$2" -o "jsonpath={.spec.template.spec.containers[?(@.name==\"$3\")].$4}"
}
