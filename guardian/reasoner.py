"""Agent 2.0 structured reasoning contract and validation.

The model may interpret evidence, but it may not invent measurements or cite
evidence that does not exist.  Invalid output is rejected and replaced by a
deterministic, evidence-grounded report.
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .evidence import EvidenceBundle


STRUCTURED_PROMPT = """你是 SenseMind Agent 2.0 的调查推理器（研究原型，不是医疗器械）。
你只能使用服务端提供的 evidence 列表和只读工具结果，不能编造任何测量值、时间或事件。
每条 observation 必须引用至少一个真实存在的 evidence_id；conclusion 也必须可追溯到证据。
如果证据不足、质量差、传感器不可用或时间无法可靠对齐，investigation_status 必须是 inconclusive。
不得输出诊断、疾病概率、发作预测、用药建议或急救承诺；不得建议系统自动触发高风险告警。
recommended_action 只能是核对设备、人工复核、补充数据、联系专业人员等保守建议。
输出要短：最多 4 条 observation，每条不超过 120 字，总输出不超过 800 字。
只输出一个 JSON 对象，不要输出 Markdown：
{"event_id":"...","investigation_status":"completed|inconclusive",
 "observations":[{"text":"...","evidence_refs":["ev_..."]}],
 "evidence_refs":["ev_..."],"uncertainties":["..."],
 "conflicting_evidence":["..."],"missing_data":["..."],
 "conclusion":"...","recommended_action":["..."]}
