"""固定规则基线：零模型调用，用固定查询与规则表产生可对照的调查记录。"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from aiops.actions.executor import AutoExecutionError
from aiops.actions.policy import ALLOWED_KINDS, POLICY_VERSION
from aiops.actions.rollout_undo import ActionError
from aiops.contracts import (
    ActionOrigin,
    AuditEvent,
    Evidence,
    Hypothesis,
    Incident,
    Investigation,
    InvestigationStatus,
    TimeRange,
    ToolCall,
    ToolError,
    ToolResult,
)
from aiops.investigation.runtime import ActionProposer, Report
from aiops.investigation.schemas import QueryRequest
from aiops.investigation.toolbox import ToolboxProtocol, observation
from aiops.tools.base import ToolContext

RULES_VERSION = "rules-1"
ACTOR = "rules-baseline"

HYPOTHESES = (
    (
        "resource",
        "容器因资源配置不足或进程按错误码退出",
        ["last_terminated.reason 与 exit_code", "restart_count"],
    ),
    (
        "change",
        "观察窗内的工作负载模板变更或新增工作负载",
        ["revision 与上一版的镜像和内存规格差异", "revision 创建时间落在观察窗"],
    ),
    (
        "dependency",
        "入口服务访问依赖时出现连接、解析或超时错误",
        ["入口服务日志中的连接错误", "业务探针成功值"],
    ),
)

DEPENDENCY_MARKERS = (
    "connection refused",
    "no such host",
    "econnrefused",
    "etimedout",
    "i/o timeout",
    "name resolution",
    "dial tcp",
    "temporarily unavailable",
)

ERROR_MARKERS = ("error", "failed", "exception", "unavailable", "refused", "timeout", "500")


class StoreProtocol(Protocol):
    async def get_investigation(self, investigation_id: str) -> Investigation | None: ...
    async def save_hypothesis(self, hypothesis: Hypothesis) -> None: ...
    async def set_hypothesis_status(self, hypothesis_id: str, status: Any) -> None: ...
    async def save_tool_call(self, call: ToolCall) -> None: ...
    async def save_evidence(self, evidence: Evidence) -> None: ...
    async def set_evidence_relation(self, evidence_id: str, relation: str) -> None: ...
    async def add_usage(
        self, investigation_id: str, *, tool_calls: int = 0, model_calls: int = 0
    ) -> None: ...
    async def set_investigation_status(
        self,
        investigation_id: str,
        status: InvestigationStatus,
        *,
        conclusion: str | None = None,
        termination_reason: str | None = None,
        finished_at: Any = None,
    ) -> None: ...
    async def append_audit(self, events: list[AuditEvent]) -> None: ...


@dataclass(frozen=True)
class _Query:
    key: str
    hypothesis: int
    request: QueryRequest


@dataclass
class _Hit:
    rule: str
    hypothesis: str
    conclusion: str
    action: str
    deployment: str = ""
    evidence_keys: tuple[str, ...] = ()
    signals: dict[str, Any] = field(default_factory=dict)


def _text(value: Any) -> str:
    return value if isinstance(value, str) else ""


def _as_time(value: Any) -> datetime | None:
    """统一成带时区的 UTC 时间；集群接口偶尔省略偏移量，不能让它和窗内时间直接比较。"""
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _revisions(deployment: dict[str, Any]) -> list[dict[str, Any]]:
    rows = [row for row in deployment.get("revisions") or [] if isinstance(row, dict)]
    return sorted(rows, key=lambda row: row.get("revision") or 0)


def _template(row: dict[str, Any]) -> tuple[Any, Any, Any]:
    return (row.get("image"), row.get("memory_limit"), row.get("memory_request"))


def _shape(row: dict[str, Any]) -> tuple[Any, Any]:
    return (row.get("image"), row.get("memory_limit"))


def _live_template(deployment: dict[str, Any]) -> tuple[Any, Any, Any] | None:
    """只取首个容器，与工具对 ReplicaSet 模板的裁剪口径一致。"""
    containers = deployment.get("containers") or []
    if not containers:
        return None
    container = containers[0]
    return (
        container.get("image"),
        (container.get("limits") or {}).get("memory"),
        (container.get("requests") or {}).get("memory"),
    )


def _templates(deployment: dict[str, Any]) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """当前模板取 Deployment 的实时定义，而不是 revision 号最大的 ReplicaSet。

    rollout undo 会复用旧 ReplicaSet，旧 RS 的 revision 号小于被回退掉的 RS；
    只看最大 revision 会把已经不在用的坏模板当成当前模板，回退方向随之反过来。
    先按镜像、限额与请求精确匹配，再退到镜像和限额，最后才退回最高 revision。
    """
    revisions = _revisions(deployment)
    if not revisions:
        return None, None
    live = _live_template(deployment)
    exact = [row for row in revisions if live is not None and _template(row) == live]
    if exact:
        current = max(exact, key=lambda row: row.get("revision") or 0)
    elif live is not None:
        same_shape = [row for row in revisions if _shape(row) == (live[0], live[1])]
        current = (
            max(same_shape, key=lambda row: row.get("revision") or 0)
            if same_shape
            else revisions[-1]
        )
    else:
        current = revisions[-1]
    key = _template(current)
    previous = next(
        (row for row in reversed(revisions) if row is not current and _template(row) != key),
        None,
    )
    if previous is None:
        index = revisions.index(current)
        previous = revisions[index - 1] if index > 0 else None
    return current, previous


def _oom_finished_at(pod: dict[str, Any], window: TimeRange) -> datetime:
    """窗内最近一次 OOMKilled 的终止时间；没有就返回最早时间，便于稳定排序。"""
    times = []
    for container in pod.get("containers") or []:
        last = container.get("last_terminated") or {}
        if last.get("reason") != "OOMKilled" and container.get("reason") != "OOMKilled":
            continue
        finished = _as_time(last.get("finished_at"))
        if finished is not None and window.start <= finished <= window.end:
            times.append(finished)
    return max(times) if times else datetime.min.replace(tzinfo=UTC)


def _oom_container(pod: dict[str, Any], window: TimeRange) -> dict[str, Any] | None:
    """只认落在观察窗内的 OOMKilled，取最近一次；历史单次退出不能当成当前根因。"""
    latest = None
    latest_at = None
    for container in pod.get("containers") or []:
        last = container.get("last_terminated") or {}
        if last.get("reason") != "OOMKilled" and container.get("reason") != "OOMKilled":
            continue
        finished = _as_time(last.get("finished_at"))
        if finished is None or not window.start <= finished <= window.end:
            continue
        if latest_at is None or finished > latest_at:
            latest, latest_at = container, finished
    return latest


_MEMORY_SUFFIX = {
    "": 1,
    "K": 10**3,
    "M": 10**6,
    "G": 10**9,
    "T": 10**12,
    "P": 10**15,
    "E": 10**18,
    "Ki": 2**10,
    "Mi": 2**20,
    "Gi": 2**30,
    "Ti": 2**40,
    "Pi": 2**50,
    "Ei": 2**60,
}
_MEMORY_VALUE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([KMGTPE]i?)?$")


def _memory_bytes(value: Any) -> int | None:
    """把 Kubernetes 内存数量解析成字节；解析不了就返回 None，不能凭字符串猜大小。"""
    match = _MEMORY_VALUE.match(_text(value).strip())
    if match is None:
        return None
    return int(float(match.group(1)) * _MEMORY_SUFFIX[match.group(2) or ""])


def _selector_match(pod: dict[str, Any], deployment: dict[str, Any]) -> bool:
    selector = deployment.get("selector") or {}
    if not isinstance(selector, dict) or not selector:
        return False
    labels = pod.get("labels") or {}
    return all(labels.get(key) == value for key, value in selector.items())


def _deployment_for_pod(pod: dict[str, Any], deployments: list[dict[str, Any]]) -> dict | None:
    matched = [item for item in deployments if _selector_match(pod, item)]
    if matched:
        return max(matched, key=lambda item: len(item.get("selector") or {}))
    # 选择器存在却不匹配时，Pod 不属于该 Deployment（例如同名 Job 的 Pod）；
    # 只在 Deployment 没有选择器时才退回 app 标签。
    app = (pod.get("labels") or {}).get("app")
    for deployment in deployments:
        if deployment.get("name") == app and not deployment.get("selector"):
            return deployment
    return None


def _fresh_start(pod: dict[str, Any], window: TimeRange) -> bool:
    """新 Pod 的容器在观察窗内首次启动：没有重启计数，也没有上一次终止。

    回退或重放旧模板时会复用旧 ReplicaSet，创建时间停在过去；
    而旧版本 Pod 因依赖故障重启时 started_at 同样落在窗内。
    用“无重启历史”把复用的新 Pod 和原地重启区分开。
    """
    for container in pod.get("containers") or []:
        start = _as_time(container.get("started_at"))
        if start is None or not window.start <= start <= window.end:
            continue
        if (container.get("restart_count") or 0) == 0 and not container.get("last_terminated"):
            return True
    return False


def _started_at(pod: dict[str, Any], window: TimeRange) -> datetime | None:
    """窗内最晚的容器启动时间；先按容器过滤，避免窗后启动的 sidecar 掩盖窗内主容器。"""
    starts = [
        start
        for container in pod.get("containers") or []
        if (start := _as_time(container.get("started_at"))) is not None
        and window.start <= start <= window.end
    ]
    return max(starts) if starts else None


def _pods_for(deployment: dict[str, Any], pods: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 Deployment 选择器归属 Pod；没有选择器时才退回 app 标签，名字前缀不能作为归属依据。"""
    selector = deployment.get("selector") or {}
    if selector:
        return [pod for pod in pods if _selector_match(pod, deployment)]
    name = _text(deployment.get("name"))
    return [pod for pod in pods if (pod.get("labels") or {}).get("app") == name]


