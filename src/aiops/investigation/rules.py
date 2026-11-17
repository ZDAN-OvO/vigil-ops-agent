"""规则基线调查：固定的判定链，不调用模型，也不读场景真值。

它和 Agent 臂共用同一套只读工具、工具账本、假设与证据关系、动作提案与执行器；
差别只在“怎样得出结论”——这里是运行前冻结、写死在代码里的规则与阈值。
对照评分时两臂看到的信息、可用的动作权限、判定口径完全一致，因此差异只能来自
方法论本身。

规则集与阈值是公开的：见 [ADR-0013](../../docs/adr/0013-comparative-evaluation.md)。
改动阈值等于换一条基线，必须重新跑全部对照，不能在看到结果后调整。
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from loguru import logger

from aiops.actions.executor import ActionExecutor
from aiops.agent.budget import BudgetExhausted, BudgetLedger
from aiops.agent.context import AgentContext, Progress
from aiops.agent.schemas import EvidenceLink, HypothesisOperation, RequestActionArgs
from aiops.agent.tools import (
    ReadOutcome,
    assess_evidence_core,
    manage_hypotheses_core,
    read_tool,
    request_action_core,
)
from aiops.contracts import (
    ActionOrigin,
    AuditEvent,
    HypothesisStatus,
    Incident,
    Investigation,
    InvestigationStatus,
    InvestigationTask,
    TaskOutcome,
    TaskPhase,
    TerminationCode,
)
from aiops.db import Store
from aiops.investigation.toolbox import QueryRequest, Toolbox
from aiops.report import Report, build_report

RULES_RUNTIME_VERSION = "rules-1"
RULES_MODEL_NAME = "rules-v1"

# 冻结阈值：改动前先改 ADR-0013，并按该 ADR 重跑全部对照。
LOOKBACK_SECONDS = 900
STEP_SECONDS = 30
LATENCY_THRESHOLD_SECONDS = 0.3
OOM_RESTART_THRESHOLD = 3
LOW_MEMORY_LIMIT_MIB = 64
BUSINESS_SIGNAL_EXPRESSION = 'probe_success{job="frontend-probe"}'
LATENCY_EXPRESSION = 'avg_over_time(probe_duration_seconds{job="frontend-probe"}[2m])'

MIB = 1024 * 1024


class Signature:
    """规则能给出的判定类别；与场景真值的对应关系写在 ADR-0013。"""

    RELEASE_REGRESSION = "release_regression"
    SPEC_SHORTAGE = "spec_shortage"
    DEPENDENCY_LATENCY = "dependency_latency"
    POD_LOCAL_STATE = "pod_local_state"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Decision:
    """一次规则判定的结果：类别、结论、依据观测与可选动作。"""

    signature: str
    confirmed: bool
    conclusion: str
    rationale: str
    evidence_ids: list[str] = field(default_factory=list)
    action: str = ""
    action_target: str = ""
    needs_human: list[str] = field(default_factory=list)


def _items(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    items = data.get("items")
    return [item for item in items or [] if isinstance(item, dict)]


def _series_value(data: Any) -> float | None:
    """取最后一条序列的最后一个采样点；没有数据返回 None，不当 0 处理。"""
    if not isinstance(data, dict):
        return None
    series = data.get("series") or []
    for item in series:
        points = item.get("points") or []
        if points:
            value = points[-1][1]
            if isinstance(value, int | float):
                return float(value)
    return None


def _memory_mib(value: str | None) -> float | None:
    if not value:
        return None
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([KMGTP]i?)?\s*", value)
    if match is None:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    scale = {
        "": 1 / MIB,
        "k": 1 / 1024,
        "ki": 1 / 1024,
        "m": 1,
        "mi": 1,
        "g": 1024,
        "gi": 1024,
        "t": 1024 * 1024,
        "ti": 1024 * 1024,
    }
    factor = scale.get(unit)
    return None if factor is None else number * factor


def _pod_containers(pod: dict[str, Any]) -> list[dict[str, Any]]:
    containers = pod.get("containers") or []
    return [item for item in containers if isinstance(item, dict)]


def _restarts(pod: dict[str, Any]) -> int:
    return sum(int(item.get("restart_count") or 0) for item in _pod_containers(pod))


def _all_ready(pods: list[dict[str, Any]]) -> bool:
    if not pods:
        return False
    for pod in pods:
        containers = _pod_containers(pod)
        if not containers or not all(item.get("ready") is True for item in containers):
            return False
    return True


def _oom_killed(pods: list[dict[str, Any]]) -> dict[str, Any] | None:
    for pod in pods:
        for container in _pod_containers(pod):
            last = container.get("last_terminated") or {}
            if last.get("reason") == "OOMKilled":
                return {"pod": pod, "container": container, "last": last}
    return None


def _deployment(facts: Facts) -> dict[str, Any] | None:
    """优先按服务名匹配，其次按 app 标签；找不到就返回 None，不猜。"""
    for deployment in facts.deployments:
        if deployment.get("name") == facts.service:
            return deployment
    for deployment in facts.deployments:
        labels = deployment.get("labels") or {}
        if labels.get("app") == facts.service:
            return deployment
    return None


def _revisions(deployment: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not deployment:
        return []
    rows = [item for item in deployment.get("revisions") or [] if isinstance(item, dict)]
    return sorted(rows, key=lambda item: int(item.get("revision") or 0))


def _log_lines(facts: Facts) -> list[str]:
    lines: list[str] = []
    for item in facts.logs:
        lines.extend(str(line) for line in item.get("lines") or [])
    return lines


def _logs_5xx(facts: Facts) -> bool:
    """目标工作负载自己的日志里出现 5xx；这是业务侧证据，不依赖入口探针。"""
    for line in _log_lines(facts):
        if re.search(r"\b5\d\d\b", line):
            return True
    return False


FAILING_REQUEST = re.compile(r'"(?:GET|POST|PUT|HEAD|DELETE) (/[^ ]*) HTTP/[^"]*" (5\d\d)')


def _failing_path(facts: Facts) -> str:
    """从目标工作负载自己的日志里取出现最多的 5xx 请求路径。

    动作执行器要按业务路径验证恢复；路径不能写死，否则换一个场景就会把
    “恢复成功”误判成失败。取不到就留空，由执行器用默认路径。
    """
    counts: dict[str, int] = {}
    for line in _log_lines(facts):
        match = FAILING_REQUEST.search(line)
        if match is None:
            continue
        counts[match.group(1)] = counts.get(match.group(1), 0) + 1
    if not counts:
        return ""
    return max(counts.items(), key=lambda item: item[1])[0]


def _redirected_service(facts: Facts) -> tuple[str, str] | None:
    """找出选择器指向“另一个工作负载”的 Service。

    健康状态下 Service 与同名 Deployment 对齐；被改成指向代理时，选择器仍然能选中
    某个 Deployment，但那个 Deployment 不叫这个名字。这条偏移是配置事实，不是场景真值。
    """
    for service in facts.all_services:
        selector = service.get("selector") or {}
        if not isinstance(selector, dict) or not selector:
            continue
        matches = [
            deployment
            for deployment in facts.all_deployments
            if all(
                (deployment.get("labels") or {}).get(key) == value
                for key, value in selector.items()
            )
        ]
        if not matches:
            continue
        names = [str(item.get("name") or "") for item in matches]
        if all(name != service.get("name") for name in names):
            return str(service.get("name") or ""), names[0]
    return None


def _service_selector(facts: Facts) -> Any:
    return facts.services[0].get("selector") if facts.services else "未知"


def _parse_time(value: Any) -> datetime | None:
    """时间字段可能是字符串，也可能是 Kubernetes 客户端直接给的 datetime。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _recent(value: Any, window: Any) -> bool:
    parsed = _parse_time(value)
    if parsed is None:
        return False
    return parsed >= window.start


