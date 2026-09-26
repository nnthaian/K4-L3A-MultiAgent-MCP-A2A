# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Luồng xử lý từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace:

```text
inputs/<case_id>.json -> Coordinator -> [Order Agent, Payment Agent, Shipment Agent, Policy Agent] -> Verifier -> Output
                              |                    |                    |                    |                    |
                              +-------- MCP Gateway (EvidenceGateway) +-------- Trace (TraceWriter)
```

`workflow.py` dùng async/await thuần Python, không dùng LLM. CLI (`cli.py`) mở một MCP session cho toàn bộ run 100 case và xóa `outputs/*.json`, `traces/trace.jsonl` cũ trước khi chạy. Mỗi case là một scope độc lập; `case_id` được truyền nguyên vẹn vào mọi MCP call và trace event. Khi thiếu tool hoặc evidence, workflow trả về `unsupported_claim` (không phải `insufficient_evidence`), không đoán dữ liệu.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case JSON, discovered tool names | mở case, phân công task theo domain, giữ scope `case_id` | task assignments và case lifecycle |
| Order Agent | order_id từ customer_request | gọi `get_order`, `get_order_items`, `get_sellers` | OrderFacts -> Payment Agent, Shipment Agent |
| Payment Agent | order_id, OrderFacts | gọi `get_order_payments`, `get_payment_timeline`, `get_refund_timeline` | PaymentFacts -> Verifier |
| Shipment Agent | order_id, OrderFacts | gọi `get_shipment_summary` | ShipmentFacts -> Verifier |
| Policy Agent | policy_version từ case | gọi `get_policy` | Policy data -> Verifier |
| Verifier | specialist evidence, claims, policy | liên kết claim/evidence, kiểm tra scope, tạo output | output và verification trace |

Quyền tool theo domain được thực thi bằng `TOOL_OWNERS` dict: mỗi agent chỉ được gọi tool thuộc domain của mình. Không có tool phù hợp thì không gọi và không tạo evidence giả. Các specialist chạy **song song** (concurrency) qua `asyncio.gather` cho payment, shipment, policy agents.

Coordinator gọi `list_tools` một lần qua gateway cache. Order agent luôn chạy trước. Payment/shipment/policy agents chạy song song sau khi order agent hoàn thành. Mỗi agent gọi tối đa một tool cho mỗi domain cần thiết; gateway là boundary duy nhất gọi MCP và validate evidence.

## 3. A2A protocol

State nội bộ là class `Message` với fields: `{case_id, sender, recipient, intent, payload}`. Không có message broker riêng. Handoff đi một chiều: Coordinator -> Specialists -> Verifier -> Coordinator; không handoff ngược nên không có vòng lặp. Trace lifecycle đầy đủ do hai lớp đảm nhiệm:
- `cli.py` emit `case_received`/`case_finalized`
- `workflow.py` emit `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided` và `verification_completed`

Trace ghi event, actor, target, tool, evidence_refs, decision_code và attributes.

## 4. Evidence lifecycle

`EvidenceGateway.call` truyền `case_id` vào request và validate envelope bằng `mcp-evidence-response-v1.schema.json`. Workflow lưu nguyên `evidence_ref` trong `CaseContext.evidence` và emit `tool_result_consumed` cho từng response hợp lệ. Output đưa các ref đã nhận vào top-level `evidence_refs` và claim assessments; scorer chịu trách nhiệm kiểm tra provenance/cross-scope. Evidence không được chia sẻ giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP tool/runtime error (`RuntimeError`) | Không retry | ném lỗi, dừng case | - |
| Missing optional tool (`get_refund_timeline`) | Không retry | trả về `None`, tiếp tục | không emit `tool_result_consumed` |
| Network/TLS (`TimeoutError`, `OSError`) | Có, tối đa 2 lần (`MAX_ATTEMPTS=2`), backoff 1s | ném lỗi sau khi hết retry | - |
| Invalid MCP envelope | Không retry | `EvidenceGateway` ném lỗi validation | không consume ref |
| Source conflict | Không resolver tự động | `data_conflicts` để rỗng `[]` | `verification_completed` |