"""

RISKY_ACTION_MARKERS = ("发送报警", "触发告警", "自动报警", "拨打急救",
                        "send alert", "trigger alarm", "emergency call")


class StructuredObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=600)
    evidence_refs: list[str] = Field(min_length=1, max_length=8)


class StructuredAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(min_length=1, max_length=80)
    investigation_status: Literal["completed", "inconclusive"]
    observations: list[StructuredObservation] = Field(default_factory=list, max_length=10)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    uncertainties: list[str] = Field(default_factory=list, max_length=10)
    conflicting_evidence: list[str] = Field(default_factory=list, max_length=10)
    missing_data: list[str] = Field(default_factory=list, max_length=10)
    conclusion: str = Field(default="", max_length=1200)
    recommended_action: list[str] = Field(default_factory=list, max_length=8)


NUMBER_RE = re.compile(r"(?<![\w.])-?\d+(?:\.\d+)?(?![\w.])")
EVIDENCE_RE = re.compile(r"ev_[0-9a-fA-F]+")
YEARS = {str(year) for year in range(2000, 2101)}
COMMON_NUMBERS = {str(value) for value in
                  list(range(0, 13)) + [15, 20, 24, 30, 31, 60, 72,
                                        120, 200, 300, 600, 1000, 2000]}


def parse_structured(text: str) -> StructuredAnalysis:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("invalid_response")
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```(?:json)?\s*", "", body)
        body = re.sub(r"\s*```$", "", body)
    start, end = body.find("{"), body.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("invalid_response")
    try:
        data = json.loads(body[start:end + 1])
    except json.JSONDecodeError:
        raise ValueError("invalid_response") from None
    try:
        return StructuredAnalysis.model_validate(data)
    except ValidationError:
        raise ValueError("invalid_response") from None


def _add_numbers(target: set[str], value) -> None:
    if value is None:
        return
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float)):
        target.add(str(value))
        if isinstance(value, float) and value.is_integer():
            target.add(str(int(value)))
        return
    text = value if isinstance(value, str) else json.dumps(
        value, ensure_ascii=False, default=str)
    for token in NUMBER_RE.findall(text):
        target.add(token)
        target.add(token.lstrip("-"))


def allowed_numbers(bundle: EvidenceBundle, event: dict,
                    evidence_refs: list[str] | None = None,
                    extra_context: dict | None = None) -> set[str]:
    ids = set(evidence_refs or [item.evidence_id for item in bundle.evidence])
    allowed = set(COMMON_NUMBERS) | set(YEARS)
    allowed.add(str(len(bundle.evidence)))
    _add_numbers(allowed, event.get("opened_at"))
    _add_numbers(allowed, event.get("note"))
    _add_numbers(allowed, extra_context)
    for item in bundle.evidence:
        if item.evidence_id not in ids:
            continue
        _add_numbers(allowed, item.value)
        _add_numbers(allowed, item.unit)
        _add_numbers(allowed, item.timestamp.isoformat() if item.timestamp else None)
        _add_numbers(allowed, item.model_version)
    return allowed


def evidence_sufficient(bundle: EvidenceBundle) -> bool:
    """Conservative minimum: at least one usable physiological observation."""
    usable = [item for item in bundle.evidence
              if item.source_type in ("heart_rate_estimate", "ppg",
                                      "breathing_rate", "imu_raw", "activity")
              and item.value is not None
              and item.quality_status in ("good", "poor")]
    return len(usable) >= 1 and len(bundle.evidence) >= 2


def validate_structured(analysis: StructuredAnalysis, *, event: dict,
                        bundle: EvidenceBundle,
                        extra_context: dict | None = None) -> StructuredAnalysis:
    if analysis.event_id != event["id"]:
        raise ValueError("event_id_mismatch")
    ids = set(bundle.by_id())
    refs = set(analysis.evidence_refs)
    if not refs and not analysis.observations:
        raise ValueError("no_evidence_references")
    if not refs <= ids:
        raise ValueError("invalid_evidence_ref")
    if not analysis.observations:
        raise ValueError("no_observations")
    for observation in analysis.observations:
        if not set(observation.evidence_refs) <= ids:
            raise ValueError("invalid_evidence_ref")
        allowed = allowed_numbers(bundle, event, observation.evidence_refs,
                                  extra_context)
        for token in NUMBER_RE.findall(observation.text):
            if token not in allowed and token.lstrip("-") not in allowed:
                raise ValueError(f"fabricated_measurement:{token}")
    allowed = allowed_numbers(bundle, event, list(refs), extra_context)
    for token in NUMBER_RE.findall(analysis.conclusion):
        if token not in allowed and token.lstrip("-") not in allowed:
            raise ValueError(f"fabricated_measurement:{token}")
    for item in analysis.conflicting_evidence:
        for reference in EVIDENCE_RE.findall(item):
            if reference not in ids:
                raise ValueError("invalid_evidence_ref")
    if any(marker in action for action in analysis.recommended_action
           for marker in RISKY_ACTION_MARKERS):
        raise ValueError("unsafe_recommended_action")

    if analysis.investigation_status == "completed" and not evidence_sufficient(bundle):
        analysis = analysis.model_copy(update={
            "investigation_status": "inconclusive",
            "uncertainties": (analysis.uncertainties +
                              ["证据不足或质量不足，降级为 inconclusive。"])[:10],
        })
    if analysis.investigation_status == "completed" and not analysis.conclusion.strip():
        raise ValueError("missing_conclusion")
    if analysis.investigation_status == "inconclusive" and not analysis.conclusion.strip():
        analysis = analysis.model_copy(update={"conclusion": "证据不足，无法得出结论。"})
    return analysis


def map_legacy_inferences(inferences: list[str], uncertainties: list[str],
                          actions: list[str], narrative: str, *, event_id: str,
                          bundle: EvidenceBundle) -> StructuredAnalysis:
    """Map the pre-2.0 narrative response onto the structured contract.

    Legacy responses cannot be strictly grounded, so the mapping is marked by
    an explicit uncertainty and uses all evidence ids only where available.
    """
    refs = [item.evidence_id for item in bundle.evidence[:8]]
    observations = []
    for index, text in enumerate(inferences[:8]):
        refs_for_item = refs[index:index + 1] or refs[:1]
        if refs_for_item:
            observations.append(StructuredObservation(
                text=text, evidence_refs=refs_for_item))
    missing = [item.source_type + ":" + (item.reason or "unavailable")
               for item in bundle.availability if not item.available]
    return StructuredAnalysis(
        event_id=event_id,
        investigation_status="completed" if observations else "inconclusive",
        observations=observations,
        evidence_refs=refs,
        uncertainties=(list(uncertainties) +
                       ["旧版叙述式响应未能逐条绑定证据。"])[:10],
        conflicting_evidence=[],
        missing_data=missing[:10],
        conclusion=narrative or "基于现有证据生成限定性摘要。",
        recommended_action=[item for item in actions[:8]
                            if not any(marker in item for marker in RISKY_ACTION_MARKERS)],
    )


def deterministic_structured(event: dict, bundle: EvidenceBundle, *,
                             status: str | None = None,
                             reason: str | None = None,
                             conflicts: list[str] | None = None
                             ) -> StructuredAnalysis:
    usable = [item for item in bundle.evidence
              if item.value is not None and item.quality_status != "unavailable"]
    observed = usable[:8]
    observations = []
    for item in observed:
        value = item.value
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        text = (f"{item.source_type} 证据（{item.quality_status}，"
                f"{item.data_mode}）：{value}"
                + (f" {item.unit}" if item.unit else ""))
        observations.append(StructuredObservation(text=text[:600],
                                                  evidence_refs=[item.evidence_id]))
    missing = [f"{item.source_type}: {item.reason}" for item in bundle.availability
               if not item.available]
    uncertainties = []
    if not bundle.alignment.get("fusion_allowed", True):
        uncertainties.append("多设备时间无法可靠对齐，未进行同步融合："
                             + (bundle.alignment.get("reason") or "unknown"))
    if bundle.has_synthetic or "replay" in bundle.data_modes:
        uncertainties.append("包含回放或合成数据，仅用于软件逻辑验证。")
    if any(item.quality_status in ("poor", "invalid", "unknown")
           for item in bundle.evidence):
        uncertainties.append("存在低质量或未知质量的证据，结论受限。")
    if reason:
        uncertainties.append(reason)
    final_status = status or ("completed" if evidence_sufficient(bundle)
                              else "inconclusive")
    conclusion = ("基于现有证据的限定性摘要；证据不足，不能得出确定结论。"
                  if final_status == "inconclusive"
                  else "基于现有证据的限定性摘要，仍需人工复核。")
    actions = ["核对设备佩戴、连接和数据质量。",
               "由人工在控制台复核证据链。",
               "证据不足时补充采集，不要据此作出健康判断。"]
    return StructuredAnalysis(
        event_id=event["id"], investigation_status=final_status,
        observations=observations,
        evidence_refs=[item.evidence_id for item in observed],
        uncertainties=uncertainties[:10],
        conflicting_evidence=(conflicts or [])[:10],
        missing_data=missing[:10], conclusion=conclusion,
        recommended_action=actions,
    )
