# 坏发布运行记录 · Web 对话

实验日期为 2026-11-18（UTC），集群为 `vigil-lab`，命名空间为 `vigil-shop`。执行版本为 `90731592832a1ed4b6887f886fdf7ddcc2ac7d8f`。故障注入、核验和复位使用[场景脚本](../repeat.sh)。浏览器通过本机页面提交初始症状、线索和审批决定。

| 验收 | 调查 | 提案 | 结果 |
|---|---|---|---|
| 批准并恢复 | `inv-e65a7c6f7e42` | `act-76d24cc7a073` | 页面显示已写入、验证改善并结束；首页 200、探针恢复、告警清零 |
| 拒绝后继续调查 | `inv-236b6cd6ab3d` | `act-4be8124620a9` | 拒绝决定已记录，调查正常结束并给出结论，无执行记录或新提案 |
| 目标变化 | `inv-e80bfc2b049c` | `act-29faf38d9869` | 页面显示未执行、目标已被改动；批准前后 Deployment generation 与 Pod 模板不变 |
| 历史版本变化 | `inv-ef7bd756b2f9` | `act-5eee40f42746` | 页面显示未执行、历史版本已变化；Deployment generation 与 Pod 模板不变 |
| 授权过期 | `inv-e64e9b29bb40` | `act-eb4e977414f4` | 页面显示未执行、授权已过期；Deployment generation 与 Pod 模板未变化 |

## 批准并恢复

- 用户选择 `frontend`，描述首页 500，补充商品详情和购物车仍可访问。页面把线索标为「已送入调查（第 1 轮）」，展示调查进展和按假设分组的证据。
- 回滚卡片显示 `vigil-shop/Deployment/productcatalogservice`，从 revision 110 回到 109。本次模板差异为 `spec.containers[server].image` 一项。浏览器刷新后卡片指纹一致。
- 结构化批准接口返回 202。执行结果为 `sent`、`improved`、`verification_done=true`。实际 Deployment 的 `spec.template` 与数据库提案中的固定模板逐字段相等，Deployment generation 从 110 变为 111。
- `aiops --help` 显示 `serve` 和 `doctor`。服务运行时，执行 `aiops dev approve act-acceptance-test` 退出码为 1，提示「Vigil 正在运行，请在页面上操作」。
- 已结束会话在 `serve` 重启前后快照逐字段一致。浏览器 SSE 断线显示「重连中」，自动重新连接后快照一致。

命令输出摘录：

```text
RESULT verb=inject ok=1 image=vigil/productcatalogservice:canary-20261104 pod=productcatalogservice-57f9df78f8-2dzpl probe=0 alert=firing elapsed_s=85
RESULT verb=verify ok=1 image=vigil/productcatalogservice:canary-20261104 home=500 probe=0 alert=firing
Deployment.spec.template 与提案参数 target_template 逐字段一致
generation 110 -> 111
RECOVERED_CHECK home=200 probe=1 alert=none
RECOVERY_WINDOW_DONE elapsed_since_rollback=63s
FINAL generation=111 bad_rs_replicas=0 probe=1 alerts=none
```

操作者在业务已恢复后运行复位检查。脚本跳过额外回滚，并确认健康镜像、坏 ReplicaSet 为 0 副本、探针为 1、没有活动告警。

截图与完整命令输出保存在本机工作区的 `.evidence/f01-web/`。主流程截图为 `01-initial.png`、`02-investigating.png`、`03-proposal.png`、`04-result.png`，重连截图为 `reconnected.png`。

## 授权过期

验收进程设置 `VIGIL_ACTION_APPROVAL_TTL_SECONDS=0`，使获得批准后由后台领取时授权已经过期。
浏览器点击批准，页面显示「未执行」「授权已过期」。执行结果为 `not_sent / expired`。
浏览器验收脚本读取批准前后的 Deployment，逐字段核对 Pod 模板并比较 generation，两者均未变化。
截图为 `expire-result.png`。

## 首轮反例验收中的中断

首次拒绝验收使用的浏览器定位器没有考虑按钮文字中的排版空格，未能点击拒绝。调查
`inv-523d1cb34f97` 停在提案 `act-5f1d8c5d8b16`，目标变化和历史版本变化用例随之无法发起。
修正定位器后，原提案的拒绝请求返回 202。紧接着切换到授权过期配置时进程重启，该次未观察到正常收尾，
因此不计为拒绝验收通过。失败时的截图、浏览器日志和快照保存在本机证据目录。

## 拒绝后继续调查

重新注入故障后，从浏览器发起独立调查。点击拒绝后接口返回 202。
会话最终为 `finished`，提案仍为 `act-4be8124620a9 / rejected`，执行结果为空。
拒绝前后 Deployment generation 与 Pod 模板一致。截图为 `reject-result.png`。

在另一个本机来源用 iframe 打开页面时，Chromium 报告 `frame-ancestors 'none'` 并阻止加载。

## 历史版本变化

在点击批准之前，操作者给历史 ReplicaSet 的 Pod 模板增加一条验收注解。
执行记录为 `not_sent / revision_changed`，Deployment generation 与实际 Pod 模板均保持不变。
验收后删除了历史模板上的验收注解。截图为 `revision-result.png`。

## 目标变化的首次测量

调查 `inv-b146a6de7489`、提案 `act-70ffda6337f4`。操作者修改 Deployment 注解后，页面得到
`not_sent / drift`，实际 Pod 模板一致。脚本把操作者改注解前的 generation 114 用作比较基线，
而操作本身把 generation 增到 115。该次保留为未通过测量，重跑改为在操作者修改后、批准前采集基线。

## 目标变化重跑

提案就绪后，操作者给 Deployment 增加临时验收注解。脚本在改动后、点击批准前采集
Deployment generation 和 Pod 模板。页面批准后执行记录为 `not_sent / drift`。
批准前后 generation 与 Pod 模板均相同。截图为 `drift-result.png`。

## 最终复位

最后一轮在 2026-11-18 18:36:47 UTC 完成复位。验收注解已清除，故障副本为 0，
商店首页在连续 60 秒窗口内返回 200，探针为 1，活动告警为 0。

```text
RECOVERY_WINDOW_DONE elapsed_since_rollback=90s
ALERT_CLEARED elapsed_since_rollback=90s
FINAL generation=121 bad_rs_replicas=0 probe=1 alerts=none
```