def _log_hint(name: str, logs: list[dict[str, Any]]) -> str:
    """入口日志里同时出现错误标记和该工作负载名称时，作为变更的失败关联。

    按完整词匹配，短名字（cart）也能命中；避免把 cartservice 当成 cart，
    也避免给短名字设长度门槛后漏掉错误关联。
    """
    compact_name = re.sub(r"[^a-z0-9]", "", name.lower())
    if not compact_name:
        return ""
    short = len(compact_name) < 8
    pattern = re.compile(rf"(?<![a-z0-9]){re.escape(compact_name)}(?![a-z0-9])")
    for item in logs:
        for line in item.get("lines") or []:
            low = str(line).lower()
            if not any(marker in low for marker in ERROR_MARKERS):
                continue
            if short:
                if pattern.search(low):
                    return str(line)[:200]
                continue
            compact = re.sub(r"[^a-z0-9]", "", low)
            if any(compact_name[:size] in compact for size in range(len(compact_name), 7, -1)):
                return str(line)[:200]
    return ""


def _probe_value(dataset: Any) -> float | None:
    if not isinstance(dataset, dict):
        return None
    for series in dataset.get("series") or []:
        points = series.get("points") or []
        if not points:
            continue
        raw = points[-1][1]
        try:
            return float(raw)
        except (TypeError, ValueError):
            return None
    return None


