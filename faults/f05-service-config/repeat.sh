#!/usr/bin/env bash
here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
source "$here/../lib/lab.sh"

namespace=vigil-experiment
target=vigil-config-fixture
operation=${1:-}
source_name=${2:-configmap}
profile=${3:-normal}
case "$source_name" in
  configmap|env) ;;
  *) printf '未知的配置来源：%s\n' "$source_name" >&2; exit 2 ;;
esac
case "$profile" in
  normal|compensate|low-margin) ;;
  *) printf '未知的配置夹具模式：%s\n' "$profile" >&2; exit 2 ;;
esac
gate="probe_success{job=\"vigil-config-probe\",signal=\"gate\",source=\"$source_name\"}"
business="probe_success{job=\"vigil-config-probe\",signal=\"business\",source=\"$source_name\"}"
alert="ALERTS{alertname=\"VigilServiceConfigDegraded\",alertstate=\"firing\",namespace=\"vigil-experiment\",target=\"$target\",source=\"$source_name\"}"

current_mode() {
  if [[ $source_name == configmap ]]; then
    k -n "$namespace" get configmap "$target" -o 'jsonpath={.data.mode}'
  else
    template_field "$namespace" "$target" app 'env[?(@.name=="VIGIL_MODE")].value'
  fi
}

loaded_mode() {
  metric "vigil_config_fixture_loaded_mode{job=\"vigil-config-state\",source=\"$source_name\",mode=\"$1\"}"
}

configuration_recovered() {
  [[ $(loaded_mode healthy) == 1 && $(metric "$gate") == 1 \
    && $(metric "$business") == 1 && $(metric "$alert") == none ]]
}

set_mode() {
  if [[ $source_name == configmap ]]; then
    k -n "$namespace" patch "configmap/$target" --type=merge -p "{\"data\":{\"mode\":\"$1\"}}"
  else
    k -n "$namespace" set env "deployment/$target" --containers=app "VIGIL_MODE=$1"
    deploy_status "$namespace" "$target"
  fi
}

set_profile() {
  k -n "$namespace" set env "deployment/$target" --containers=app "VIGIL_PROFILE=$1"
  deploy_status "$namespace" "$target"
}

snapshot() {
  local mode
  mode=$(current_mode)
  printf '%s source=%s mode=%s loaded=%s gate=%s business=%s alert=%s\n' \
    "$(utc)" "$source_name" "$mode" "$(loaded_mode "$mode")" "$(metric "$gate")" \
    "$(metric "$business")" "$(metric "$alert")"
}

case "$operation" in
  install)
    check_operator
    install_app vigil-config-code "$here"
    deploy_status "$namespace" "$target"
    ;;
  inject)
    check_operator
    [[ $(current_mode) == healthy && $(metric "$gate") == 1 && $(metric "$alert") == none ]]
    if [[ $source_name == configmap && $profile != normal ]]; then
      set_profile "$profile"
    fi
    set_mode degraded
    snapshot
    ;;
  verify)
    snapshot
    [[ $(current_mode) == degraded && $(loaded_mode degraded) == 1 \
      && $(metric "$gate") == 0 && $(metric "$alert") == 1 ]]
    ;;
  samples) prom_query "${business}[5m]" ;;
  status) snapshot ;;
  restore)
    check_operator
    if [[ $(current_mode) != healthy ]]; then
      set_mode healthy
    fi
    if [[ $source_name == configmap \
      && $(template_field "$namespace" "$target" app 'env[?(@.name=="VIGIL_PROFILE")].value') != normal ]]; then
      set_profile normal
    fi
    await_condition configuration_recovered
    snapshot
    ;;
  drift)
    check_operator
    set_mode limited
    ;;
  share|unshare)
    check_operator
    if [[ $operation == share ]]; then
      k apply -f "$here/shared-consumer.yaml"
      deploy_status "$namespace" vigil-config-second-consumer
    else
      k delete -f "$here/shared-consumer.yaml" --ignore-not-found
    fi
    ;;
  *)
    printf '用法：%s {install|inject|verify|samples|status|restore|drift|share|unshare} [configmap|env] [normal|compensate|low-margin]\n' "$0" >&2
    exit 2
    ;;
esac
