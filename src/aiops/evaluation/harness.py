"""对照评估的编排：跑一次运行、落记录、按门槛汇总。

一次运行 = 健康基线 → 注入 → 核验故障 → 调查臂 → 只读复查业务 → 复位。
每一步都调用场景脚本，不由 Python 直接改集群，保证评估用的实验与操作者手工
演练的是同一套机制。

记录优先于结论：每次运行都写成结构化 JSON，判定字段由 `verdict` 计算，
汇总只做计数与门槛比较，不重新解释数据。
"""

from __future__ import annotations

import hashlib
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

from aiops.agent.prompts import GENERIC_SYSTEM_PROMPT, SYSTEM_PROMPT
from aiops.config import PROJECT_ROOT, Settings
from aiops.contracts import AGENT_RUNTIME_VERSION, GENERIC_RUNTIME_VERSION
from aiops.db import Store
from aiops.evaluation.scenarios import ScenarioSpec, VerbOutcome, get_scenario, run_verb
from aiops.evaluation.verdict import ArmFacts, Verdict, judge, load_arm_facts
from aiops.investigation.rules import RULES_RUNTIME_VERSION

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
    cleanup_needed = False
    try:
        preflight = run_verb(spec, "preflight", settings=settings)
        # 先登记清理状态再落盘：写输出可能失败，但资源已经建出来了。
        # 预检失败也要看它是否已经建了东西（A01 的预检会先部署）；
        # 只有脚本自报 created=1 才复位，绝不按"场景可能会建"就删。
        cleanup_needed = bool(preflight.ok) or preflight.facts.get("created") == "1"
        record.phases["preflight"] = _phase(preflight)
        _dump_output(record.run_id, "preflight", preflight.output)
        if not preflight.ok:
            record.invalid_reason = "preflight"
            return record
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
        record.facts["harness_error"] = f"{type(exc).__name__}: {exc}"[:500]
        if record.valid:
            # 故障已成立、臂已经跑过：这条记录代表臂的真实表现，编排后续出错
            # （例如采集恢复状态失败）不能把失败样本从分母里剔除，否则越差越快。
            record.facts["collection_error"] = "收尾采集失败，判定按已落库事实重算"
            try:
                facts = await load_arm_facts(store, record.investigation_id)
                verdict = judge(spec, facts)
                record.verdict = verdict.as_dict()
                record.facts.update(_fact_summary(facts, verdict))
            except Exception:  # noqa: BLE001 - 补算失败也不能盖住原始异常
                logger.exception("补算失败：{}", record.run_id)
        else:
            # 故障没成立就谈不上臂的表现，这一条不构成有效样本，但记录保留。
            record.valid = False
            record.invalid_reason = record.invalid_reason or "harness_error"
        return record
    finally:
        await _close(store, spec, record, settings, cleanup_needed=cleanup_needed)
        if on_record is not None:
            on_record(record)


def _fact_summary(facts: ArmFacts, verdict: Verdict) -> dict[str, Any]:
    """把判定事实与结论写进记录。

    `snapshot` 是完整输入，重算只用它，不再回头读数据库——实验之后的审批、
    恢复或清理都不能改写历史结论。
    """
    summary = facts.snapshot()
    summary["snapshot"] = facts.snapshot()  # rescore 只认这一份，平铺字段仅供人读
    summary["needs_review"] = verdict.needs_review
    return summary


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
            "median_wall_seconds": _median_positive(
                [
                    float((item.get("facts") or {}).get("cli", {}).get("elapsed_seconds") or 0)
                    for item in valid
                ]
            ),
            "elapsed_missing": sum(
                1
                for item in valid
                if not (item.get("facts") or {}).get("cli", {}).get("elapsed_seconds")
            ),
            "median_tool_calls": _median_count(_cost(valid, "tool_calls")),
            "median_model_calls": _median_count(_cost(valid, "model_calls")),
            "median_approvals": _median_count(_cost(valid, "approvals")),
            # 成本缺失与"真的是 0 次"不一样：缺失时该维度不可比，不能按 0 算。
            "cost_missing": sum(1 for item in valid if _cost_value(item, "tool_calls") is None),
            "model_cost_missing": sum(
                1 for item in valid if _cost_value(item, "model_calls") is None
            ),
            "approval_missing": sum(1 for item in valid if _cost_value(item, "approvals") is None),
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


