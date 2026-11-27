#!/usr/bin/env bash
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$here/../lib/lab.sh"

namespace=vigil-experiment
target=vigil-replica-fixture
probe='probe_success{job="vigil-replica-probe"}'
endpoints='vigil_replica_fixture_endpoints{job="vigil-replica-state"}'
rejected='vigil_replica_fixture_rejected{job="vigil-replica-state"}'
alert='ALERTS{alertname="VigilReplicaCapacityDegraded",alertstate="firing",namespace="vigil-experiment",target="vigil-replica-fixture"}'

replicas() { k -n "$namespace" get deployment "$target" -o 'jsonpath={.spec.replicas}'; }

capacity_recovered() {
  [[ $(metric "$probe") == 1 && $(metric "$endpoints") == 2 \
    && $(metric "$rejected") == 0 && $(metric "$alert") == none ]]
}

snapshot() {
  printf '%s replicas=%s probe=%s endpoints=%s rejected=%s alert=%s\n' \
    "$(utc)" "$(replicas)" "$(metric "$probe")" "$(metric "$endpoints")" \
    "$(metric "$rejected")" "$(metric "$alert")"
}

case "${1:-}" in
  install)
    check_operator
    install_app vigil-replica-code "$here"
    deploy_status "$namespace" "$target"
    deploy_status "$namespace" vigil-replica-gateway
    ;;
  inject)
    check_operator
    [[ $(replicas) == 2 && $(metric "$probe") == 1 && $(metric "$alert") == none ]]
    snapshot
    k -n "$namespace" scale "deployment/$target" --replicas=1
    deploy_status "$namespace" "$target"
    ;;
  verify)
    snapshot
    [[ $(replicas) == 1 && $(metric "$probe") == 0 && $(metric "$endpoints") == 1 \
      && $(metric "$rejected") == 1 && $(metric "$alert") == 1 ]]
    ;;
  status) snapshot ;;
  restore)
    check_operator
    if [[ $(replicas) != 2 ]]; then
      k -n "$namespace" scale "deployment/$target" --replicas=2
    fi
    deploy_status "$namespace" "$target"
    await_condition capacity_recovered
    snapshot
    ;;
  *) printf '用法：%s {install|inject|verify|status|restore}\n' "$0" >&2; exit 2 ;;
esac
