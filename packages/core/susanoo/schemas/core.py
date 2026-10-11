"""Core schemas. [PRD › Contracts (locked) › Core schemas]

Frozen: change only with both owners' agreement (see the contract-change skill).
"""

from typing import Literal

from pydantic import BaseModel

Layer = Literal["code", "tool", "prompt_policy", "test"]
Severity = Literal["block", "warn", "nit"]
Split = Literal["train", "heldout", "acceptance"]


class Rule(BaseModel):
    id: str  # "no-raw-sql"
    text: str  # plain English from the user
    default_severity: Severity


class Plan(BaseModel):
    product_name: str
    summary: str
    rules: list[Rule]
    intake_answers: dict[str, str]
    files_to_generate: list[str]
    acceptance_criteria: list[str]


class AgentBundle(BaseModel):
    prompt_path: str  # agent/prompt.md
    policy_path: str  # agent/policy.yaml
    tools_path: str  # agent/tools.py
    model: str


class ExpectedIssue(BaseModel):
    file: str
    line_start: int  # new-file line numbers
    line_end: int
    rule_id: str
    severity: Severity


class AnswerKey(BaseModel):
    expected: list[ExpectedIssue]  # empty = clean diff
    must_not_flag: list[str] = []  # rule_ids that must NOT fire


class Scenario(BaseModel):
    id: str
    kind: Literal["planted", "edge", "adversarial", "clean"]
    split: Split
    title: str  # plain-English card
    diff: str
    answer_key: AnswerKey
    approved_by_user: bool = False


class Finding(BaseModel):
    file: str
    line: int
    rule_id: str  # must be a policy rule id
    severity: Severity
    message: str


class ScenarioResult(BaseModel):
    scenario_id: str
    iteration: int
    findings: list[Finding]
    matched: int
    missed: int
    false_positives: int
    severity_correct: int
    passed: bool
    trace_id: str  # neatlogs
    error: str | None = None


class Diagnosis(BaseModel):
    id: str
    scenario_ids: list[str]
    layer: Layer
    hypothesis: str  # one paragraph, plain English
    evidence_span_ids: list[str]
    target_files: list[str]  # must pass the layer allow-list


class Patch(BaseModel):
    id: str
    diagnosis_id: str
    commit_sha: str
    checkpoint_id: str | None  # Entire
    lines_changed: int
    verdict: Literal["pending", "kept", "rolled_back", "awaiting_approval"]


class IterationMetrics(BaseModel):
    iteration: int
    recall: float
    precision: float
    f1: float
    severity_accuracy: float
    heldout_f1: float
    acceptance_pass_rate: float
    cost_usd: float