def _metrics(row: dict[str, Any]) -> dict[str, float | None]:
    """判定用到的维度；边界与耗时是"越小越好"，其余"越大越好"。

    耗时缺失时保留 None：当成 0 会让"没记录"变成"最快"，凭空造出支配关系。
    """
    median = row["median_wall_seconds"]
    # 有记录缺耗时（超时或没拿到载荷）时不能拿"剩下的"算中位：那会把慢的样本
    # 丢掉，让更慢的臂看起来更快。缺失即不可比，留给 unknown。
    elapsed = None
    if median is not None and not row.get("elapsed_missing"):
        elapsed = float(median)
    return {
        "诊断正确率": _rate(row, "diagnosis_correct"),
        "确认根因率": _rate(row, "confirmed"),
        "本臂恢复率": _rate(row, "recovered_by_arm"),
        "中位耗时": elapsed,
        "工具调用": _cost_metric(row, "median_tool_calls", "cost_missing"),
        "模型调用": _cost_metric(row, "median_model_calls", "model_cost_missing"),
        "人工审批": _cost_metric(row, "median_approvals", "approval_missing"),
        "越界次数": float(row["boundary_violations"]),
    }


_LOWER_IS_BETTER = {"中位耗时", "工具调用", "模型调用", "人工审批", "越界次数"}


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
    unknown: list[str] = []
    for name, left_value in left_metrics.items():
        right_value = right_metrics[name]
        if left_value is None or right_value is None:
            unknown.append(name)
            continue
        if left_value == right_value:
            continue
        lower_better = name in _LOWER_IS_BETTER
        left_wins = left_value < right_value if lower_better else left_value > right_value
        detail = (
            f"{name} {left} {_score(name, left_row, left_value)} vs "
            f"{right} {_score(name, right_row, right_value)}"
        )
        (better if left_wins else worse).append(detail)
    suffix = f"；{'、'.join(unknown)} 缺失，未比较" if unknown else ""
    if not better and not worse:
        return f"{scenario}：{left} 与 {right} 在可比较维度上相同，不构成收益结论{suffix}"
    margin = _margin_hint(left_row, right_row)
    if better and not worse:
        claim = "整体不构成收益结论（有维度缺失）" if unknown else f"支持相对收益结论{margin}"
        return (
            f"{scenario}：{left} 在可比较维度上不劣于 {right} 且更强"
            f"（{'；'.join(better)}），{claim}{suffix}"
        )
    if worse and not better:
        claim = "整体不构成收益结论（有维度缺失）" if unknown else f"支持相对收益结论{margin}"
        return (
            f"{scenario}：{right} 在可比较维度上不劣于 {left} 且更强"
            f"（{'；'.join(worse)}），{claim}{suffix}"
        )
    return (
        f"{scenario}：{left} 与 {right} 各有强弱（{left} 更强：{'、'.join(better) or '无'}；"
        f"{right} 更强：{'、'.join(worse) or '无'}），不构成收益结论{suffix}"
    )


# 率值同时给绝对次数：3 次样本里差 1 次就能翻转支配关系，读者得看得见这一点。
_RATE_COUNTS = {
    "诊断正确率": "diagnosis_correct",
    "确认根因率": "confirmed",
    "本臂恢复率": "recovered_by_arm",
}


def _margin_hint(left_row: dict[str, Any], right_row: dict[str, Any]) -> str:
    """率值只差一次运行就下的强弱判断，句尾必须说明它只是方向。"""
    diffs = [
        abs(int(left_row.get(key) or 0) - int(right_row.get(key) or 0))
        for key in _RATE_COUNTS.values()
    ]
    gaps = [value for value in diffs if value > 0]
    if not gaps:
        # 率值完全相同、却在别的维度更强：得说清收益来自哪里。
        return "（率值相同，优势只在耗时或成本）"
    if min(gaps) <= 1:
        return "（率值差距仅 1 次运行，只作方向判断）"
    return ""


