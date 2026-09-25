from __future__ import annotations

import re
from typing import Any

from .mcp_gateway import EvidenceGateway
from .trace import TraceWriter


async def solve_case(
    case: dict[str, Any], gateway: EvidenceGateway, trace: TraceWriter
) -> dict[str, Any]:
    """Run the MCP workflow; semantic analysis is evidence-grounded and LLM-pluggable."""
    case_id = _required_string(case, "case_id")
    request = case.get("customer_request", {})
    order_id = request.get("claimed_order_id") if isinstance(request, dict) else None
    order_id = order_id if isinstance(order_id, str) and order_id else None
    available = set(await gateway.list_tools())
    evidence: list[dict[str, Any]] = []
    trace.emit(case_id=case_id, event_type="task_assigned", actor="coordinator", target="order-agent")

    steps = [
        ("order-agent", "order", order_id),
        ("payment-agent", "payment", order_id),
        ("shipment-agent", "shipment", order_id),
        ("policy-agent", "policy", case.get("policy_version")),
    ]
    evidence_domains: dict[str, str] = {}
    for index, (actor, domain, identifier) in enumerate(steps):
        await _run_specialist(
            case_id, gateway, trace, available, evidence, evidence_domains,
            actor, domain, identifier,
        )
        if index < len(steps) - 1:
            trace.emit(case_id=case_id, event_type="handoff", actor=actor, target=steps[index + 1][0])

    output = _build_output(case, evidence, evidence_domains)
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
    evidence_domains: dict[str, str],
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
    evidence_domains[result["evidence_ref"]] = domain
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


