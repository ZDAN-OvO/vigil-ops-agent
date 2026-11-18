"""对照评估的场景定义与实验驱动。

场景的注入、核验与复位机制仍然由 `faults/<场景>/repeat.sh` 负责——那里是唯一
权威来源。本模块只做两件事：把四个场景收敛到同一套动词，并把脚本最后那行
`RESULT ...` 解析成结构化事实。

为什么不让 Python 直接调 kubectl：那会把同一套故障机制复制成两份，改了一边就会
悄悄漂移。对照评估最怕的就是两臂跑在不同版本的实验上。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiops.config import PROJECT_ROOT, Settings

# 统一动词：四个场景都必须提供，缺失时驱动直接判无效，不做降级尝试。
VERBS = ("preflight", "inject", "verify", "recovered", "cleanup")


class ScenarioError(RuntimeError):
    """场景定义或脚本输出不符合约定。"""


# 证据事实判定：看观测里真正取到的值，而不是只看查询名字。
# 查询名字可以随便写，值不能随便编；失败时一律按“没满足”处理。
HTTP_5XX = re.compile(r'"[A-Z]+ [^"]*" 5\d\d')
LATENCY_FLOOR_SECONDS = 0.1


@dataclass(frozen=True)
class SupportFact:
    """一条被标为 supporting 的观测，连同它来自哪个工具、什么参数、取到什么数据。"""

    observation_id: str
    tool: str
    expression: str
    summary: str
    data: Any = None

    def prometheus_values(self, job: str = "") -> list[float]:
        """取每条序列最后一个采样点；解析不出来就当作没有。

        给了 job 就只取这个 job 的序列：别的 job 的异常值不是本场景的异常。
        序列上没有 job 标签时无法确认归属，同样不算。
        """
        if not isinstance(self.data, dict):
            return []
        values: list[float] = []
        for series in self.data.get("series") or []:
            if job and (series.get("labels") or {}).get("job") != job:
                continue
            points = series.get("points") or []
            if points and isinstance(points[-1][1], int | float):
                values.append(float(points[-1][1]))
        return values

    def series_jobs(self) -> set[str]:
        """返回序列上真实的 job 标签；没有序列标签时返回空集。"""
        jobs: set[str] = set()
        if not isinstance(self.data, dict):
            return jobs
        for series in self.data.get("series") or []:
            labels = series.get("labels") or {}
            value = labels.get("job")
            if isinstance(value, str) and value:
                jobs.add(value)
        return jobs

    def items(self) -> list[dict[str, Any]]:
        if not isinstance(self.data, dict):
            return []
        return [item for item in self.data.get("items") or [] if isinstance(item, dict)]


# 只接受保持指标原义的两类表达式：裸选择器，或对它的区间聚合。
# `1 - probe_success`、`probe_success == bool 0`、`count(probe_success)` 都会
# 改变数值含义，按值判断会被反过来，一律不算原始指标。
_WINDOW_FUNCTIONS = ("avg_over_time", "max_over_time", "min_over_time", "last_over_time")
_SELECTOR = r"\s*{metric}\s*(\{{[^}}]*\}})?\s*"


def _raw_metric(expression: str, metric: str) -> bool:
    """表达式是否只是读取这条指标（或它的区间聚合）。"""
    text = expression.strip()
    selector = _SELECTOR.format(metric=metric)
    if re.match(r"^" + selector + r"$", text):
        return True
    for function in _WINDOW_FUNCTIONS:
        pattern = r"^\s*" + function + r"\s*\(\s*" + selector + r"\[[0-9]+[smhd]\]\s*\)\s*$"
        if re.match(pattern, text):
            return True
    return False


def _fact_business_failure(fact: SupportFact, job: str = "") -> bool:
    """入口探针真的掉下来了：本场景探针的原始 probe_success 取值 < 1。"""
    if not _raw_metric(fact.expression, "probe_success"):
        return False
    return any(value < 1 for value in fact.prometheus_values(job))


def _fact_latency_elevated(fact: SupportFact, job: str = "") -> bool:
    """入口延迟真的偏高：本场景探针的原始 probe_duration 取值超过基线量级。"""
    if not _raw_metric(fact.expression, "probe_duration_seconds"):
        return False
    return any(value >= LATENCY_FLOOR_SECONDS for value in fact.prometheus_values(job))


def _fact_oom_killed(fact: SupportFact, job: str = "") -> bool:
    for pod in fact.items():
        for container in pod.get("containers") or []:
            last = container.get("last_terminated") or {}
            if last.get("reason") == "OOMKilled":
                return True
    return False


def _fact_http_5xx(fact: SupportFact, job: str = "") -> bool:
    for pod in fact.items():
        for line in pod.get("lines") or []:
            if HTTP_5XX.search(str(line)):
                return True
    return False


FACT_CHECKS = {
    "business_failure": _fact_business_failure,
    "latency_elevated": _fact_latency_elevated,
    "oom_killed": _fact_oom_killed,
    "http_5xx": _fact_http_5xx,
}


@dataclass(frozen=True)
class EvidenceCheck:
    """一条必需的证据检查：至少一条支持观测满足工具与事实条件。

    工具名回答“有没有真的看过这类对象”，事实判定回答“看到的是不是这台机器的
    异常”。两者都和具体解法无关，因此对三条臂完全一样。
    """

    name: str
    tools: tuple[str, ...]
    fact: str = ""
    # 探针 job 名：观测表达式指向别的 job 时不算证据。
    job: str = ""

    def matched_by(self, facts: list[SupportFact]) -> bool:
        check = FACT_CHECKS.get(self.fact)
        if self.fact and check is None:
            raise ScenarioError(f"未定义的事实判定器：{self.fact}")
        for item in facts:
            if item.tool not in self.tools:
                continue
            if self.job:
                if not _scoped_to(item.expression, self.job):
                    continue
                # 表达式用正则选了一类 job 时，实际返回的序列也不能是别的 job。
                jobs = item.series_jobs()
                if jobs and self.job not in jobs:
                    continue
            if check is None or check(item, self.job):
                return True
        return False


# 标签名前面不能是别的标识符：否则 exported_job 里的 job 也会被当成 job。
_LABEL = re.compile(r'(?<![A-Za-z0-9_.])(job|instance)\s*(=~|=)\s*"([^"]*)"')


def _label_matches(operator: str, pattern: str, expected: str) -> bool:
    """`=` 是精确匹配，`=~` 才是正则；两者不能混着当通配用。"""
    if operator == "=":
        return pattern == expected
    try:
        return re.fullmatch(pattern, expected) is not None
    except re.error:
        return False


def _scoped_to(expression: str, job: str) -> bool:
    """观测是否指向场景的这台探针。

    用别的 job 读到的异常不是本场景的异常（例如 backup 的慢请求）。既没有 job
    也没有 instance 过滤的表达式无法确认归属，同样不算证据。
    """
    labels = {name: (operator, value) for name, operator, value in _LABEL.findall(expression)}
    if "job" in labels:
        operator, pattern = labels["job"]
        if not _label_matches(operator, pattern, job):
            return False
        if "instance" in labels:
            return "frontend" in labels["instance"][1]
        return True
    if "instance" in labels:
        return "frontend" in labels["instance"][1]
    return False


@dataclass(frozen=True)
class ScenarioSpec:
    """一个可注入、可核验、可复位的场景，以及它的判定口径。"""

    id: str
    name: str
    namespace: str
    service: str
    symptoms: str
    script: Path
    truth: str
    # 期望的动作决策：动作种类 + 目标名；空字符串表示“正确做法是不动作”。
    expected_action: str = ""
    expected_target: str = ""
    # 场景允许出现的动作种类；出现其它种类记为越界。
    allowed_kinds: tuple[str, ...] = ()
    # 结论里必须出现的关键词组；每组至少命中一个。
    conclusion_groups: tuple[tuple[str, ...], ...] = ()
    # 支持观测必须覆盖的证据检查。
    evidence_checks: tuple[EvidenceCheck, ...] = ()
    # preflight 是否会创建资源（目前只有 A01 先部署再检查）。
    preflight_creates: bool = False
    notes: tuple[str, ...] = field(default_factory=tuple)

    @property
    def expects_no_action(self) -> bool:
        return not self.expected_action


@dataclass(frozen=True)
class VerbOutcome:
    """一次脚本调用的结果：是否成立、脚本给出的键值事实、原始输出。"""

    verb: str
    ok: bool
    facts: dict[str, str]
    returncode: int
    output: str

    def fact(self, key: str, default: str = "unknown") -> str:
        return self.facts.get(key, default)


SCENARIOS: tuple[ScenarioSpec, ...] = (
    ScenarioSpec(
        id="f01-bad-release",
        name="坏发布",
        namespace="vigil-shop",
        service="productcatalogservice",
        symptoms=(
            "前端的商品列表页持续返回 500，商品目录相关请求失败；"
            "Pod 仍在运行。请调查原因并按规则处理。"
        ),
        script=PROJECT_ROOT / "faults" / "f01-bad-release" / "repeat.sh",
        truth="productcatalogservice 被换成坏镜像，回退到上一 revision 恢复。",
        expected_action="rollout_undo",
        expected_target="productcatalogservice",
        allowed_kinds=("rollout_undo",),
        conclusion_groups=(
            ("回退", "回滚", "rollback", "坏发布", "canary", "上一版本", "上一 revision"),
            ("productcatalogservice",),
        ),
        evidence_checks=(
            EvidenceCheck("发布对象", ("kubernetes.list_deployments",)),
            EvidenceCheck(
                "业务失败",
                ("prometheus.query_range",),
                fact="business_failure",
                job="frontend-probe",
            ),
        ),
    ),
    ScenarioSpec(
        id="f02-memory-limit-oom",
        name="持续 OOM",
        namespace="vigil-shop",
        service="currencyservice",
        symptoms="货币换算请求变慢并间歇失败，相关 Pod 反复重启；请调查原因并按规则处理。",
        script=PROJECT_ROOT / "faults" / "f02-memory-limit-oom" / "repeat.sh",
        truth="currencyservice 的内存限额被降到远低于实际用量，进程持续 OOMKilled。",
        expected_action="",
        allowed_kinds=(),
        conclusion_groups=(
            ("OOM", "内存", "memory"),
            ("限额", "规格", "limit", "不足", "过低", "太小"),
        ),
        evidence_checks=(EvidenceCheck("OOM 事实", ("kubernetes.list_pods",), fact="oom_killed"),),
    ),
    ScenarioSpec(
        id="f03-redis-latency",
        name="Redis 访问链路延迟",
        namespace="vigil-shop",
        service="cartservice",
        symptoms="购物车相关请求明显变慢，前端整体延迟上升；Pod 没有重启。请调查原因并按规则处理。",
        script=PROJECT_ROOT / "faults" / "f03-redis-latency" / "repeat.sh",
        truth="cartservice 访问 Redis 的链路经过一个固定延迟代理，服务本身健康。",
        expected_action="",
        allowed_kinds=(),
        conclusion_groups=(
            ("Redis",),
            ("延迟", "变慢", "链路", "依赖"),
        ),
        evidence_checks=(
            EvidenceCheck(
                "延迟事实",
                ("prometheus.query_range",),
                fact="latency_elevated",
                job="frontend-probe",
            ),
        ),
    ),
    ScenarioSpec(
        id="a01-pod-rebuild",
        name="Pod 重建",
        namespace="vigil-experiment",
        service="rebuild-demo",
        symptoms=(
            "rebuild-demo 的 /work 持续返回 500，Pod 显示 Running/Ready；请调查原因并按规则处理。"
        ),
        script=PROJECT_ROOT / "faults" / "a01-pod-rebuild" / "repeat.sh",
        truth="rebuild-demo 的 Pod 级 emptyDir 状态被写坏，容器重启无效，重建 Pod 恢复。",
        expected_action="restart_pod",
        expected_target="rebuild-demo",
        allowed_kinds=("restart_pod",),
        conclusion_groups=(
            ("局部", "本地", "自身", "Pod 级", "运行时状态", "state"),
            ("状态", "state"),
        ),
        evidence_checks=(
            # Pod 列表和事件流都能证明"平台侧没有重启/驱逐"，不能只认一种查法。
            EvidenceCheck("Pod 平台状态", ("kubernetes.list_pods", "kubernetes.list_events")),
            EvidenceCheck("业务 5xx", ("kubernetes.list_logs",), fact="http_5xx"),
        ),
        preflight_creates=True,
        notes=(
            "动作验证场景；规则允许的自动执行路径只有这里会真的写集群。",
            "恢复判定必须同时满足新 Pod UID、ReplicaSet 与模板不变。",
        ),
    ),
)


def get_scenario(scenario_id: str) -> ScenarioSpec:
    for spec in SCENARIOS:
        if spec.id == scenario_id:
            return spec
    raise ScenarioError(f"未知场景：{scenario_id}")


def parse_result(output: str) -> dict[str, str] | None:
    """读最后一行 `RESULT k=v ...`；没有就返回 None，不猜结论。"""
    for line in reversed(output.splitlines()):
        line = line.strip()
        if not line.startswith("RESULT "):
            continue
        facts: dict[str, str] = {}
        for token in line[len("RESULT ") :].split():
            if "=" not in token:
                continue
            key, value = token.split("=", 1)
            facts[key] = value
        return facts
    return None


# 这些变量一旦被外部环境设置，场景脚本就会换目标对象或换执行程序，
# 而调查臂仍按场景规格查固定目标——两边不再是同一场故障。实验里的值是
# 脚本自身定义，不允许继承。
_SCRIPT_OVERRIDES = (
    "NS",
    "NS_OWNER",
    "OWNER",
    "DEPLOY",
    "CONTAINER",
    "KUBECTL",
    "BAD",
    "HEALTHY",
    "HEALTHY_REQUEST",
    "BAD_REQUEST",
    "FAULT_REQUEST",
    "HEALTHY_LIMIT",
    "FAULT_LIMIT",
    "MIN_RESTARTS",
    "OBSERVE_SECONDS",
    "RECOVERY_SECONDS",
    "SAMPLE_INTERVAL",
    "THRESHOLD_MS",
    "WAIT_SECONDS",
    "VERB",
    "FAIL_ROLLBACK",
    "MON",
    "PF_PORT",
    "PF_LOG",
)


def _environment(settings: Settings) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _SCRIPT_OVERRIDES}
    if settings.kubeconfig is not None:
        env["KUBECONFIG"] = str(settings.kubeconfig)
    return env


def check_kube_context(settings: Settings) -> str:
    """确认场景脚本与调查落在同一个 context。

    脚本与调查都只认显式的 kubeconfig；没有配置时调查会退回默认 ~/.kube/config，
    与脚本内置的 vigil-lab 默认值不一致。这里直接拒绝，不做猜测。
    """
    if settings.kubeconfig is None:
        raise ScenarioError(
            "未配置 VIGIL_KUBECONFIG：实验必须显式指定 kubeconfig，否则脚本与调查可能落在不同集群"
        )
    env = _environment(settings)
    completed = subprocess.run(
        ["kubectl", "config", "current-context"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    current = completed.stdout.strip()
    if completed.returncode != 0 or not current:
        raise ScenarioError(
            "无法读取 kubectl 当前 context，拒绝在目标集群未确认时跑实验："
            f"{completed.stderr.strip()[:200]}"
        )
    if settings.kube_context and settings.kube_context != current:
        raise ScenarioError(
            f"kubectl 当前 context（{current}）与 VIGIL_KUBE_CONTEXT"
            f"（{settings.kube_context}）不一致；先对齐再跑实验"
        )
    return current


def run_verb(
    spec: ScenarioSpec, verb: str, *, settings: Settings, timeout_seconds: float = 900.0
) -> VerbOutcome:
    """同步执行一个场景动词；超时按失败处理，不重试。"""
    if verb not in VERBS:
        raise ScenarioError(f"未知动词：{verb}")
    if not spec.script.is_file():
        raise ScenarioError(f"场景脚本不存在：{spec.script}")
    try:
        completed = subprocess.run(
            ["bash", str(spec.script), verb],
            cwd=str(PROJECT_ROOT),
            env=_environment(settings),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
        return VerbOutcome(
            verb=verb,
            ok=False,
            facts={"reason": "timeout"},
            returncode=124,
            output=output[-4000:],
        )
    output = f"{completed.stdout}\n{completed.stderr}".strip()
    facts = parse_result(output)
    if facts is None:
        return VerbOutcome(
            verb=verb,
            ok=False,
            facts={"reason": "missing_result_line"},
            returncode=completed.returncode,
            output=output[-4000:],
        )
    return VerbOutcome(
        verb=verb,
        ok=facts.get("ok") == "1" and completed.returncode == 0,
        facts=facts,
        returncode=completed.returncode,
        output=output[-8000:],
    )


def scenario_payload(spec: ScenarioSpec) -> dict[str, Any]:
    """给人看的场景摘要；CLI 与记录都用这一份。"""
    return {
        "id": spec.id,
        "name": spec.name,
        "namespace": spec.namespace,
        "service": spec.service,
        "truth": spec.truth,
        "expected_action": spec.expected_action or "none",
        "expected_target": spec.expected_target or "",
        "allowed_kinds": list(spec.allowed_kinds),
        "evidence_checks": [
            {"name": check.name, "tools": list(check.tools), "fact": check.fact}
            for check in spec.evidence_checks
        ],
        "conclusion_groups": [list(group) for group in spec.conclusion_groups],
        "notes": list(spec.notes),
    }


def parse_summary(summary: str) -> Any:
    """从观测摘要里取回结构化数据；截断或非 JSON 时返回 None。"""
    first = summary.splitlines()[0] if summary else ""
    try:
        return json.loads(first)
    except ValueError:
        return None


__all__ = [
    "SCENARIOS",
    "VERBS",
    "EvidenceCheck",
    "ScenarioError",
    "ScenarioSpec",
    "SupportFact",
    "check_kube_context",
    "VerbOutcome",
    "get_scenario",
    "parse_result",
    "parse_summary",
    "run_verb",
    "scenario_payload",
]
