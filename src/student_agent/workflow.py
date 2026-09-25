from __future__ import annotations

import re
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run one observable, deterministic A2A workflow without inventing evidence."""
    case_id = _required_string(case, "case_id")
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id") if isinstance(request, dict) else None
    if not isinstance(order_id, str) or not order_id:
        order_id = None

    available = set(await gateway.list_tools())
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="order-agent")
    evidence: list[dict[str, Any]] = []
    await _run_specialist(case_id, gateway, trace, available, evidence, "order-agent", "order", order_id)
    trace.emit(case_id=case_id, event_type="handoff", actor="order-agent", target="payment-agent")
    await _run_specialist(case_id, gateway, trace, available, evidence, "payment-agent", "payment", order_id)
    trace.emit(case_id=case_id, event_type="handoff", actor="payment-agent", target="shipment-agent")
    await _run_specialist(case_id, gateway, trace, available, evidence, "shipment-agent", "shipment", order_id)
    trace.emit(case_id=case_id, event_type="handoff", actor="shipment-agent", target="policy-agent")
    await _run_specialist(
        case_id, gateway, trace, available, evidence, "policy-agent", "policy", case.get("policy_version")
    )

    output = _build_output(case, evidence)
    trace.emit(
        case_id=case_id,
        event_type="policy_decided",
        actor="policy-agent",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"],
    )
    output = _verify_output(output, evidence)
    trace.emit(
        case_id=case_id,
        event_type="verification_completed",
        actor="verifier",
        decision_code=output["assessment"]["primary_issue"],
        evidence_refs=output["evidence_refs"],
        attributes={"evidence_count": len(evidence)},
    )
    return output


async def _run_specialist(
    case_id: str,
    gateway: EvidenceGateway,
    trace: TraceWriter,
    available: set[str],
    evidence: list[dict[str, Any]],
    actor: str,
    domain: str,
    identifier: str | None,
) -> None:
    tool = _select_tool(available, domain)
    if tool is None:
        return
    arguments = {"order_id": identifier} if identifier else {}
    if domain == "policy" and identifier:
        arguments = {"policy_version": identifier}
    try:
        result = await gateway.call(tool, case_id=case_id, **arguments)
    except (RuntimeError, ValueError, TypeError):
        return
    evidence.append(result)
    trace.emit(
        case_id=case_id,
        event_type="tool_result_consumed",
        actor=actor,
        tool_name=tool,
        evidence_refs=[result["evidence_ref"]],
    )


def _select_tool(available: set[str], domain: str) -> str | None:
    candidates = sorted(
        name for name in available if domain in re.sub(r"[^a-z0-9]", "", name.lower())
    )
    return candidates[0] if candidates else None


def _build_output(case: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    topics = [
        claim.get("topic") for claim in claims
        if isinstance(claim, dict) and isinstance(claim.get("topic"), str)
    ]
    refs = [item["evidence_ref"] for item in evidence]
    data = [item.get("data") for item in evidence]
    issue = _primary_issue(topics, data)
    supported_topics = _supported_topics(data)
    issue_supported = issue in supported_topics
    confidence = 0.8 if issue_supported and len(refs) >= 2 else 0.55 if issue_supported else 0.15
    claim_assessments = [
        {
            "claim_id": claim.get("claim_id", f"claim-{index}"),
            "verdict": "supported" if claim.get("topic") in supported_topics else "insufficient_evidence",
            "confidence": confidence if claim.get("topic") in supported_topics else 0.15,
            "evidence_refs": refs,
        }
        for index, claim in enumerate(claims, 1)
        if isinstance(claim, dict)
    ]
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue,
            "case_status": "needs_investigation" if issue == "insufficient_evidence" else "action_required",
            "confidence": confidence,
        },
        "affected_entities": _entities(case, data),
        "claim_assessments": claim_assessments[:5],
        "root_cause_analysis": _root_cause(issue, data),
        "evidence_refs": refs[:30],
        "data_conflicts": [],
        "financial_resolution": _financial_resolution(issue, case, data),
        "resolution_actions": _resolution_actions(issue, refs),
    }


def _verify_output(output: dict[str, Any], evidence: list[dict[str, Any]]) -> dict[str, Any]:
    refs = {item["evidence_ref"] for item in evidence}
    output["evidence_refs"] = [ref for ref in output["evidence_refs"] if ref in refs]
    output["assessment"]["confidence"] = min(1.0, max(0.0, output["assessment"]["confidence"]))
    if output["assessment"]["primary_issue"] == "insufficient_evidence":
        output["assessment"]["case_status"] = "needs_investigation"
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.15)
        output["root_cause_analysis"] = {"ranked_causes": [], "responsible_parties": []}
        output["financial_resolution"] = {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []}
    return output


def _root_cause(issue: str, data: list[Any]) -> dict[str, Any]:
    party_map = {
        "late_delivery_seller": "seller",
        "late_delivery_logistics": "logistics_provider",
        "payment_mismatch": "payment_provider",
        "duplicate_charge": "payment_provider",
        "canceled_order_paid": "platform",
        "unavailable_order_paid": "platform",
    }
    party_type = party_map.get(issue)
    if not party_type:
        return {"ranked_causes": [], "responsible_parties": []}
    party_id = _first_value(data, "seller_id") if party_type == "seller" else None
    return {
        "ranked_causes": [{"cause_code": issue.upper(), "rank": 1}],
        "responsible_parties": [{"party_type": party_type, "party_id": party_id}],
    }


def _financial_resolution(issue: str, case: dict[str, Any], data: list[Any]) -> dict[str, Any]:
    amount = _first_number(data, "recommended_refund_brl", "refund_amount_brl", "amount_brl")
    if amount is None or issue not in {"canceled_order_paid", "unavailable_order_paid", "refund_pending", "refund_failed"}:
        return {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []}
    order_id = case.get("customer_request", {}).get("claimed_order_id")
    return {
        "currency": "BRL",
        "recommended_refund_brl": amount,
        "refund_lines": [{"reason_code": issue, "amount_brl": amount, "entity_id": order_id}],
    }


def _resolution_actions(issue: str, refs: list[str]) -> list[str]:
    if not refs:
        return ["collect_missing_evidence"]
    if issue in {"canceled_order_paid", "unavailable_order_paid", "refund_pending", "refund_failed"}:
        return ["process_refund"]
    if issue in {"late_delivery_seller", "late_delivery_logistics"}:
        return ["review_delivery_breach"]
    if issue in {"payment_mismatch", "duplicate_charge"}:
        return ["review_payment"]
    return ["review_case"]


def _first_value(data: list[Any], key: str) -> str | None:
    for value in data:
        found = _find_value(value, key)
        if isinstance(found, str):
            return found
    return None


def _first_number(data: list[Any], *keys: str) -> float | None:
    for value in data:
        for key in keys:
            found = _find_value(value, key)
            if isinstance(found, (int, float)) and found >= 0:
                return float(found)
    return None


def _find_value(value: Any, wanted_key: str) -> Any:
    if isinstance(value, dict):
        if wanted_key in value:
            return value[wanted_key]
        for nested in value.values():
            found = _find_value(nested, wanted_key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_value(nested, wanted_key)
            if found is not None:
                return found
    return None


def _entities(case: dict[str, Any], data: list[Any]) -> dict[str, list[str]]:
    values = {
        "order_ids": [], "item_ids": [], "seller_ids": [],
        "payment_references": [], "shipment_ids": [],
    }
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    if isinstance(claimed, str):
        values["order_ids"].append(claimed)
    for item in data:
        _collect_entities(item, values)
    return {key: list(dict.fromkeys(value))[:20] for key, value in values.items()}


def _collect_entities(value: Any, values: dict[str, list[str]]) -> None:
    if isinstance(value, dict):
        mapping = {
            "order_id": "order_ids", "item_id": "item_ids", "seller_id": "seller_ids",
            "payment_id": "payment_references", "payment_reference": "payment_references",
            "shipment_id": "shipment_ids",
        }
        for key, item in value.items():
            target = mapping.get(key)
            if target and isinstance(item, str):
                values[target].append(item)
            _collect_entities(item, values)
    elif isinstance(value, list):
        for item in value:
            _collect_entities(item, values)


def _supported_topics(data: list[Any]) -> set[str]:
    text = str(data).lower()
    topics = (
        "canceled_order_paid", "late_delivery_seller", "late_delivery_logistics",
        "payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed",
    )
    return {topic for topic in topics if topic.replace("_", " ") in text}


def _primary_issue(topics: list[str], data: list[Any]) -> str:
    supported = _supported_topics(data)
    for topic in topics:
        if topic in supported:
            return topic
    if topics and topics[0] in {"valid_split_payment", "unsupported_claim"}:
        return topics[0]
    return "insufficient_evidence"


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"case is missing {key}")
    return item
