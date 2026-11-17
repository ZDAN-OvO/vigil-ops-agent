"""对照评估的场景定义与实验驱动。

场景的注入、核验与复位机制仍然由 `faults/<场景>/repeat.sh` 负责——那里是唯一
权威来源。本模块只做两件事：把四个场景收敛到同一套动词，并把脚本最后那行
`RESULT ...` 解析成结构化事实。

为什么不让 Python 直接调 kubectl：那会把同一套故障机制复制成两份，改了一边就会
悄悄漂移。对照评估最怕的就是两臂跑在不同版本的实验上。
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from aiops.config import PROJECT_ROOT, Settings

# 统一动词：四个场景都必须提供，缺失时驱动直接判无效，不做降级尝试。
VERBS = ("preflight", "inject", "verify", "recovered", "cleanup")


class ScenarioError(RuntimeError):
    """场景定义或脚本输出不符合约定。"""


@dataclass(frozen=True)
class EvidenceCheck:
    """一条必需的证据检查：至少一条支持观测满足工具与关键词条件。

    用工具名而不是自由文本判断“有没有真的看过”，用关键词判断“看到的是不是
    这台机器的故障机制”。两个条件都和解法无关，因此对三条臂完全一样。
    """

    name: str
    tools: tuple[str, ...]
    contains: tuple[str, ...] = ()

    def matched_by(self, facts: list[SupportFact]) -> bool:
        for fact in facts:
            if fact.tool not in self.tools:
                continue
            if not self.contains:
                return True
            haystack = f"{fact.expression} {fact.summary}"
            if any(token in haystack for token in self.contains):
                return True
        return False


@dataclass(frozen=True)
class SupportFact:
    """一条被标为 supporting 的观测，连同它来自哪个工具、什么参数。"""

    observation_id: str
    tool: str
    expression: str
    summary: str


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
    # 复位后需要多等一会儿的额外秒数（真实告警清空、滚动更新）。
    settle_seconds: int = 0
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
            EvidenceCheck("业务失败", ("prometheus.query_range", "kubernetes.list_logs")),
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
        evidence_checks=(EvidenceCheck("OOM 事实", ("kubernetes.list_pods",), ("OOMKilled",)),),
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
            EvidenceCheck("延迟事实", ("prometheus.query_range",), ("probe_duration",)),
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
            EvidenceCheck("Pod 平台状态", ("kubernetes.list_pods",)),
            EvidenceCheck("业务 5xx", ("kubernetes.list_logs",), ("500",)),
        ),
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


def _environment(settings: Settings) -> dict[str, str]:
    env = dict(os.environ)
    if settings.kubeconfig is not None:
        env["KUBECONFIG"] = str(settings.kubeconfig)
    if settings.kube_context:
        env["VIGIL_KUBE_CONTEXT"] = settings.kube_context
    return env


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
            {"name": check.name, "tools": list(check.tools), "contains": list(check.contains)}
            for check in spec.evidence_checks
        ],
        "conclusion_groups": [list(group) for group in spec.conclusion_groups],
        "notes": list(spec.notes),
    }


__all__ = [
    "SCENARIOS",
    "VERBS",
    "EvidenceCheck",
    "ScenarioError",
    "ScenarioSpec",
    "SupportFact",
    "VerbOutcome",
    "get_scenario",
    "parse_result",
    "run_verb",
    "scenario_payload",
]