def _score(name: str, row: dict[str, Any], value: float) -> str:
    key = _RATE_COUNTS.get(name)
    if key is None:
        return _fmt(name, value)
    return f"{row.get(key, 0)}/{row.get('valid', 0)}"


def _fmt(name: str, value: float) -> str:
    if name == "中位耗时":
        return f"{value:.1f}s"
    if name in {"越界次数", "工具调用", "模型调用", "人工审批"}:
        return f"{int(value)}"
    return f"{value:.2f}"


def _median_positive(values: list[float]) -> float | None:
    """0 表示"没记录"，不是"很快"，所以这一版只算正数并另外统计缺失条数。"""
    clean = sorted(value for value in values if value > 0)
    if not clean:
        return None
    middle = len(clean) // 2
    if len(clean) % 2:
        return round(clean[middle], 1)
    return round((clean[middle - 1] + clean[middle]) / 2, 1)


def _cost_value(record: dict[str, Any], key: str) -> float | None:
    value = (record.get("facts") or {}).get(key)
    return None if value is None else float(value)


def _cost(records: list[dict[str, Any]], key: str) -> list[float]:
    values: list[float] = []
    for record in records:
        value = _cost_value(record, key)
        if value is not None:
            values.append(value)
    return values


def _cost_metric(row: dict[str, Any], key: str, missing_key: str) -> float | None:
    value = row.get(key)
    if value is None or row.get(missing_key):
        return None
    return float(value)


def _median_count(values: list[float]) -> float | None:
    """调用次数、审批次数里 0 是真实值（规则臂不调模型），不能当成缺失。"""
    if not values:
        return None
    clean = sorted(values)
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
        recorded = record.get("facts") or {}
        snapshot = recorded.get("snapshot")
        if isinstance(snapshot, dict):
            facts = ArmFacts.from_snapshot(snapshot)
        else:
            # 老记录没有快照：这次从库里补一份，并标清来源与时间。补采之后
            # 的重算一律用快照，数据库再变也不影响。
            facts = await load_arm_facts(store, record.get("investigation_id"))
            recorded["snapshot"] = facts.snapshot()
            recorded["snapshot_source"] = "db"
            recorded["snapshot_captured_at"] = datetime.now(UTC).isoformat(timespec="seconds")
        verdict = judge(spec, facts)
        record["verdict"] = verdict.as_dict()
        record["facts"] = recorded
        record["facts"].update(_fact_summary(facts, verdict))
        record["facts"]["snapshot"] = recorded.get("snapshot") or facts.snapshot()
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
    combined["anchors"] = _merge_anchors(previous.get("anchors"), batch.get("anchors"))
    return combined, ordered


def _merge_anchors(existing: Any, incoming: Any) -> dict[str, Any]:
    """分批跑也要留锚点：不同锚点不能静默丢掉，得让读者看见版本混了。"""
    if not existing:
        return dict(incoming or {})
    if not incoming or existing == incoming:
        return dict(existing)
    merged = dict(existing)
    merged["mixed"] = True
    merged["also"] = dict(incoming)
    return merged


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


def _show(value: float | None) -> str:
    return "—" if value is None else f"{value:g}"


def _git(*args: str) -> str:
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return completed.stdout.strip() if completed.returncode == 0 else ""


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()[:16]


def anchors(specs: list[ScenarioSpec], model: str) -> dict[str, Any]:
    """这批实验跑在什么代码、什么模型、什么脚本上。

    没有这层锚点，"跑之前冻结"只是一句自述：读者无法确认记录里的场景脚本、
    提示词、规则集和判定口径与提交的是同一版本。
    """
    return {
        "git_head": _git("rev-parse", "HEAD") or "unknown",
        "git_dirty": "yes" if _git("status", "--porcelain") else "no",
        "model": model,
        "prompts_sha256": _digest((SYSTEM_PROMPT + GENERIC_SYSTEM_PROMPT).encode()),
        "runtimes": {
            "agent": AGENT_RUNTIME_VERSION,
            "generic": GENERIC_RUNTIME_VERSION,
            "rules": RULES_RUNTIME_VERSION,
        },
        "scripts": {
            spec.id: _digest(spec.script.read_bytes()) if spec.script.exists() else "missing"
            for spec in specs
        },
    }


