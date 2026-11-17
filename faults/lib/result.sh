#!/usr/bin/env bash
# 实验驱动脚本共用的机器可读结论行。
#
# 约定：每个动词的最后一行是 `RESULT verb=<动词> ok=<0|1> k=v ...`，
# 值里不能有空格；缺值的字段写 unknown，不能写空。
# 评估驱动只读这一行决定本次注入/核验/复位是否成立，其余输出留作证据。

result() {
  local verb=$1 status=$2
  shift 2
  local ok
  ok=0
  [ "$status" = "ok" ] && ok=1
  printf 'RESULT verb=%s ok=%s %s\n' "$verb" "$ok" "$*"
}

# 取最后一条匹配 `probe_success` 的点值；查询失败返回 none，不当 0 处理。
# 由各脚本按自己的 Prometheus 访问方式传入命令，避免在这里复制 kubectl 逻辑。