Retry chỉ áp dụng cho transport errors (idempotent calls). Tool-level errors (`RuntimeError`) không retry.

## 6. Verification invariants

CLI validate output theo `l3a-output-v2.schema.json` và kiểm tra `case_id` khớp. Workflow verifier (`verify()`) kiểm tra:
- `EVIDENCE_NOT_OWNED`: mọi `evidence_refs` trong output phải thuộc evidence đã fetch
- `MISSING_ORDER_EVIDENCE`: issue không phải payment-only phải có `get_order` ref
- `MISSING_POLICY_EVIDENCE`: phải có `get_policy` ref
- `REFUND_TOTAL_MISMATCH`: tổng `refund_lines` phải khớp `recommended_refund_brl`
- `NO_ACTION_WITH_REFUND`: status `no_action` không được có refund > 0
- `POLICY_MISMATCH`: case_status và resolution_actions phải khớp policy rule
- `SELLER_NOT_IN_SCOPE`: responsible_parties seller phải nằm trong affected_entities.seller_ids
- `ENTITY_SCOPE`: affected_entities.order_ids phải là `[order_id]`

Khi issue là `unsupported_claim`: confidence = 0.98, status = `no_action`, refund = 0, responsible_parties = [].

## 7. Reproducibility

Workflow không dùng model hoặc random seed; dependency nằm trong `pyproject.toml`. Mỗi run gọi `list_tools` một lần qua cache; mỗi case gọi tối đa 1 tool cho mỗi domain cần thiết (order: 3 tools, payment: 3 tools, shipment: 1 tool, policy: 1 tool), chạy song song cho payment/shipment/policy. Lệnh chạy là `day09 run`, kiểm tra là `day09 validate`, đóng gói là `day09 package --output dist/submission.zip`. Không ghi API key, prompt riêng hoặc chain-of-thought vào artifact. Input competition chỉ là runtime payload và không được commit theo `tests/test_release_safety.py`.

## 8. Decision Rules (rules.py)

Pure functions over MCP evidence payloads (no I/O). Logic quyết định:
1. `order_facts`: lọc items theo shipping_limit_date trong window [purchase, opened], dedupe, lấy earliest
2. `payment_facts`: lọc captures trong window 2h sau approved, mismatches trong 3h sau last_capture, refunds trong [purchase, opened]
3. `shipment_facts`: late = delivered > estimated; seller_late = late và carrier > shipping_limit_date
4. `decide`: ưu tiên theo `ISSUE_PRIORITY` tuple, confidence 0.98 (single issue) hoặc 0.95 (multiple), giảm 0.8 nếu late delivery actor mismatch

## 9. Output Assembly (build_output)

- `primary_issue`: từ `decision.issue`
- `case_status`, `recommended_action`, `refund_brl`, `responsible_parties`: từ policy rule
- `evidence_refs`: dedupe từ base tools + `ISSUE_EVIDENCE[issue]` + `get_policy`
- `payment_references`: cho payment issues, format `{order_id}:{payment_sequential}`
- `claim_assessments`: map topic -> verdict (supported/partially_supported/unsupported/insufficient_evidence)
- `root_cause_analysis`: ranked_causes + responsible_parties (seller_id từ order.seller_ids[0])
- `financial_resolution`: currency BRL, refund lines
- `data_conflicts`: luôn `[]`

## 10. Contracts & Schemas

- `contracts/schemas/l3a-output-v2.schema.json`: output schema
- `contracts/schemas/trace-event-v1.schema.json`: trace event schema
- `contracts/schemas/mcp-evidence-response-v1.schema.json`: MCP evidence envelope
- `contracts/schemas/submission-manifest-v2.schema.json`: submission manifest
- `contracts/registry/variants.json`: variant registry
- `contracts/scoring/scoring-policy-v2.json`: scoring weights và components