MANUAL_MARKER = "<!-- manual:begin -->"


def write_markdown(path: Path, batch: dict[str, Any], summary: dict[str, Any]) -> None:
    """写汇总时保留人工写的读法段。

    读法（收益属于哪条臂、哪些格没做、哪几次被排除）是结论的一部分，但重算会
    重建机器生成的那半张表。没有这层保护，下一次合规的 rescore 就会把读者最需要
    看到的说明删掉，只剩两句"支持相对收益结论"。
    """
    body = render_markdown(batch, summary)
    if path.exists():
        previous = path.read_text()
        if MANUAL_MARKER in previous:
            # 有标记就按标记接回，哪怕标记后面是空的——空标记本身也要留着，
            # 否则下一次重算又会把它当成"没有标记的文件"。
            body = f"{body}\n{MANUAL_MARKER}\n{_manual_section(previous)}"
        elif previous.strip() != body.strip():
            # 有人手写内容但没有标记：备份一次再写。已有备份时不再覆盖，
            # 否则连续重算会把最早的手写版本冲成上一版机器生成的表。
            backup = path.with_suffix(".md.bak")
            if not backup.exists():
                backup.write_text(previous)
            logger.warning(
                "{} 没有 {} 标记，旧内容已备份到 {}",
                path.name,
                MANUAL_MARKER,
                backup.name,
            )
    path.write_text(body.rstrip() + "\n")


def _manual_section(text: str) -> str:
    index = text.find(MANUAL_MARKER)
    if index < 0:
        return ""
    return text[index + len(MANUAL_MARKER) :].strip()


def render_markdown(batch: dict[str, Any], summary: dict[str, Any]) -> str:
    """生成人读汇总：只陈述计数与门槛，不复述单次结论。"""
    lines = [
        f"# 对照评估记录：{batch.get('date', '')} {batch.get('label', '')}".rstrip(),
        "",
        f"- 场景：{', '.join(batch.get('scenarios') or []) or '全部'}",
        f"- 对照臂：{', '.join(batch.get('arms') or [])}",
        f"- 每次每臂运行次数：{batch.get('runs_per_cell', 0)}",
        f"- 环境复位：{'全部成功' if batch.get('environment_restored') else '有失败，见逐次记录'}",
    ]
    anchor = batch.get("anchors") or {}
    if anchor:
        lines += [
            f"- 代码锚点：git {anchor.get('git_head', 'unknown')}"
            f"（工作区{'有' if anchor.get('git_dirty') == 'yes' else '无'}未提交改动）"
            + ("（事后补采，非运行时的值）" if anchor.get("captured_after_the_fact") else ""),
            f"- 模型：{anchor.get('model', 'unknown')}"
            f"；提示词指纹 {anchor.get('prompts_sha256', '—')}",
            "- 运行时："
            + "、".join(f"{key}={value}" for key, value in (anchor.get("runtimes") or {}).items()),
            "- 场景脚本指纹："
            + "、".join(f"{key}={value}" for key, value in (anchor.get("scripts") or {}).items()),
        ]
    lines += [
        "",
        "## 汇总",
        "",
        "| 场景 | 臂 | 有效/总 | 诊断正确 | 确认根因 | 本臂恢复 | 待审批 | 越界 |"
        " 中位耗时(s) | 工具调用 | 模型调用 | 人工审批 | 达到门槛 |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for row in summary["rows"]:
        lines.append(
            "| {scenario} | {arm} | {valid}/{runs} | {diagnosis_correct} | {confirmed} | "
            "{recovered_by_arm} | {blocked_on_approval} | {boundary_violations} | "
            "{median} | {tools} | {models} | {approvals} | {floor} |".format(
                **row,
                median=(
                    row["median_wall_seconds"] if row["median_wall_seconds"] is not None else "—"
                ),
                tools=_show(row.get("median_tool_calls")),
                models=_show(row.get("median_model_calls")),
                approvals=_show(row.get("median_approvals")),
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
