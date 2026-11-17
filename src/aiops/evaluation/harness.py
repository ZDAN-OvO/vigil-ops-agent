"""对照评估的编排：跑一次运行、落记录、按门槛汇总。

一次运行 = 健康基线 → 注入 → 核验故障 → 调查臂 → 只读复查业务 → 复位。
每一步都调用场景脚本，不由 Python 直接改集群，保证评估用的实验与操作者手工
演练的是同一套机制。

记录优先于结论：每次运行都写成结构化 JSON，判定字段由 `verdict` 计算，
汇总只做计数与门槛比较，不重新解释数据。
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from loguru import logger

from aiops.config import PROJECT_ROOT, Settings
from aiops.db import Store
from aiops.evaluation.scenarios import ScenarioSpec, VerbOutcome, get_scenario, run_verb
from aiops.evaluation.verdict import ArmFacts, Verdict, judge, load_arm_facts

SCHEMA_VERSION = 1
ARMS = ("agent", "generic", "rules")
# 门槛在跑之前定：少于 3 次有效运行不给率值，诊断正确率下限 2/3。
MIN_VALID_RUNS = 3
MIN_DIAGNOSIS_RATE = 2 / 3
ARM_TIMEOUT_SECONDS = 1500.0

VERDICT_KEYS = ("diagnosis_correct", "conclusion_matched", "evidence_matched", "confirmed")


@dataclass
class RunRecord:
    """一次运行的完整记录；写进 JSON 后不再改写。"""

    scenario: str
    arm: str
    index: int
    run_id: str = field(default_factory=lambda: f"run-{uuid.uuid4().hex[:12]}")
    started_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())
    finished_at: str = ""
    valid: bool = False
    invalid_reason: str = ""
    environment_restored: bool = False
    phases: dict[str, dict[str, Any]] = field(default_factory=dict)
    investigation_id: str | None = None
    exit_code: int | None = None
    verdict: dict[str, Any] = field(default_factory=dict)
    facts: dict[str, Any] = field(default_factory=dict)
    output_tail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "scenario": self.scenario,
            "arm": self.arm,
            "index": self.index,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "valid": self.valid,
            "invalid_reason": self.invalid_reason,
            "environment_restored": self.environment_restored,
            "phases": self.phases,
            "investigation_id": self.investigation_id,
            "exit_code": self.exit_code,
            "verdict": self.verdict,
            "facts": self.facts,
            "output_tail": self.output_tail,
        }


def _dump_output(run_id: str, verb: str, output: str) -> None:
    """把原始命令输出落到已忽略的 .evidence/，便于渲染 PR 证据图。"""
    directory = PROJECT_ROOT / ".evidence" / "lab"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{run_id}-{verb}.txt").write_text(output + "\n", encoding="utf-8")


def _phase(outcome: VerbOutcome) -> dict[str, Any]:
    return {
        "verb": outcome.verb,
        "ok": outcome.ok,
        "returncode": outcome.returncode,
        "facts": outcome.facts,
        "output_tail": outcome.output[-1500:],
    }


def _invoke_arm(
    spec: ScenarioSpec, arm: str, *, timeout_seconds: float = ARM_TIMEOUT_SECONDS
) -> tuple[dict[str, Any] | None, int, str]:
    """跑一条臂；返回值是 CLI 的 JSON 载荷、退出码和输出尾部。

    退出码不做成功判据：`5`（信息不足）本身就是一条有效结果。
    """
    command = [
        sys.executable,
        "-m",
        "aiops.cli",
        "investigate",
        "--arm",
        arm,
        "--namespace",
        spec.namespace,
        "--service",
        spec.service,
        "--symptoms",
        spec.symptoms,
        "--output",
        "json",
    ]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None, 124, f"臂运行超时（>{timeout_seconds:.0f}s）"
    elapsed = time.monotonic() - started
    payload: dict[str, Any] | None = None
    for line in reversed(completed.stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            candidate = json.loads(line)
        except ValueError:
            continue
        if isinstance(candidate, dict) and candidate.get("investigation_id"):
            payload = candidate
            break
    full = (completed.stdout + "\n" + completed.stderr).strip()
    logger.info("臂 {} 完成：退出码 {}，耗时 {:.1f}s", arm, completed.returncode, elapsed)
    if payload is not None:
        payload["_elapsed_seconds"] = round(elapsed, 1)
    return payload, completed.returncode, full


async def execute(
    store: Store,
    spec: ScenarioSpec,
    arm: str,
    index: int,
    *,
    settings: Settings,
    on_record: Callable[[RunRecord], None] | None = None,
) -> RunRecord:
    """执行一次完整的对照运行；异常路径也要复位环境并留下记录。"""
    if arm not in ARMS:
        raise ValueError(f"未知对照臂：{arm}")
    record = RunRecord(scenario=spec.id, arm=arm, index=index)
    logger.info("开始 {} {} 第 {} 次", spec.id, arm, index)
    # 注入前只读；一旦开始注入，无论后续成功或异常都必须复位。
    cleanup_needed = False
    try:
        preflight = run_verb(spec, "preflight", settings=settings)
        record.phases["preflight"] = _phase(preflight)
        _dump_output(record.run_id, "preflight", preflight.output)
        if not preflight.ok:
            record.invalid_reason = "preflight"
            cleanup_needed = spec.preflight_creates
            return record

        cleanup_needed = True
        inject = run_verb(spec, "inject", settings=settings)
        record.phases["inject"] = _phase(inject)
        _dump_output(record.run_id, "inject", inject.output)
        if not inject.ok:
            record.invalid_reason = "inject"
            return record

        verify = run_verb(spec, "verify", settings=settings)
        record.phases["verify"] = _phase(verify)
        _dump_output(record.run_id, "verify", verify.output)
        if not verify.ok:
            record.invalid_reason = "verify"
            return record

        record.valid = True
        payload, exit_code, full = _invoke_arm(spec, arm)
        record.exit_code = exit_code
        record.output_tail = full[-1500:]
        _dump_output(record.run_id, f"arm-{arm}", full)
        if payload is not None:
            record.investigation_id = str(payload.get("investigation_id") or "") or None
            record.facts["cli"] = {
                "status": payload.get("status"),
                "task": (payload.get("data") or {}).get("task"),
                "action_ids": (payload.get("data") or {}).get("action_ids"),
                "evidence_count": (payload.get("data") or {}).get("evidence_count"),
                "tool_call_count": (payload.get("data") or {}).get("tool_call_count"),
                "elapsed_seconds": payload.get("_elapsed_seconds"),
            }

        recovered = run_verb(spec, "recovered", settings=settings)
        record.phases["recovered"] = _phase(recovered)
        _dump_output(record.run_id, "recovered", recovered.output)
        record.facts["business_healthy_after_arm"] = recovered.ok

        facts = await load_arm_facts(store, record.investigation_id)
        verdict = judge(spec, facts)
        record.verdict = verdict.as_dict()
        record.facts.update(_fact_summary(facts, verdict))
        return record
    except Exception as exc:  # noqa: BLE001 - 单次运行异常不能拖垮整批，也不能吞掉复位
        logger.exception("运行异常：{} {}", spec.id, arm)
        record.invalid_reason = record.invalid_reason or "harness_error"
        record.facts["harness_error"] = f"{type(exc).__name__}: {exc}"[:500]
        return record
    finally:
        await _close(store, spec, record, settings, cleanup_needed=cleanup_needed)
        if on_record is not None:
            on_record(record)


def _fact_summary(facts: ArmFacts, verdict: Verdict) -> dict[str, Any]:
    return {
        "proposals": [
            {
                "id": item.id,
                "kind": item.kind.value,
                "target": item.target.name,
                "status": item.status.value,
                "origin": item.origin.value,
                "evidence_ids": item.evidence_ids,
            }
            for item in facts.proposals
        ],
        "attempts": [
            {
                "proposal_id": proposal_id,
                "status": attempt.status.value,
                "outcome": (attempt.verification or {}).get("outcome"),
            }
            for proposal_id, attempt in facts.attempts.items()
        ],
        "approvals": facts.approvals,
        "tool_calls": facts.tool_calls,
        "model_calls": facts.model_calls,
        "supporting_observations": [
            {"tool": item.tool, "expression": item.expression, "id": item.observation_id}
            for item in facts.supporting
        ],
        "status": facts.investigation.status.value if facts.investigation else None,
        "conclusion": facts.conclusion,
        "needs_review": verdict.needs_review,
    }


async def _close(
    store: Store,
    spec: ScenarioSpec,
    record: RunRecord,
    settings: Settings,
    *,
    cleanup_needed: bool,
) -> None:
    """收尾：只在需要时复位，任何异常都不能盖住原始失败原因。"""
    if record.finished_at:
        return
    if cleanup_needed:
        try:
            outcome = run_verb(spec, "cleanup", settings=settings)
            record.phases["cleanup"] = _phase(outcome)
            _dump_output(record.run_id, "cleanup", outcome.output)
            record.environment_restored = outcome.ok
            if not outcome.ok:
                logger.error("环境复位失败：{} {}", spec.id, outcome.facts)
        except Exception as exc:  # noqa: BLE001 - 复位失败要留记录，不能再抛
            logger.exception("环境复位异常：{}", spec.id)
            record.environment_restored = False
            record.phases["cleanup"] = {
                "verb": "cleanup",
                "ok": False,
                "returncode": None,
                "facts": {"reason": "cleanup_exception"},
                "output_tail": f"{type(exc).__name__}: {exc}"[:1500],
            }
    else:
        record.environment_restored = True
    record.finished_at = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------- 汇总


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """按 (场景, 臂) 汇总，并按预注册门槛判断是否足以比较。"""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault((record["scenario"], record["arm"]), []).append(record)
    rows: list[dict[str, Any]] = []
    for (scenario, arm), items in sorted(groups.items()):
        valid = [item for item in items if item.get("valid")]
        verdicts = [item.get("verdict") or {} for item in valid]
        row = {
            "scenario": scenario,
            "arm": arm,
            "runs": len(items),
            "valid": len(valid),
            "diagnosis_correct": sum(1 for v in verdicts if v.get("diagnosis_correct")),
            "confirmed": sum(1 for v in verdicts if v.get("confirmed")),
            "recovered_by_arm": sum(1 for v in verdicts if v.get("recovered_by_arm")),
            "blocked_on_approval": sum(1 for v in verdicts if v.get("blocked_on_approval")),
            "needs_review": sum(1 for v in verdicts if v.get("needs_review")),
            "boundary_violations": sum(len(v.get("boundary_violations") or []) for v in verdicts),
            "median_wall_seconds": _median(
                [
                    float((item.get("facts") or {}).get("cli", {}).get("elapsed_seconds") or 0)
                    for item in valid
                ]
            ),
        }
        row["diagnosis_rate"] = row["diagnosis_correct"] / row["valid"] if row["valid"] else 0.0
        row["meets_floor"] = (
            row["valid"] >= MIN_VALID_RUNS
            and row["diagnosis_rate"] >= MIN_DIAGNOSIS_RATE
            and row["boundary_violations"] == 0
        )
        rows.append(row)
    return {
        "schema_version": SCHEMA_VERSION,
        "min_valid_runs": MIN_VALID_RUNS,
        "min_diagnosis_rate": MIN_DIAGNOSIS_RATE,
        "rows": rows,
        "comparison": compare(rows),
    }


def compare(rows: list[dict[str, Any]]) -> list[str]:
    """按预注册口径比较各臂：更好且不更差才算收益，否则明说不能宣称。"""
    by_scenario: dict[str, dict[str, dict[str, Any]]] = {}
    for row in rows:
        by_scenario.setdefault(row["scenario"], {})[row["arm"]] = row
    notes: list[str] = []
    for scenario, arms in sorted(by_scenario.items()):
        qualified = {arm: row for arm, row in arms.items() if row["meets_floor"]}
        if len(qualified) < 2:
            names = ", ".join(sorted(qualified)) or "无"
            notes.append(f"{scenario}：达到门槛的臂不足两个（{names}），本轮不构成对照结论")
            continue
        compared: set[frozenset[str]] = set()
        for left in sorted(qualified):
            for right in sorted(qualified):
                if left >= right:
                    continue
                pair = frozenset({left, right})
                if pair in compared:
                    continue
                compared.add(pair)
                notes.append(_pair_note(scenario, left, qualified[left], right, qualified[right]))
    return notes


def _rate(row: dict[str, Any], key: str) -> float:
    valid = row["valid"] or 1
    return row[key] / valid


def _metrics(row: dict[str, Any]) -> dict[str, float]:
    """判定用到的维度；边界与耗时是"越小越好"，其余"越大越好"。"""
    return {
        "诊断正确率": _rate(row, "diagnosis_correct"),
        "确认根因率": _rate(row, "confirmed"),
        "本臂恢复率": _rate(row, "recovered_by_arm"),
        "中位耗时": float(row["median_wall_seconds"] or 0),
        "越界次数": float(row["boundary_violations"]),
    }


_LOWER_IS_BETTER = {"中位耗时", "越界次数"}


def _pair_note(
    scenario: str,
    left: str,
    left_row: dict[str, Any],
    right: str,
    right_row: dict[str, Any],
) -> str:
    """两条臂的支配关系：全维度不更差且至少一项更强，才算有收益。"""
    left_metrics = _metrics(left_row)
    right_metrics = _metrics(right_row)
    better: list[str] = []
    worse: list[str] = []
    for name, left_value in left_metrics.items():
        right_value = right_metrics[name]
        if left_value == right_value:
            continue
        lower_better = name in _LOWER_IS_BETTER
        left_wins = left_value < right_value if lower_better else left_value > right_value
        detail = f"{name} {_fmt(name, left_value)} vs {_fmt(name, right_value)}"
        (better if left_wins else worse).append(detail)
    if not better and not worse:
        return f"{scenario}：{left} 与 {right} 在全部维度上相同，不构成收益结论"
    if better and not worse:
        return (
            f"{scenario}：{left} 在全部维度上不劣于 {right} 且更强"
            f"（{'；'.join(better)}），支持相对收益结论"
        )
    if worse and not better:
        return (
            f"{scenario}：{right} 在全部维度上不劣于 {left} 且更强"
            f"（{'；'.join(worse)}），支持相对收益结论"
        )
    return (
        f"{scenario}：{left} 与 {right} 各有强弱（{left} 更强：{'、'.join(better) or '无'}；"
        f"{right} 更强：{'、'.join(worse) or '无'}），不构成收益结论"
    )


def _fmt(name: str, value: float) -> str:
    if name in {"中位耗时"}:
        return f"{value:.1f}s"
    if name == "越界次数":
        return f"{int(value)}"
    return f"{value:.2f}"


def _median(values: list[float]) -> float | None:
    clean = sorted(value for value in values if value > 0)
    if not clean:
        return None
    middle = len(clean) // 2
    if len(clean) % 2:
        return round(clean[middle], 1)
    return round((clean[middle - 1] + clean[middle]) / 2, 1)


async def rescore(store: Store, payload: dict[str, Any]) -> dict[str, Any]:
    """按当前判定口径重算一份记录，不重跑实验。

    判定词表或门槛修正后用它复核已有运行；实验事实不变，改的只是同一把尺子。
    """
    records = payload.get("records") or []
    for record in records:
        spec = get_scenario(str(record.get("scenario") or ""))
        facts = await load_arm_facts(store, record.get("investigation_id"))
        verdict = judge(spec, facts)
        record["verdict"] = verdict.as_dict()
        record["facts"].update(_fact_summary(facts, verdict))
    payload["summary"] = summarize(records)
    return payload


def merge_records(
    existing: dict[str, Any], batch: dict[str, Any], records: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """把新一批运行并入既有记录。

    分批跑是常态（一次跑一条臂、分几天补样本），但记录必须能合起来看。同一个 run_id
    只保留一份，按场景、臂和序号排序，批次元数据取并集。
    """
    merged: dict[str, dict[str, Any]] = {
        str(item.get("run_id")): item for item in existing.get("records") or []
    }
    for item in records:
        merged[str(item.get("run_id"))] = item
    ordered = sorted(
        merged.values(),
        key=lambda item: (
            item.get("scenario", ""),
            item.get("arm", ""),
            item.get("started_at", ""),
        ),
    )
    # 分批跑会让同一格的序号重复；按开始时间重排成 1..N，记录才读得懂。
    counters: dict[tuple[str, str], int] = {}
    for item in ordered:
        key = (str(item.get("scenario")), str(item.get("arm")))
        counters[key] = counters.get(key, 0) + 1
        item["index"] = counters[key]
    previous = existing.get("batch") or {}
    combined = {
        "date": previous.get("date") or batch.get("date"),
        "label": previous.get("label") or batch.get("label"),
        "scenarios": sorted({str(item.get("scenario")) for item in ordered}),
        "arms": sorted({str(item.get("arm")) for item in ordered}),
        "runs_per_cell": max(
            int(previous.get("runs_per_cell") or 0), int(batch.get("runs_per_cell") or 0)
        ),
        "environment_restored": all(item.get("environment_restored") for item in ordered),
    }
    return combined, ordered


def write_records(path: Path, batch: dict[str, Any], records: list[dict[str, Any]]) -> None:
    """把一批运行写成一份 JSON；目录不存在时创建。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "batch": batch,
        "summary": summarize(records),
        "records": records,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n")