def _pod_started_recently(facts: Facts) -> bool:
    """有 Pod 在取证窗口内启动过：说明近期确实重排过工作负载。

    不能用 ReplicaSet 的创建时间判断“刚刚发布”——Kubernetes 会复用历史 ReplicaSet，
    那次发布的创建时间可能还是几天前。
    """
    for pod in facts.pods:
        for container in _pod_containers(pod):
            if _recent(container.get("started_at"), facts.window):
                return True
    return False


@dataclass
class Facts:
    """一次运行里由规则读取的事实集合；每条事实都带观测 ID，可回查。"""

    service: str
    window: Any
    deployments: list[dict[str, Any]] = field(default_factory=list)
    all_deployments: list[dict[str, Any]] = field(default_factory=list)
    all_services: list[dict[str, Any]] = field(default_factory=list)
    pods: list[dict[str, Any]] = field(default_factory=list)
    services: list[dict[str, Any]] = field(default_factory=list)
    logs: list[dict[str, Any]] = field(default_factory=list)
    probe_success: float | None = None
    probe_duration: float | None = None
    observation_ids: dict[str, str] = field(default_factory=dict)

    def evidence(self, *names: str) -> list[str]:
        return [self.observation_ids[name] for name in names if self.observation_ids.get(name)]


def decide(facts: Facts) -> Decision:
    """冻结的规则链：先硬后软，命中即返回；都不命中时明确给出未知。

    顺序有意固定：发布回归会改模板，必须先排除；OOM 与 Pod 局部状态都会表现为
    重启或请求失败，用重启计数和 last_terminated 区分；依赖延迟靠“本服务自身
    健康但入口变慢”识别。
    """
    deployment = _deployment(facts)
    revisions = _revisions(deployment)
    current = revisions[-1] if revisions else None
    previous = revisions[-2] if len(revisions) >= 2 else None
    image_changed = bool(
        current
        and previous
        and current.get("image")
        and previous.get("image")
        and current.get("image") != previous.get("image")
    )
    # 入口探针失败说明影响面已经扩散到应用入口；只有这种情形才归因到发布回归。
    entry_failing = facts.probe_success is not None and facts.probe_success < 1
    oom = _oom_killed(facts.pods)
    restarts = sum(_restarts(pod) for pod in facts.pods)

    # R1 发布回归：窗口内的镜像变更，且业务同时变差 → 回退到上一 revision。
    if deployment and image_changed and entry_failing and _pod_started_recently(facts):
        evidence = facts.evidence("deployments")
        if facts.probe_success is not None:
            evidence += facts.evidence("business")
        else:
            evidence += facts.evidence("logs")
        return Decision(
            signature=Signature.RELEASE_REGRESSION,
            confirmed=True,
            conclusion=(
                f"{deployment.get('name')} 在最近一次发布里把镜像从 "
                f"{previous.get('image')} 改成 {current.get('image')}，"
                f"发布后业务请求开始失败；回退到上一 revision 可以恢复。"
            ),
            rationale=(
                f"revision {current.get('revision')} 的镜像与更早的 revision "
                f"{previous.get('revision')} 不同，Pod 在取证窗口内重新启动，"
                f"并且入口成功率降到 {facts.probe_success}。"
            ),
            evidence_ids=evidence,
            action="rollout_undo",
            action_target=str(deployment.get("name") or facts.service),
        )

    # R2 规格不足：持续 OOMKilled 且模板限额低于下限 → 只判定，不动作。
    if oom is not None and restarts >= OOM_RESTART_THRESHOLD:
        container = oom["container"]
        limits = (container.get("resources") or {}).get("limits") or {}
        limit_mib = _memory_mib(limits.get("memory"))
        if limit_mib is not None and limit_mib <= LOW_MEMORY_LIMIT_MIB:
            return Decision(
                signature=Signature.SPEC_SHORTAGE,
                confirmed=True,
                conclusion=(
                    f"{facts.service} 的容器内存限额是 {limits.get('memory')}，"
                    f"进程反复超过限额被 OOMKilled（累计重启 {restarts} 次）；"
                    "这是规格不足，不属于任何已注册动作能修的范围。"
                ),
                rationale=(
                    f"Pod {oom['pod'].get('name')} 的 last_terminated.reason=OOMKilled，"
                    f"restart_count 合计 {restarts} ≥ {OOM_RESTART_THRESHOLD}，"
                    f"模板限额 {limits.get('memory')} ≤ {LOW_MEMORY_LIMIT_MIB}Mi。"
                ),
                evidence_ids=facts.evidence("pods", "deployments", "business"),
                needs_human=[f"评估 {facts.service} 的内存限额与真实用量，按容量调整规格"],
            )

    # R3 依赖链路：本服务自身干净，但入口延迟持续偏高 → 判定链路，不动作。
    if (
        facts.probe_duration is not None
        and facts.probe_duration >= LATENCY_THRESHOLD_SECONDS
        and _all_ready(facts.pods)
        and restarts == 0
        and oom is None
        and not image_changed
    ):
        redirect = _redirected_service(facts)
        if redirect is not None:
            service_name, target = redirect
            detail = (
                f"Service {service_name} 的选择器已经指向 {target}，"
                f"请求因此走上了 {service_name} 访问链路的新跳点"
            )
        else:
            detail = "变慢发生在访问下游依赖的链路上，不是本服务崩溃"
        return Decision(
            signature=Signature.DEPENDENCY_LATENCY,
            confirmed=True,
            conclusion=(
                f"{facts.service} 自身 Pod 全部就绪、没有重启，也没有资源或发布变更，"
                f"但入口延迟达到 {round(facts.probe_duration * 1000)}ms；"
                f"{detail}。"
            ),
            rationale=(
                f"probe_duration 2 分钟均值 {facts.probe_duration:.3f}s ≥ "
                f"{LATENCY_THRESHOLD_SECONDS}s，重启计数 {restarts}=0，"
                "镜像与模板未变。"
            ),
            evidence_ids=facts.evidence("pods", "deployments", "latency"),
            needs_human=["定位变慢的依赖方并在其侧处置；本服务不做写操作"],
        )

    # R4 Pod 局部状态：平台侧全绿而业务请求持续失败 → 重建 Pod。
    if (
        deployment
        and not image_changed
        and oom is None
        and _all_ready(facts.pods)
        and restarts == 0
        and _logs_5xx(facts)
        and (facts.probe_success is None or facts.probe_success >= 1)
    ):
        pod = facts.pods[0] if facts.pods else None
        template = deployment.get("containers") or []
        if pod is not None and template:
            return Decision(
                signature=Signature.POD_LOCAL_STATE,
                confirmed=True,
                conclusion=(
                    f"{facts.service} 的 Pod {pod.get('name')} 就绪且没有重启，"
                    "镜像、模板和 revision 都没有变化，但业务请求持续返回 5xx；"
                    "故障在 Pod 自身的运行时状态，重建 Pod 能拿到干净状态。"
                ),
                rationale=(
                    "Pod ready=true、restart_count=0，Deployment 模板未变，日志出现 5xx；"
                    f"服务选择器 {_service_selector(facts)}。"
                ),
                evidence_ids=facts.evidence("pods", "logs", "deployments"),
                action="restart_pod",
                action_target=str(pod.get("name") or ""),
            )

    return Decision(
        signature=Signature.UNKNOWN,
        confirmed=False,
        conclusion="",
        rationale=(
            "规则链没有命中任何已知签名：发布未变、无 OOMKilled、Pod 就绪、"
            "入口延迟与成功率没有超过阈值。"
        ),
        evidence_ids=facts.evidence("deployments", "pods", "business", "latency"),
    )


