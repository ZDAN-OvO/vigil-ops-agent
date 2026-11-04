#!/usr/bin/env bash
# F01 实验驱动脚本：真实注入坏发布、观察告警、执行操作者回退并验证复位。
# 仅用于专用实验集群。运行前确认 KUBECONFIG 指向 vigil-lab。
set -u

export KUBECONFIG="${KUBECONFIG:-$HOME/.kube/vigil-lab.yaml}"
KUBECTL="${KUBECTL:-kubectl}"
NS="${NS:-vigil-shop}"
MON="${MON:-vigil-monitoring}"
BAD="${BAD:-vigil/productcatalogservice-bad:exp-20261104-1}"
HEALTHY="${HEALTHY:-gcr.io/google-samples/microservices-demo/productcatalogservice@sha256:1d51f9e5aa923ff9c338b1b155b39da737306c0ebc230e62ec6271f2b0cb7bef}"
PF_PORT="${PF_PORT:-8080}"
PF_LOG="${PF_LOG:-${TMPDIR:-/tmp}/vigil-f01-port-forward.log}"

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
deploy_image() {
  "$KUBECTL" get deploy productcatalogservice -n "$NS" -o jsonpath='{.spec.template.spec.containers[0].image}'
}
bad_rs_replicas() {
  "$KUBECTL" get rs -n "$NS" -l app=productcatalogservice -o json 2>/dev/null \
    | python3 -I -c 'import sys,json
for r in json.load(sys.stdin)["items"]:
    if r["spec"]["template"]["spec"]["containers"][0]["image"].startswith("vigil/productcatalogservice-bad"):
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
fail() {
  say "FAIL: $*"
  rollback_if_bad
  say "STATE image=$(deploy_image) probe=$(probe_value) alerts=$(alert_state)"
  exit 1
}

trap 'kill ${pf:-} 2>/dev/null || true' EXIT

say "PRE image=$(deploy_image) probe=$(probe_value) alerts=$(alert_state)"
[ "$(deploy_image)" = "$HEALTHY" ] || fail "productcatalog not on healthy image"
[ "$(probe_value)" = "1" ] || fail "probe_success not 1"
[ "$(alert_state)" = "none" ] || fail "active alerts present"
[ "$(bad_rs_replicas)" = "0" ] || fail "bad ReplicaSet not scaled to 0"

"$KUBECTL" port-forward -n "$NS" svc/frontend ${PF_PORT}:80 --address 127.0.0.1 >"$PF_LOG" 2>&1 &
pf=$!
for i in $(seq 1 30); do [ "$(home_status)" = "200" ] && break; sleep 1; done
[ "$(home_status)" = "200" ] || fail "frontend not reachable before injection"
say "BASELINE image=$(deploy_image) probe=$(probe_value) alerts=$(alert_state) home=200"

t0=$(epoch); say "INJECT start"
"$KUBECTL" set image deployment/productcatalogservice -n "$NS" server="$BAD" >/dev/null
"$KUBECTL" rollout status deployment/productcatalogservice -n "$NS" --timeout=180s >/dev/null || fail "bad rollout did not complete"
t_rollout=$(epoch); say "ROLLOUT_DONE elapsed=$((t_rollout-t0))s"
newpod=""
for i in $(seq 1 60); do
  newpod=$("$KUBECTL" get pods -n "$NS" -l app=productcatalogservice --no-headers -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[0].image | awk -v img="$BAD" '$2==img{print $1; exit}')
  [ -n "$newpod" ] && break
  sleep 1
done
[ -n "$newpod" ] || fail "bad pod not found"
newuid=$("$KUBECTL" get pod -n "$NS" "$newpod" -o jsonpath='{.metadata.uid}')
newimg=$("$KUBECTL" get pod -n "$NS" "$newpod" -o jsonpath='{.spec.containers[0].image}')
say "NEW_POD $newpod uid=$newuid image=$newimg"

t500=""; for i in $(seq 1 90); do if [ "$(home_status)" = "500" ]; then t500=$(epoch); break; fi; sleep 1; done
[ -n "$t500" ] || fail "home did not return 500"
say "HOME_500 elapsed=$((t500-t0))s"

tp0=""; for i in $(seq 1 90); do if [ "$(probe_value)" = "0" ]; then tp0=$(epoch); break; fi; sleep 1; done
[ -n "$tp0" ] || fail "probe_success did not reach 0"
say "PROBE_0 elapsed=$((tp0-t0))s"

tfire=""; for i in $(seq 1 180); do if [ "$(alert_state)" = "firing" ]; then tfire=$(epoch); break; fi; sleep 1; done
[ -n "$tfire" ] || fail "alert did not fire"
say "ALERT_FIRING elapsed=$((tfire-t0))s state=$(alert_state)"

trb=$(epoch); say "ROLLBACK start"
"$KUBECTL" rollout undo deployment/productcatalogservice -n "$NS" >/dev/null
"$KUBECTL" rollout status deployment/productcatalogservice -n "$NS" --timeout=180s >/dev/null || fail "healthy rollout did not complete"
t_rollback=$(epoch); say "ROLLBACK_DONE elapsed=$((t_rollback-trb))s image=$(deploy_image)"
healthy_pod=""
for i in $(seq 1 60); do
  healthy_pod=$("$KUBECTL" get pods -n "$NS" -l app=productcatalogservice --no-headers -o custom-columns=NAME:.metadata.name,IMAGE:.spec.containers[0].image | awk -v img="$HEALTHY" '$2==img{print $1; exit}')
  [ -n "$healthy_pod" ] && break
  sleep 1
done
[ -n "$healthy_pod" ] || fail "healthy pod not found"
healthy_uid=$("$KUBECTL" get pod -n "$NS" "$healthy_pod" -o jsonpath='{.metadata.uid}')
say "HEALTHY_POD $healthy_pod uid=$healthy_uid"

first_ok=""; window_start=""; trec=""
for i in $(seq 1 120); do
  v=$(probe_value); s=$(home_status)
  if [ "$v" = "1" ] && [ "$s" = "200" ]; then
    if [ -z "$window_start" ]; then window_start=$(epoch); first_ok=$(now); say "RECOVERY_FIRST probe=1 home=200"; fi
    if [ $(( $(epoch) - window_start )) -ge 60 ]; then trec=$(epoch); break; fi
  else
    if [ -n "$window_start" ]; then say "RECOVERY_RESET probe=$v home=$s"; fi
    window_start=""
  fi
  sleep 5
done
[ -n "$trec" ] || fail "recovery window not met"
say "RECOVERY_WINDOW_DONE elapsed_since_rollback=$((trec-t_rollback))s"

tclear=""; for i in $(seq 1 90); do if [ "$(alert_state)" = "none" ]; then tclear=$(epoch); break; fi; sleep 1; done
[ -n "$tclear" ] || fail "alert did not clear"
say "ALERT_CLEARED elapsed_since_rollback=$((tclear-t_rollback))s"

say "FINAL image=$(deploy_image) generation=$("$KUBECTL" get deploy productcatalogservice -n "$NS" -o jsonpath='{.metadata.generation}') bad_rs_replicas=$(bad_rs_replicas) probe=$(probe_value) alerts=$(alert_state)"
say "TOTAL elapsed=$(( $(epoch) - t0 ))s"
"$KUBECTL" get pods -n "$NS" -l app=productcatalogservice -o custom-columns=NAME:.metadata.name,UID:.metadata.uid,IMAGE:.spec.containers[0].image,READY:.status.containerStatuses[0].ready
"$KUBECTL" logs deploy/loadgenerator -n "$NS" --tail=5 2>&1 | tail -4