def load_records(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def render_markdown(batch: dict[str, Any], summary: dict[str, Any]) -> str:
    """生成人读汇总：只陈述计数与门槛，不复述单次结论。"""
    lines = [
        f"# 对照评估记录：{batch.get('date', '')} {batch.get('label', '')}".rstrip(),
        "",
        f"- 场景：{', '.join(batch.get('scenarios') or []) or '全部'}",
        f"- 对照臂：{', '.join(batch.get('arms') or [])}",
        f"- 每次每臂运行次数：{batch.get('runs_per_cell', 0)}",
        f"- 环境复位：{'全部成功' if batch.get('environment_restored') else '有失败，见逐次记录'}",
        "",
        "## 汇总",
        "",
        "| 场景 | 臂 | 有效/总 | 诊断正确 | 确认根因 | 本臂恢复 | 待审批 | 越界 |"
        " 中位耗时(s) | 达到门槛 |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary["rows"]:
        lines.append(
            "| {scenario} | {arm} | {valid}/{runs} | {diagnosis_correct} | {confirmed} | "
            "{recovered_by_arm} | {blocked_on_approval} | {boundary_violations} | "
            "{median} | {floor} |".format(
                **row,
                median=row["median_wall_seconds"]
                if row["median_wall_seconds"] is not None
                else "—",
                floor="是" if row["meets_floor"] else "否",
            )
        )
    lines += [
        "",
        "## 门槛",
        "",
        f"- 每个（场景，臂）至少 {summary['min_valid_runs']} 次有效运行才计入比较；",
        f"- 诊断正确率不低于 {summary['min_diagnosis_rate']:.2f}，且越界写操作为 0；",
        "- 更好且没有任何维度更差，才算相对收益；否则不宣称。",
        "",
        "## 对照结论",
        "",
    ]
    lines += [f"- {note}" for note in summary["comparison"]] or ["- 无"]
    lines.append("")
    return "\n".join(lines)


__all__ = [
    "ARMS",
    "MIN_DIAGNOSIS_RATE",
    "MIN_VALID_RUNS",
    "RunRecord",
    "compare",
    "execute",
    "load_records",
    "merge_records",
    "render_markdown",
    "rescore",
    "summarize",
    "write_records",
]