class RulesInvestigator:
    """规则基线的一次运行；接口与 `LangChainInvestigation.run()` 对齐。"""

    def __init__(
        self,
        *,
        store: Store,
        incident: Incident,
        investigation: Investigation,
        toolbox: Toolbox,
        executor: ActionExecutor | None,
        runtime_path: Any,
        max_observation_chars: int = 8000,
        tool_timeout_seconds: float = 10.0,
    ) -> None:
        self._store = store
        self._incident = incident
        self._investigation = investigation
        self._toolbox = toolbox
        self._executor = executor
        self._runtime_path = runtime_path
        self._max_observation_chars = max_observation_chars
        self._tool_timeout_seconds = tool_timeout_seconds
        self._needs_human: list[str] = []

    # ------------------------------------------------------------------ 装配

    async def _ensure_task(self) -> None:
        existing = await self._store.get_task(self._investigation.id)
        if existing is not None:
            return
        await self._store.create_task(
            InvestigationTask(
                investigation_id=self._investigation.id,
                phase=TaskPhase.RUNNING,
                runtime_version=RULES_RUNTIME_VERSION,
                calendar_started_at=self._investigation.started_at,
            )
        )

    async def _context(self) -> AgentContext:
        task = await self._store.get_task(self._investigation.id)
        if task is None:
            raise LookupError(f"任务记录创建失败：{self._investigation.id}")
        return AgentContext(
            store=self._store,
            incident=self._incident,
            investigation_id=self._investigation.id,
            window=self._incident.time_range,
            budget=BudgetLedger.restore(
                store=self._store,
                investigation_id=self._investigation.id,
                budget=self._investigation.budget,
                task=task,
            ),
            toolbox=self._toolbox,
            executor=self._executor,
            runtime_path=self._runtime_path,
            max_observation_chars=self._max_observation_chars,
            tool_timeout_seconds=self._tool_timeout_seconds,
            model_reserve_seconds=0.0,
            progress=Progress(),
        )

    # ------------------------------------------------------------------ 取证

    def _request(self, tool: str, **kwargs: Any) -> QueryRequest:
        return QueryRequest(tool=tool, **kwargs)

    async def _read(
        self, ctx: AgentContext, call_id: str, tool: str, request: QueryRequest
    ) -> ReadOutcome:
        return await read_tool(ctx, call_id, tool_name=tool, request=request)

    async def _gather(self, ctx: AgentContext) -> Facts:
        """固定顺序、固定参数的取证；每条读取都会进工具账本与观测表。"""
        facts = Facts(service=self._incident.service, window=ctx.window)
        selector = f"app={self._incident.service}"

        deployments = await self._read(
            ctx,
            "rules-01-deployments",
            "kubernetes.list_deployments",
            self._request("kubernetes.list_deployments", selector=selector),
        )
        facts.deployments = _items(deployments.data)
        if deployments.observation_id:
            facts.observation_ids["deployments"] = deployments.observation_id

        pods = await self._read(
            ctx,
            "rules-02-pods",
            "kubernetes.list_pods",
            self._request("kubernetes.list_pods", selector=selector),
        )
        facts.pods = _items(pods.data)
        if pods.observation_id:
            facts.observation_ids["pods"] = pods.observation_id

        all_deployments = await self._read(
            ctx,
            "rules-03-deployments-all",
            "kubernetes.list_deployments",
            self._request("kubernetes.list_deployments", selector=""),
        )
        facts.all_deployments = _items(all_deployments.data)

        all_services = await self._read(
            ctx,
            "rules-04-services-all",
            "kubernetes.list_services",
            self._request("kubernetes.list_services", selector=""),
        )
        facts.all_services = _items(all_services.data)

        services = await self._read(
            ctx,
            "rules-05-services",
            "kubernetes.list_services",
            self._request("kubernetes.list_services", selector=self._incident.service),
        )
        facts.services = _items(services.data)
        if services.observation_id:
            facts.observation_ids["services"] = services.observation_id

        pod_name = str(facts.pods[0].get("name")) if facts.pods else ""
        if pod_name:
            logs = await self._read(
                ctx,
                "rules-07-logs",
                "kubernetes.list_logs",
                self._request("kubernetes.list_logs", selector=selector, involved_name=pod_name),
            )
            facts.logs = _items(logs.data)
            if logs.observation_id:
                facts.observation_ids["logs"] = logs.observation_id

        business = await self._read(
            ctx,
            "rules-08-business",
            "prometheus.query_range",
            self._request(
                "prometheus.query_range",
                expression=BUSINESS_SIGNAL_EXPRESSION,
                lookback_seconds=LOOKBACK_SECONDS,
                step_seconds=STEP_SECONDS,
            ),
        )
        facts.probe_success = _series_value(business.data)
        if business.observation_id and facts.probe_success is not None:
            facts.observation_ids["business"] = business.observation_id

        latency = await self._read(
            ctx,
            "rules-09-latency",
            "prometheus.query_range",
            self._request(
                "prometheus.query_range",
                expression=LATENCY_EXPRESSION,
                lookback_seconds=LOOKBACK_SECONDS,
                step_seconds=STEP_SECONDS,
            ),
        )
        facts.probe_duration = _series_value(latency.data)
        if latency.observation_id and facts.probe_duration is not None:
            facts.observation_ids["latency"] = latency.observation_id
        return facts

    # ------------------------------------------------------------------ 记录

    async def _record_hypothesis(self, ctx: AgentContext, decision: Decision, facts: Facts) -> str:
        """把判定写成正常假设记录；规则臂不跳过结构化记录。"""
        statement = (
            decision.conclusion
            if decision.conclusion
            else f"未命中已知签名，需要人工判断：{facts.service}"
        )
        operations = [
            HypothesisOperation(
                op="create",
                temp_id="h1",
                statement=statement,
                reason=f"规则链命中 {decision.signature}：{decision.rationale}",
            )
        ]
        if decision.confirmed:
            operations.append(
                HypothesisOperation(
                    op="update_status",
                    temp_id="h1",
                    status=HypothesisStatus.SUPPORTED.value,
                    reason="规则所需信号全部成立",
                )
            )
        message = await manage_hypotheses_core(
            ctx, "rules-10-hypothesis", operations, origin="rules"
        )
        hypothesis_id = _hypothesis_id(message)
        if not hypothesis_id:
            raise RuntimeError(f"规则基线未能写入假设：{message[:200]}")
        usable = [evidence_id for evidence_id in decision.evidence_ids if evidence_id]
        if usable:
            await assess_evidence_core(
                ctx,
                "rules-11-evidence",
                [
                    EvidenceLink(
                        observation_id=evidence_id,
                        hypothesis_id=hypothesis_id,
                        relation="supporting",
                        rationale=decision.rationale,
                    ).model_dump()
                    for evidence_id in usable
                ],
                [],
            )
        return hypothesis_id

    async def _maybe_act(
        self, ctx: AgentContext, decision: Decision, hypothesis_id: str, facts: Facts
    ) -> str:
        if not decision.action:
            return ""
        service = self._incident.service
        path = _failing_path(facts) or "/"
        args = RequestActionArgs(
            action="restart_pod" if decision.action == "restart_pod" else "rollout_undo",
            purpose="remediation",
            target_pod=decision.action_target if decision.action == "restart_pod" else "",
            target_deployment=(decision.action_target if decision.action == "rollout_undo" else ""),
            service=service,
            path=path,
            reason=decision.rationale,
            hypothesis_id=hypothesis_id,
            evidence_ids=[item for item in decision.evidence_ids if item],
            prediction=(
                "重建后业务请求恢复 200"
                if decision.action == "restart_pod"
                else "回退后业务请求与入口成功率恢复"
            ),
            absence_meaning="没有恢复说明判定或动作都不适用，需要人工核实",
            business_target=f"{facts.service}{path}",
        )
        return await request_action_core(
            ctx,
            "rules-12-action",
            args,
            actor="rules-baseline",
            origin=ActionOrigin.RULES,
            pause_for_approval=False,
        )

    async def _finish(
        self,
        ctx: AgentContext,
        decision: Decision,
        action_message: str,
    ) -> Report:
        finished_at = datetime.now(UTC)
        task = await self._store.get_task(self._investigation.id)
        waiting = _action_status(action_message) == "waiting_approval"
        recovered = task is not None and task.recovered_at is not None

        if waiting:
            # 需要人工审批的动作：与 Agent 臂一致，停在等待审批，不替操作者批准。
            await self._audit(
                TaskOutcome.NEEDS_HUMAN,
                TerminationCode.WAITING_APPROVAL,
                decision.rationale,
            )
            return await self._report(ctx, model_summary_completed=True)

        if recovered:
            status = InvestigationStatus.COMPLETED
            outcome = TaskOutcome.RECOVERED
            code = TerminationCode.BUSINESS_RECOVERED
            conclusion = decision.conclusion or None
        elif decision.confirmed:
            status = InvestigationStatus.COMPLETED
            outcome = TaskOutcome.DIAGNOSIS_ONLY
            code = TerminationCode.NO_APPLICABLE_ACTION
            conclusion = decision.conclusion
        else:
            status = InvestigationStatus.INCONCLUSIVE
            outcome = TaskOutcome.INCONCLUSIVE
            code = TerminationCode.INSUFFICIENT_EVIDENCE
            conclusion = None

        await self._store.set_investigation_status(
            self._investigation.id,
            status,
            conclusion=conclusion,
            termination_reason=None if conclusion else "规则链未命中，未形成可引用的根因候选",
            finished_at=finished_at,
        )
        await self._store.finish_task(
            self._investigation.id,
            outcome=outcome,
            termination_code=code.value,
            finished_at=finished_at,
        )
        await self._audit(outcome, code, decision.rationale)
        return await self._report(ctx, model_summary_completed=True)

    async def _audit(self, outcome: TaskOutcome, code: TerminationCode, detail: str) -> None:
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=self._investigation.id,
                    actor="rules-baseline",
                    event="investigation.rules",
                    occurred_at=datetime.now(UTC),
                    payload={
                        "outcome": outcome.value,
                        "termination_code": code.value,
                        "detail": detail[:500],
                        "runtime_version": RULES_RUNTIME_VERSION,
                    },
                )
            ]
        )

    async def _report(self, ctx: AgentContext, *, model_summary_completed: bool) -> Report:
        return await build_report(
            self._store,
            self._investigation.id,
            remaining_budget=ctx.budget.remaining(),
            needs_human=list(self._needs_human),
            calendar_seconds=ctx.budget.calendar_seconds(),
            model_summary_completed=model_summary_completed,
        )

    # ------------------------------------------------------------------ 运行

    async def run(self) -> Report:
        await self._ensure_task()
        ctx = await self._context()
        try:
            await ctx.budget.start()
            facts = await self._gather(ctx)
            decision = decide(facts)
            self._needs_human = list(decision.needs_human)
            hypothesis_id = await self._record_hypothesis(ctx, decision, facts)
            action_message = await self._maybe_act(ctx, decision, hypothesis_id, facts)
            return await self._finish(ctx, decision, action_message)
        except BudgetExhausted as exc:
            return await self._abort(ctx, TerminationCode.BUDGET_EXHAUSTED, str(exc))
        except Exception as exc:  # noqa: BLE001 - 运行故障必须落成可追踪终态
            logger.exception("规则基线运行异常：{}", exc.__class__.__name__)
            return await self._abort(
                ctx, TerminationCode.RUNTIME_ERROR, f"规则基线异常：{exc.__class__.__name__}"
            )

    async def _abort(self, ctx: AgentContext, code: TerminationCode, detail: str) -> Report:
        finished_at = datetime.now(UTC)
        await self._store.set_investigation_status(
            self._investigation.id,
            InvestigationStatus.ABORTED,
            conclusion=None,
            termination_reason=detail,
            finished_at=finished_at,
        )
        await self._store.finish_task(
            self._investigation.id,
            outcome=TaskOutcome.ABORTED,
            termination_code=code.value,
            finished_at=finished_at,
        )
        await self._audit(TaskOutcome.ABORTED, code, detail)
        return await self._report(ctx, model_summary_completed=False)


def _action_status(message: str) -> str:
    """读动作工具返回的结构化状态；无法解析时按失败处理，不假装成功。"""
    import json

    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return ""
    return str(payload.get("status") or "")


def _hypothesis_id(message: str) -> str:
    """从假设工具的返回消息里取第一条假设 ID；取不到就当作失败。"""
    import json

    try:
        payload = json.loads(message)
    except (TypeError, ValueError):
        return ""
    for item in payload.get("hypotheses") or []:
        if isinstance(item, dict) and item.get("id"):
            return str(item["id"])
    return ""


__all__ = [
    "LATENCY_THRESHOLD_SECONDS",
    "LOW_MEMORY_LIMIT_MIB",
    "OOM_RESTART_THRESHOLD",
    "RULES_MODEL_NAME",
    "RULES_RUNTIME_VERSION",
    "Decision",
    "Facts",
    "RulesInvestigator",
    "Signature",
    "decide",
]