class RulesBaseline:
    """固定版本、固定顺序的规则调查；任何结论都必须来自本轮取到的证据。"""

    def __init__(
        self,
        *,
        store: StoreProtocol,
        toolbox: ToolboxProtocol,
        incident: Incident,
        investigation: Investigation,
        observation_window: TimeRange,
        runtime_path: Path,
        proposer: ActionProposer | None = None,
        tool_timeout_seconds: float = 10.0,
        max_observation_chars: int = 4000,
    ) -> None:
        self._store = store
        self._toolbox = toolbox
        self._incident = incident
        self._investigation = investigation
        self._investigation_id = investigation.id
        self._budget = investigation.budget
        self._window = observation_window
        self._runtime_path = runtime_path
        self._proposer = proposer
        self._tool_timeout = tool_timeout_seconds
        self._max_observation_chars = max_observation_chars
        self._used_tool_calls = investigation.usage.tool_calls
        self._deadline = time.monotonic() + investigation.budget.max_wall_clock_seconds

    async def run(self) -> Report:
        await self._audit(
            "baseline.started",
            {
                "rules_version": RULES_VERSION,
                "budget": self._budget.model_dump(mode="json"),
                "tools": [item.request.tool for item in self._plan()],
                "policy_version": POLICY_VERSION,
                "authorized_kinds": sorted(kind.value for kind in ALLOWED_KINDS),
                "service": self._incident.service,
                "namespace": self._incident.namespace,
            },
        )
        hypotheses = await self._create_hypotheses()
        evidence_keys: dict[str, list[str]] = {}
        datasets: dict[str, Any] = {}
        truncated: set[str] = set()
        failures: list[dict[str, Any]] = []
        aborted: str | None = None
        for item in self._plan():
            block = self._tool_budget_block()
            if block is not None:
                aborted = block
                break
            context = self._tool_context()
            started_at = datetime.now(UTC)
            # 工具自己的截止时间不被所有实现遵守（知识库查询就没有），这里再加一层超时。
            # wait_for 只能协作取消，工具可能仍在后台跑；单次超时按查询失败记录，墙钟耗尽才中止。
            timeout = max(0.05, context.deadline - time.monotonic()) + 0.05
            try:
                result = await asyncio.wait_for(
                    self._toolbox.run(context, self._window, item.request), timeout=timeout
                )
            except TimeoutError:
                finished_at = datetime.now(UTC)
                if time.monotonic() >= self._deadline:
                    aborted = "wall_clock"
                    break
                result = ToolResult(
                    tool_call_id=f"tc-{uuid.uuid4().hex[:12]}",
                    source=item.request.tool,
                    query=item.request.model_dump(mode="json"),
                    time_range=self._window,
                    collected_at=finished_at,
                    error=ToolError(
                        code="timeout",
                        message=f"工具调用超过 {self._tool_timeout:g} 秒未返回",
                        retryable=True,
                    ),
                )
            finished_at = datetime.now(UTC)
            await self._record_tool_call(item.request, result, started_at, finished_at)
            if result.error is not None:
                failures.append(
                    {
                        "query": item.key,
                        "tool": item.request.tool,
                        "error": result.error.code,
                        "message": result.error.message,
                    }
                )
                continue
            datasets[item.key] = result.data
            if result.truncated:
                truncated.add(item.key)
            evidence_keys[item.key] = [
                await self._record_evidence(item, hypotheses[item.hypothesis]["id"], result)
            ]

        if aborted is None and time.monotonic() >= self._deadline:
            aborted = "wall_clock"
        if aborted is not None:
            return await self._finish(
                InvestigationStatus.ABORTED,
                conclusion=None,
                termination=f"预算耗尽：{aborted}",
                hypotheses=hypotheses,
                evidence_keys=evidence_keys,
                proposal_id=None,
            )

        hit = self._decide(datasets, failures, truncated)
        if time.monotonic() >= self._deadline:
            return await self._finish(
                InvestigationStatus.ABORTED,
                conclusion=None,
                termination="预算耗尽：wall_clock",
                hypotheses=hypotheses,
                evidence_keys=evidence_keys,
                proposal_id=None,
            )
        if hit is None:
            return await self._finish(
                InvestigationStatus.INCONCLUSIVE,
                conclusion=None,
                termination="固定规则未命中，证据不足以判定根因",
                hypotheses=hypotheses,
                evidence_keys=evidence_keys,
                proposal_id=None,
            )

        await self._apply(hit, hypotheses, evidence_keys)
        # 记账写入也可能耗尽墙钟预算；超出后不能再产生新的提案。
        if time.monotonic() >= self._deadline:
            return await self._finish(
                InvestigationStatus.ABORTED,
                conclusion=None,
                termination="预算耗尽：wall_clock",
                hypotheses=hypotheses,
                evidence_keys=evidence_keys,
                proposal_id=None,
            )
        supporting = [
            evidence_id for key in hit.evidence_keys for evidence_id in evidence_keys.get(key, [])
        ]
        proposal_id = None
        if hit.action == "rollout_undo" and hit.deployment:
            conclusion = hit.conclusion
            if supporting:
                conclusion = f"{conclusion} 证据：{', '.join(supporting)}。"
            else:
                conclusion = f"{conclusion} 没有可作为动作依据的集群证据。"
            proposal_id = await self._propose_rollback(hit, supporting)
        else:
            conclusion = hit.conclusion
        return await self._finish(
            InvestigationStatus.COMPLETED,
            conclusion=conclusion,
            termination=None,
            hypotheses=hypotheses,
            evidence_keys=evidence_keys,
            proposal_id=proposal_id,
        )

    def _plan(self) -> list[_Query]:
        lookback = max(60, min(3600, int((self._window.end - self._window.start).total_seconds())))
        service = self._incident.service
        symptoms = self._incident.symptoms[:500]
        return [
            _Query(
                "deployments",
                1,
                QueryRequest(
                    hypothesis=1,
                    tool="kubernetes.list_deployments",
                    purpose="列出全部工作负载与最近模板，检查观察窗内的变更",
                    expression="",
                    lookback_seconds=lookback,
                    step_seconds=30,
                    selector="",
                    involved_name="",
                ),
            ),
            _Query(
                "pods",
                0,
                QueryRequest(
                    hypothesis=0,
                    tool="kubernetes.list_pods",
                    purpose="检查容器终止原因、退出码与重启次数",
                    expression="",
                    lookback_seconds=lookback,
                    step_seconds=30,
                    selector="",
                    involved_name="",
                ),
            ),
            _Query(
                "events",
                1,
                QueryRequest(
                    hypothesis=1,
                    tool="kubernetes.list_events",
                    purpose="核对探针失败、驱逐与调度事件",
                    expression="",
                    lookback_seconds=lookback,
                    step_seconds=30,
                    selector="",
                    involved_name="",
                ),
            ),
            _Query(
                "logs",
                2,
                QueryRequest(
                    hypothesis=2,
                    tool="kubernetes.list_logs",
                    purpose="读取入口服务日志中的依赖访问错误",
                    expression="",
                    lookback_seconds=lookback,
                    step_seconds=30,
                    selector=f"app={service}",
                    involved_name="",
                ),
            ),
            _Query(
                "probe",
                2,
                QueryRequest(
                    hypothesis=2,
                    tool="prometheus.query_range",
                    purpose="读取业务探针成功值，作为独立业务信号",
                    expression='probe_success{job="frontend-probe"}',
                    lookback_seconds=max(60, min(3600, lookback)),
                    step_seconds=30,
                    selector="",
                    involved_name="",
                ),
            ),
            _Query(
                "runbooks",
                0,
                QueryRequest(
                    hypothesis=0,
                    tool="knowledge.list_runbooks",
                    purpose="按症状检索固定 runbook，仅作参考",
                    expression=symptoms,
                    lookback_seconds=lookback,
                    step_seconds=30,
                    selector=service,
                    involved_name="",
                ),
            ),
        ]

    async def _create_hypotheses(self) -> list[dict[str, Any]]:
        hypotheses = []
        for key, statement, expected in HYPOTHESES:
            hypothesis = Hypothesis(
                id=f"hyp-{uuid.uuid4().hex[:12]}",
                investigation_id=self._investigation_id,
                statement=statement,
                expected_observations=list(expected),
                origin="runbook",
            )
            await self._store.save_hypothesis(hypothesis)
            hypotheses.append(
                {
                    "key": key,
                    "id": hypothesis.id,
                    "statement": statement,
                    "status": hypothesis.status.value,
                }
            )
        return hypotheses

    def _tool_budget_block(self) -> str | None:
        if self._used_tool_calls >= self._budget.max_tool_calls:
            return "tool_calls"
        if time.monotonic() >= self._deadline:
            return "wall_clock"
        return None

    def _tool_context(self) -> ToolContext:
        remaining = self._deadline - time.monotonic()
        return ToolContext(
            investigation_id=self._investigation_id,
            cluster=self._incident.cluster,
            namespace=self._incident.namespace,
            runtime_path=self._runtime_path,
            deadline=time.monotonic() + min(self._tool_timeout, max(0.0, remaining)),
        )

    async def _record_tool_call(
        self, request: QueryRequest, result: ToolResult, started_at, finished_at
    ) -> None:
        self._used_tool_calls += 1
        await self._store.add_usage(self._investigation_id, tool_calls=1)
        await self._store.save_tool_call(
            ToolCall(
                id=result.tool_call_id,
                investigation_id=self._investigation_id,
                tool=request.tool,
                parameters=request.model_dump(),
                time_range=result.time_range,
                started_at=started_at,
                finished_at=finished_at,
                error_code=result.error.code if result.error else None,
                truncated=result.truncated,
                raw_ref=result.raw_ref,
            )
        )

    async def _record_evidence(self, item: _Query, hypothesis_id: str, result: ToolResult) -> str:
        evidence = Evidence(
            id=f"ev-{uuid.uuid4().hex[:12]}",
            investigation_id=self._investigation_id,
            hypothesis_id=hypothesis_id,
            tool_call_id=result.tool_call_id,
            source=result.source,
            independence_key=f"{result.source}:{item.request.tool}",
            observation=observation(result, max_chars=self._max_observation_chars),
            relation="neutral",
            raw_ref=result.raw_ref or "",
            time_range=result.time_range,
        )
        await self._store.save_evidence(evidence)
        return evidence.id

    def _decide(
        self,
        datasets: dict[str, Any],
        failures: list[dict[str, Any]],
        truncated: set[str],
    ) -> _Hit | None:
        deployments = _items(datasets.get("deployments"))
        pods = _items(datasets.get("pods"))
        events = _items(datasets.get("events"))
        logs = _items(datasets.get("logs"))
        probe = _probe_value(datasets.get("probe"))
        runbooks = [
            str(item.get("id")) for item in _items(datasets.get("runbooks")) if item.get("id")
        ]
        missing = [item["query"] for item in failures]
        common = {
            "probe_success": probe,
            "missing_queries": missing,
            "truncated_queries": sorted(truncated),
            "runbooks": runbooks,
            "failures": failures,
            "event_count": len(events),
        }
        complete = all(
            key in datasets and key not in truncated for key in ("deployments", "pods", "logs")
        )
        hit = self._match_oom(deployments, pods, common)
        if hit is None:
            hit = self._match_change(deployments, pods, logs, common)
        if hit is None:
            hit = self._match_dependency(logs, probe, common, complete=complete)
        return hit

    def _match_oom(
        self, deployments: list[dict], pods: list[dict], common: dict[str, Any]
    ) -> _Hit | None:
        # 多个 Pod 同时 OOM 时按最近一次终止时间选，同一时间用 Pod 名兜底，结果稳定。
        for pod in sorted(
            pods,
            key=lambda item: (_oom_finished_at(item, self._window), _text(item.get("name"))),
            reverse=True,
        ):
            container = _oom_container(pod, self._window)
            if container is None:
                continue
            deployment = _deployment_for_pod(pod, deployments)
            name = _text(deployment.get("name") if deployment else "")
            current, previous = _templates(deployment) if deployment else (None, None)
            limits = ""
            requests = ""
            if deployment:
                for item in deployment.get("containers") or []:
                    if item.get("name") == container.get("name"):
                        limits = _text((item.get("limits") or {}).get("memory"))
                        requests = _text((item.get("requests") or {}).get("memory"))
            last = container.get("last_terminated") or {}
            detail = (
                f"{name or pod.get('name')} 的 Pod {pod.get('name')} 以 OOMKilled 退出"
                f"（exit_code={last.get('exit_code')}，restart_count={container.get('restart_count')}），"
                f"当前内存 limits={limits or '未取到'}、requests={requests or '未取到'}"
            )
            previous_limit = _memory_bytes(previous.get("memory_limit")) if previous else None
            current_limit = _memory_bytes((current or {}).get("memory_limit"))
            live = _live_template(deployment) if deployment else None
            live_limit = _memory_bytes(live[1]) if live else None
            if previous is None:
                detail += "；没有可比较的历史模板，不能确认回退适用"
                action = "none"
            elif previous_limit is None or current_limit is None:
                detail += (
                    f"；当前或上一版 revision {previous.get('revision')} 的内存限额无法解析，"
                    "不能确认回退能缓解资源不足"
                )
                action = "none"
            elif live is None or live_limit != current_limit:
                detail += "；实时模板的内存限额与历史模板不一致，不能判断回退方向"
                action = "none"
            elif previous_limit <= current_limit:
                detail += (
                    f"；上一版 revision {previous.get('revision')} 的内存限额 "
                    f"{previous.get('memory_limit')} 不高于当前 {limits or '未取到'}，"
                    "回退不能修复资源规格"
                )
                action = "none"
            else:
                detail += (
                    f"；上一版 revision {previous.get('revision')} 的内存限额为 "
                    f"{previous.get('memory_limit') or '未取到'}"
                )
                action = "rollout_undo" if deployment else "none"
                if not deployment:
                    detail += "；没有匹配到 Deployment，无法提案"
            return _Hit(
                rule="oom-limit",
                hypothesis="resource",
                conclusion=f"固定规则判定：{detail}。",
                action=action,
                deployment=name,
                evidence_keys=("pods", "deployments"),
                signals={
                    **common,
                    "pod": pod.get("name"),
                    "deployment": name,
                    "last_reason": last.get("reason"),
                    "current_revision": (current or {}).get("revision"),
                    "previous_revision": (previous or {}).get("revision"),
                },
            )
        return None

    def _match_change(
        self, deployments: list[dict], pods: list[dict], logs: list[dict], common: dict[str, Any]
    ) -> _Hit | None:
        """观察窗内重新启动、且模板与上一版不同的工作负载；必须有失败关联。"""
        window = self._window
        candidates: list[tuple[datetime, dict, dict, dict | None, str, datetime | None]] = []
        for deployment in deployments:
            latest, previous = _templates(deployment)
            if latest is None:
                continue
            pods_for = _pods_for(deployment, pods)
            starts = [start for pod in pods_for if (start := _started_at(pod, window)) is not None]
            if not starts:
                continue
            started = max(starts)
            created = _as_time(latest.get("created_at"))
            created_in_window = created is not None and window.start <= created <= window.end
            # 容器今天重启不代表模板今天改过。新增负载必须是模板本身在窗内创建；
            # 有上一版时，复用旧 ReplicaSet 的回退或重放可以靠“无重启历史的新 Pod”识别。
            if not created_in_window:
                if previous is None:
                    continue
                # 模板创建时间在窗后属于数据不一致，不能再拿新 Pod 反推窗内变更。
                if created is not None and created > window.end:
                    continue
                if not any(_fresh_start(item, window) for item in pods_for):
                    continue
            name = _text(deployment.get("name"))
            hint = _log_hint(name, logs)
            unhealthy = bool(deployment.get("unavailable_replicas")) or any(
                container.get("ready") is False
                for pod in pods_for
                for container in pod.get("containers") or []
            )
            if previous is None:
                candidates.append((started, deployment, latest, None, hint, created))
                continue
            if (
                previous.get("image") == latest.get("image")
                and previous.get("memory_limit") == latest.get("memory_limit")
                and previous.get("memory_request") == latest.get("memory_request")
            ):
                continue
            if not hint and not unhealthy:
                continue
            candidates.append((started, deployment, latest, previous, hint, created))
        if not candidates:
            return None
        started, deployment, latest, previous, hint, created = max(
            candidates, key=lambda row: row[0]
        )
        name = _text(deployment.get("name"))
        if previous is None:
            return _Hit(
                rule="new-workload",
                hypothesis="change",
                conclusion=(
                    f"固定规则判定：观察窗内新增工作负载 {name}"
                    f"（revision {latest.get('revision')}，镜像 {latest.get('image')}，"
                    f"创建于 {created.isoformat() if created else '未取到'}）。"
                    "现有动作没有可回退的历史模板，需要人工核实该变更是否与故障相关。"
                ),
                action="none",
                deployment=name,
                evidence_keys=("deployments", "pods"),
                signals={
                    **common,
                    "deployment": name,
                    "rule_revision": latest.get("revision"),
                    "created_at": created.isoformat() if created else None,
                    "started_at": started.isoformat(),
                },
            )
        return _Hit(
            rule="recent-rollout",
            hypothesis="change",
            conclusion=(
                f"固定规则判定：{name} 的 Pod 在观察窗内启动"
                f"（{started.isoformat()}），当前模板 revision {latest.get('revision')}"
                f"（镜像 {latest.get('image')}，内存限额 {latest.get('memory_limit') or '未取到'}）"
                f"与上一版 revision {previous.get('revision')} 不同"
                f"（镜像 {previous.get('image')}，内存限额 "
                f"{previous.get('memory_limit') or '未取到'}）；"
                + ("入口日志出现该工作负载的错误。" if hint else "该工作负载当前不健康。")
            ),
            action="rollout_undo",
            deployment=name,
            evidence_keys=("deployments", "pods") + (("logs",) if hint else ()),
            signals={
                **common,
                "deployment": name,
                "rule_revision": latest.get("revision"),
                "previous_revision": previous.get("revision"),
                "started_at": started.isoformat(),
                "log_hint": hint,
                "unhealthy": unhealthy,
            },
        )

    def _match_dependency(
        self, logs: list[dict], probe: float | None, common: dict[str, Any], *, complete: bool
    ) -> _Hit | None:
        markers: list[str] = []
        for item in logs:
            for line in item.get("lines") or []:
                lowered = str(line).lower()
                for marker in DEPENDENCY_MARKERS:
                    if marker in lowered and marker not in markers:
                        markers.append(marker)
        if not markers and not (probe is not None and probe < 1):
            return None
        # 只有探针失败、取证又缺了工作负载或日志时，既不能确认依赖，也不能声称排除其他根因。
        if not markers and not complete:
            return None
        observed: list[str] = []
        if markers:
            observed.append(f"日志出现依赖访问错误（{', '.join(markers)}）")
        if probe is not None and probe < 1:
            observed.append(f"业务探针成功值为 {probe:g}")
        absence = "命名空间内没有命中 OOMKilled 或观察窗内的模板变更；" if complete else ""
        return _Hit(
            rule="dependency-chain",
            hypothesis="dependency",
            conclusion=(
                "固定规则判定："
                + "；".join(observed)
                + "。"
                + absence
                + "现有动作不能移除依赖链路故障，需要人工核实下游服务与网络路径。"
            ),
            action="none",
            evidence_keys=("logs", "probe"),
            signals={
                **common,
                "markers": markers,
                "probe_success": probe,
                "coverage_complete": complete,
            },
        )

    async def _apply(
        self,
        hit: _Hit,
        hypotheses: list[dict[str, Any]],
        evidence_keys: dict[str, list[str]],
    ) -> None:
        for hypothesis in hypotheses:
            status = "supported" if hypothesis["key"] == hit.hypothesis else "inconclusive"
            hypothesis["status"] = status
            await self._store.set_hypothesis_status(hypothesis["id"], status)
        supporting = set(hit.evidence_keys)
        for key, ids in evidence_keys.items():
            relation = "supporting" if key in supporting else "neutral"
            for evidence_id in ids:
                await self._store.set_evidence_relation(evidence_id, relation)
        await self._audit(
            "baseline.rule",
            {
                "rules_version": RULES_VERSION,
                "rule": hit.rule,
                "hypothesis": hit.hypothesis,
                "action": hit.action,
                "deployment": hit.deployment,
                "signals": hit.signals,
            },
        )

    async def _propose_rollback(self, hit: _Hit, supporting: list[str]) -> str | None:
        if self._proposer is None:
            await self._skip_action("proposer_unavailable", hit.deployment)
            return None
        if not supporting:
            await self._skip_action("no_supporting_evidence", hit.deployment)
            return None
        try:
            proposal, decision = await self._proposer.propose_rollback(
                deployment=hit.deployment,
                reason=hit.conclusion,
                actor=ACTOR,
                namespace=self._incident.namespace,
                investigation_id=self._investigation_id,
                origin=ActionOrigin.RULES,
                evidence_ids=supporting,
            )
        except AutoExecutionError as exc:
            await self._audit(
                "action.proposed_but_not_executed",
                {"proposal_id": exc.proposal_id, "code": exc.code},
            )
            return exc.proposal_id
        except ActionError as exc:
            await self._skip_action(exc.code, hit.deployment)
            return None
        await self._audit(
            "action.proposed_by_rules",
            {
                "proposal_id": proposal.id,
                "kind": proposal.kind.value,
                "target": hit.deployment,
                "disposition": decision.disposition.value,
                "evidence_ids": supporting,
            },
        )
        return proposal.id

    async def _skip_action(self, reason: str, deployment: str) -> None:
        await self._audit("action.skipped", {"reason": reason, "deployment": deployment})

    async def _finish(
        self,
        status: InvestigationStatus,
        *,
        conclusion: str | None,
        termination: str | None,
        hypotheses: list[dict[str, Any]],
        evidence_keys: dict[str, list[str]],
        proposal_id: str | None,
    ) -> Report:
        finished_at = datetime.now(UTC)
        await self._store.set_investigation_status(
            self._investigation_id,
            status,
            conclusion=conclusion,
            termination_reason=termination,
            finished_at=finished_at,
        )
        await self._audit(
            f"investigation.{status.value}",
            {
                "rules_version": RULES_VERSION,
                "hypotheses": len(hypotheses),
                "evidence": sum(len(ids) for ids in evidence_keys.values()),
                "tool_calls": self._used_tool_calls,
                "model_calls": 0,
                "termination": termination,
            },
        )
        saved = await self._store.get_investigation(self._investigation_id)
        return Report(
            investigation_id=self._investigation_id,
            status=saved.status if saved is not None else status,
            conclusion=saved.conclusion if saved is not None else conclusion,
            termination_reason=saved.termination_reason if saved is not None else termination,
            hypotheses=hypotheses,
            evidence_count=sum(len(ids) for ids in evidence_keys.values()),
            tool_call_count=self._used_tool_calls,
            proposal_id=proposal_id,
        )

    async def _audit(self, event: str, payload: dict[str, Any]) -> None:
        await self._store.append_audit(
            [
                AuditEvent(
                    id=f"audit-{uuid.uuid4().hex[:12]}",
                    correlation_id=self._investigation_id,
                    actor=ACTOR,
                    event=event,
                    occurred_at=datetime.now(UTC),
                    payload=payload,
                )
            ]
        )


def _items(dataset: Any) -> list[dict[str, Any]]:
    if not isinstance(dataset, dict):
        return []
    items = dataset.get("items")
    return [item for item in items or [] if isinstance(item, dict)]
