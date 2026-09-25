# L3A Architecture Record

Team phải cập nhật tài liệu này cùng source. Mục tiêu là mô tả quyết định có thể kiểm chứng, không ghi prompt bí mật hoặc chain-of-thought.

## 1. System overview

Vẽ hoặc mô tả luồng từ `inputs/<case_id>.json` đến MCP calls, specialist agents, verifier, output và trace.

```text
inputs/<case_id>.json -> Coordinator -> Specialists -> Verifier
                              |             |             |
                              +-------- MCP +-------- Trace/output
```

`workflow.py` dùng state machine async thuần Python, không dùng LLM. CLI mở một MCP
session cho toàn bộ run 100 case và xóa `outputs/*.json`, `traces/trace.jsonl` cũ
trước khi chạy. Mỗi case là một scope độc lập; `case_id` được truyền nguyên vẹn vào
mọi MCP call và trace event. Khi thiếu tool hoặc evidence, workflow trả về
`insufficient_evidence`, không đoán dữ liệu.

## 2. Agent ownership

| Actor | Input | Trách nhiệm | Output/handoff |
| --- | --- | --- | --- |
| Coordinator | case JSON, discovered tool names | mở case, phân công theo domain, giữ scope `case_id` | task assignments và case lifecycle |
| Order/item | order id từ request | chọn tool order đã discovery, xác minh identifiers | order evidence -> payment-agent |
| Payment | order id, order evidence | chỉ truy vấn payment tool | payment evidence -> shipment-agent |
| Shipment | order id, order evidence | chỉ truy vấn shipment tool, xác minh timeline | shipment evidence -> policy-agent |
| Policy | `policy_version` | chỉ truy vấn policy tool | policy evidence -> verifier |
| Verifier | specialist evidence, claims | liên kết claim/evidence, kiểm tra scope, tạo output | output và verification trace |

Quyền tool theo domain được thực thi bằng `_select_tool`: agent chỉ chọn tool đầu
tiên theo thứ tự alphabet có tên chứa domain sau khi chuẩn hóa chữ thường/ký tự đặc
biệt. Không có tool phù hợp thì không gọi và không tạo evidence giả. Các specialist
đang chạy tuần tự, không có concurrency.

Coordinator gọi `list_tools`; gateway cache danh sách tool trong một run. Order agent
luôn chạy, payment/shipment agent chỉ chạy khi claim cần domain tương ứng, policy agent
chạy để áp dụng policy. Mỗi agent gọi tối đa một tool thuộc domain; gateway là
boundary duy nhất gọi MCP và validate evidence.

## 3. A2A protocol

State nội bộ tương đương `{case_id, sender, recipient, domain, evidence_refs, status}`;
không có message broker riêng. Handoff đi một chiều order -> payment -> shipment ->
policy -> verifier; không handoff ngược nên không có vòng lặp. Trace lifecycle đầy đủ
do hai lớp đảm nhiệm: `cli.py` emit `case_received`/`case_finalized`, còn workflow
emit `task_assigned`, `tool_result_consumed`, `handoff`, `policy_decided` và
`verification_completed`. Trace chỉ ghi event, actor, tool, ref và decision code.

## 4. Evidence lifecycle

`EvidenceGateway.call` truyền `case_id` vào request và validate envelope bằng
`mcp-evidence-response-v1.schema.json`. Workflow lưu nguyên `evidence_ref` và emit
`tool_result_consumed` cho từng response hợp lệ. Output hiện đưa các ref đã nhận vào
top-level `evidence_refs` và claim assessments; scorer vẫn chịu trách nhiệm kiểm tra
provenance/cross-scope. Evidence không được chia sẻ giữa các case.

## 5. Failure policy

| Failure | Retry? | Fallback | Trace event/code |
| --- | --- | --- | --- |
| MCP tool/runtime error | Không retry | bỏ specialist, tiếp tục với evidence còn lại | `verification_completed` |
| Missing tool/evidence | Không retry | `insufficient_evidence`, không suy đoán | `policy_decided`/`verification_completed` |
| Network/TLS `ConnectError` | Chưa có retry/reconnect; lỗi có thể dừng run | chạy lại toàn bộ run sau khi endpoint ổn định | process error, có thể không có finalize |
| Invalid MCP envelope | Không retry | `EvidenceGateway` ném lỗi validation; specialist bắt `ValueError` | không consume ref |
| Source conflict | Chưa có resolver tự động | `data_conflicts` hiện để rỗng | `verification_completed` |

Retry/reconnect nếu bổ sung phải tối đa 2 lần, có backoff và chỉ dùng cho call
idempotent; hiện tại chưa được triển khai.

## 6. Verification invariants

CLI validate output theo `l3a-output-v2.schema.json` và kiểm tra `case_id` khớp.
Workflow verifier lọc top-level refs theo các response đã nhận, ép confidence vào
`[0, 1]`, và khi issue là `insufficient_evidence` thì đặt status phù hợp, xóa
responsible parties/refund. Các kiểm tra sâu như claim linkage, tổng refund,
cross-scope ownership và consistency cuối cùng do public/private scorer thực hiện;
workflow hiện chưa tự giải quyết source conflict.

## 7. Reproducibility

Workflow không dùng model hoặc random seed; dependency nằm trong `pyproject.toml`.
Mỗi run gọi `list_tools` một lần qua cache; mỗi case gọi tối đa một tool cho từng
domain cần thiết, chạy tuần tự trong cùng MCP session. Lệnh chạy là `day09 run`, kiểm tra là
`day09 validate`, đóng gói là `day09 package --output dist/submission.zip`. Không ghi
API key, prompt riêng hoặc chain-of-thought vào artifact. Input competition chỉ là
runtime payload và không được commit theo `tests/test_release_safety.py`.