def _build_output(
    case: dict[str, Any], evidence: list[dict[str, Any]], evidence_domains: dict[str, str]
) -> dict[str, Any]:
    claims = _claims(case)
    refs = [item["evidence_ref"] for item in evidence]
    data = [item.get("data") for item in evidence]
    supported = _supported_topics(data)
    issue = _primary_issue([claim.get("topic") for claim in claims], supported)
    issue_supported = issue in supported
    confidence = 0.85 if issue_supported and len(refs) >= 2 else 0.65 if issue_supported else 0.15
    claim_assessments = []
    for index, claim in enumerate(claims, 1):
        topic = claim.get("topic")
        supported_claim = _claim_supported(topic, data, supported)
        claim_assessments.append({
            "claim_id": claim.get("claim_id", f"claim-{index}"),
            "verdict": "supported" if supported_claim else "insufficient_evidence",
            "confidence": confidence if supported_claim else 0.15,
            "evidence_refs": _claim_refs(topic, refs, evidence_domains),
        })
    return {
        "schema_version": "day09-l3a-output-v2",
        "case_id": case["case_id"],
        "assessment": {
            "primary_issue": issue,
            "case_status": "action_required" if issue_supported else "needs_investigation",
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
    valid_refs = {item["evidence_ref"] for item in evidence}
    output["evidence_refs"] = [ref for ref in output["evidence_refs"] if ref in valid_refs]
    output["assessment"]["confidence"] = max(0.0, min(1.0, output["assessment"]["confidence"]))
    if output["assessment"]["primary_issue"] == "insufficient_evidence":
        output["assessment"]["case_status"] = "needs_investigation"
        output["assessment"]["confidence"] = min(output["assessment"]["confidence"], 0.15)
        output["root_cause_analysis"] = {"ranked_causes": [], "responsible_parties": []}
        output["financial_resolution"] = {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []}
    return output


def _claims(case: dict[str, Any]) -> list[dict[str, Any]]:
    request = case.get("customer_request", {})
    claims = request.get("claims", []) if isinstance(request, dict) else []
    return [claim for claim in claims if isinstance(claim, dict)]


def _primary_issue(topics: list[Any], supported: set[str]) -> str:
    for topic in topics:
        if isinstance(topic, str) and topic in supported:
            return topic
    for topic in topics:
        if topic in {"valid_split_payment", "unsupported_claim"}:
            return topic
    return "insufficient_evidence"


def _claim_supported(topic: Any, data: list[Any], supported: set[str]) -> bool:
    if topic in supported:
        return True
    return topic == "requested_full_refund" and "refund" in _evidence_text(data) and _has_refund_signal(data)


def _supported_topics(data: list[Any]) -> set[str]:
    text = _evidence_text(data)
    topics: set[str] = set()
    if _has(text, "canceled", "cancelled") and _has(text, "paid", "payment approved", "approved"):
        topics.add("canceled_order_paid")
    if _has(text, "unavailable", "out of stock", "not available") and _has(text, "paid", "approved"):
        topics.add("unavailable_order_paid")
    if _has(text, "late", "delayed", "overdue"):
        if _has(text, "seller", "merchant", "vendor"):
            topics.add("late_delivery_seller")
        if _has(text, "logistics", "carrier", "shipping", "delivery provider"):
            topics.add("late_delivery_logistics")
    if _has(text, "mismatch", "different amount", "wrong amount"):
        topics.add("payment_mismatch")
    if _has(text, "duplicate", "double charge", "charged twice"):
        topics.add("duplicate_charge")
    if _has(text, "refund pending", "pending refund"):
        topics.add("refund_pending")
    if _has(text, "refund failed", "failed refund"):
        topics.add("refund_failed")
    if _has(text, "split payment", "installment", "parcel"):
        topics.add("valid_split_payment")
    return topics


def _has(text: str, *terms: str) -> bool:
    return any(term in text for term in terms)


def _evidence_text(data: list[Any]) -> str:
    parts: list[str] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                parts.append(str(key).replace("_", " ").lower())
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)
        elif value is not None:
            parts.append(str(value).replace("_", " ").lower())

    visit(data)
    return " ".join(parts)


def _has_refund_signal(data: list[Any]) -> bool:
    text = _evidence_text(data)
    return _has(text, "eligible", "approved", "requested", "full refund")


def _claim_refs(topic: Any, refs: list[str], domains: dict[str, str]) -> list[str]:
    required = {"order"}
    if topic in {"canceled_order_paid", "unavailable_order_paid", "valid_split_payment", "payment_mismatch", "duplicate_charge", "refund_pending", "refund_failed", "requested_full_refund"}:
        required.add("payment")
    if topic in {"late_delivery_seller", "late_delivery_logistics"}:
        required.add("shipment")
    return [ref for ref in refs if domains.get(ref) in required]


def _root_cause(issue: str, data: list[Any]) -> dict[str, Any]:
    party_map = {
        "late_delivery_seller": "seller", "late_delivery_logistics": "logistics_provider",
        "payment_mismatch": "payment_provider", "duplicate_charge": "payment_provider",
        "canceled_order_paid": "platform", "unavailable_order_paid": "platform",
    }
    party = party_map.get(issue)
    if not party:
        return {"ranked_causes": [], "responsible_parties": []}
    party_id = _first_value(data, "seller_id") if party == "seller" else None
    return {"ranked_causes": [{"cause_code": issue.upper(), "rank": 1}], "responsible_parties": [{"party_type": party, "party_id": party_id}]}


def _financial_resolution(issue: str, case: dict[str, Any], data: list[Any]) -> dict[str, Any]:
    amount = _first_number(data, "recommended_refund_brl", "refund_amount_brl", "refund_amount", "amount_brl")
    refundable = {"canceled_order_paid", "unavailable_order_paid", "refund_pending", "refund_failed"}
    if amount is None or issue not in refundable:
        return {"currency": "BRL", "recommended_refund_brl": 0, "refund_lines": []}
    order_id = case.get("customer_request", {}).get("claimed_order_id")
    return {"currency": "BRL", "recommended_refund_brl": amount, "refund_lines": [{"reason_code": issue, "amount_brl": amount, "entity_id": order_id}]}


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
    values = {"order_ids": [], "item_ids": [], "seller_ids": [], "payment_references": [], "shipment_ids": []}
    claimed = case.get("customer_request", {}).get("claimed_order_id")
    if isinstance(claimed, str):
        values["order_ids"].append(claimed)
    mapping = {"order_id": "order_ids", "item_id": "item_ids", "seller_id": "seller_ids", "payment_id": "payment_references", "payment_reference": "payment_references", "shipment_id": "shipment_ids"}

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            for key, nested in value.items():
                if key in mapping and isinstance(nested, str):
                    values[mapping[key]].append(nested)
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(data)
    return {key: list(dict.fromkeys(items))[:20] for key, items in values.items()}


def _required_string(value: dict[str, Any], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"case is missing {key}")
    return item
