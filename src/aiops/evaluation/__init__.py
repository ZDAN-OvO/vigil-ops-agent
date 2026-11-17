"""对照评估：场景驱动、判定口径与汇总。

模块边界见 [ADR-0013](../../docs/adr/0013-comparative-evaluation.md)：场景机制仍在
`faults/`，本包只负责把实验跑成可比较的记录。
"""

from aiops.evaluation.harness import ARMS, RunRecord, execute, summarize
from aiops.evaluation.scenarios import SCENARIOS, ScenarioSpec, get_scenario, run_verb
from aiops.evaluation.verdict import ArmFacts, Verdict, judge, load_arm_facts

__all__ = [
    "ARMS",
    "SCENARIOS",
    "ArmFacts",
    "RunRecord",
    "ScenarioSpec",
    "Verdict",
    "execute",
    "get_scenario",
    "judge",
    "load_arm_facts",
    "run_verb",
    "summarize",
]
