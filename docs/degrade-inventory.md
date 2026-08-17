# P0/T0.5 降级 occurrence inventory

- 审计日期：2026-08-16。2026-08-17 随 `review-findings-2026-08-17.md` 的 R1/R2 修复更新：新增 `HUB_DEGRADE_DUPLICATE_TERMINAL_SKIPPED` 两处 occurrence，且**全表行号按当时工作树重算**（协议层插入常量与 `_repeats_terminal` 使 `:4292` 之后偏移 +12、`feed` 之后偏移 +43）。同日随 R3–R6 修复二次更新：occurrence／distinct 数不变，`sanitize_error_text` 补四类凭证形状使 `:3675` 之后再偏移 +43，**全表行号按当时工作树重算**。同日第三次更新：R5 的后续修复（宽容闭合引号 + header 值形状判定）使 `:3683` 之后偏移 +32，occurrence／distinct 数仍不变，表内 18 行行号与 §2、§3 的两处行号引用一并重算。行号会继续漂移，定位以符号名为准。
- 审计命令：`grep -c 'HUB_DEGRADE_' claude1_protocol.py`、`rg -n 'HUB_DEGRADE_' claude1_protocol.py`、`rg -o 'HUB_DEGRADE_[A-Z0-9_]+' claude1_protocol.py | sort -u | wc -l`。
- 范围：只盘点 `claude1_protocol.py` 中的每一个 `HUB_DEGRADE_` 文本 occurrence，不按 code 合并；请求 plan、非流 response plan、转译 bridge 运行时 warning 和 native request plan 均纳入。注释 occurrence 也计数，但不算生产点。
- P0 口径：只有 lossy 请求已经发送，或 lossy 响应/事件已经交付下游，才算本回合 degradation。成功回合进 usage，失败回合进 errors；strict 在生成 lossy 输出前拒绝，不算成功 degradation。`/count_tokens` 是预检探针而非 message turn，三条 count 路径（native 转发 / 本地估算 / 上游 404-405-501 回退估算）**一律不写 usage 行**，因此不以其作为落盘证据。
- 载体缩写：`T` = translated request/response path，`N` = native request path，`B` = `AnthropicStreamBridge.warning_codes`，`P` = `ConversionPlan.warning_codes`。Hub 的通用出口是 `_handle_transformed_messages` 的 `record_usage`/`record_error`，或 `_forward_to_channel` 的对应调用。

## 1. occurrence 表

| code | `claude1_protocol.py:<line>` + 符号 | 触发条件 | 冒泡载体（request plan / response plan / bridge） | Hub 最终出口 | 状态 | 测试或代码路径证据 |
|---|---|---|---|---|---|---|
| `HUB_DEGRADE_SCHEMA_NORMALIZED` | `claude1_protocol.py:359` `_clean_schema` | tool schema 递归出现 `format: "uri"`，清理后继续。 | `P → T`；count_tokens 的校验 plan 被丢弃。 | translated message turn → `record_usage`；失败 → `record_error`。 | 已落盘 usage / errors；strict reject；count_tokens 本地估算为 P0 范围外。 | `_clean_schema` → `ConversionPlan` → `_handle_transformed_messages`；协议 capability contract 覆盖该请求能力。 |
| `HUB_DEGRADE_TOOL_RESULT_CONTENT_ENVELOPED` | `claude1_protocol.py:459` `_tool_result_output` | 已校验的 tool result content 是非字符串，通常是内容块数组，改为可见 envelope。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `_tool_result_output` 与 `RequestCapabilityContractTests.test_nested_tool_result_and_is_error_use_a_lossless_visible_envelope`；通用 carrier journal。 |
| `HUB_DEGRADE_TOOL_RESULT_ERROR_ENVELOPED` | `claude1_protocol.py:469` `_tool_result_output` | `tool_result.is_error == true`，错误状态被可见 envelope 表达。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | 同一请求 capability 测试；`P.warning_codes` 和 T journal 代码路径。 |
| `HUB_DEGRADE_CITATION_METADATA_DROPPED` | `claude1_protocol.py:501` `_record_content_metadata` | 请求内容块带 `citations`，文本保留而引用元数据没有等价载体。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_document_search_result_and_citations_degrade_to_provenance_text`；`_record_content_metadata` → P → journal。 |
| `HUB_DEGRADE_CONTENT_METADATA_DROPPED` | `claude1_protocol.py:511` `_record_content_metadata` | 内容块带 `cache_control`，目标请求没有等价载体。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_nested_document_and_search_text_metadata_is_observably_classified`；通用 T carrier。 |
| `HUB_DEGRADE_EMPTY_THINKING_SIGNATURE_IGNORED` | `claude1_protocol.py:531` `_record_thinking_degradation` | thinking block 有空字符串 signature，忽略空签名但保留 thinking。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_empty_thinking_signature_placeholder_is_ignored_but_non_string_rejects`；P warning path。 |
| `HUB_DEGRADE_THINKING_SIGNATURE_DROPPED` | `claude1_protocol.py:541` `_record_thinking_degradation` | thinking block 有非空 Anthropic signature，但目标不能安全转发。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_thinking_text_degrades_but_anthropic_signature_is_never_fabricated_or_forwarded`；P → T journal。 |
| `HUB_DEGRADE_THINKING_TO_REASONING` | `claude1_protocol.py:550` `_record_thinking_degradation` | thinking block 含字符串 thinking，转为目标 reasoning 表达。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | 同上；`ConversionPlan.warning_codes` 到通用成功/失败出口。 |
| `HUB_DEGRADE_DOCUMENT_CONTEXT_TEXTIFIED` | `claude1_protocol.py:587` `_document_text` | document context 为非空字符串，转成可见文本。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_responses_stop_sequences_and_document_context_are_observable`；P/T 代码路径。 |
| `HUB_DEGRADE_DOCUMENT_TEXT_EXTRACTED` | `claude1_protocol.py:630` `_document_text` | document source 可提取文本，例如 text/plain、文本块或文本型 base64。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_document_search_result_and_citations_degrade_to_provenance_text`；P/T 代码路径。 |
| `HUB_DEGRADE_DOCUMENT_PLACEHOLDER` | `claude1_protocol.py:649` `_document_text` | 合法 document base64/url 无法提取文本，生成占位文本。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | 同上；占位输出仍经 P → T 通用 journal。 |
| `HUB_DEGRADE_SEARCH_RESULT_TEXTIFIED` | `claude1_protocol.py:707` `_search_result_text` | search result content 是字符串或合法文本块数组，转为文本。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_document_search_result_and_citations_degrade_to_provenance_text`；P/T 代码路径。 |
| `HUB_DEGRADE_THINKING_TO_EFFORT` | `claude1_protocol.py:1096` `_anthropic_effort` | legacy `thinking.effort` 合法但目标仅有 reasoning effort。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_chat_request_maps_xhigh_thinking_to_reasoning_effort`；请求 plan → T。 |
| `HUB_DEGRADE_THINKING_BUDGET_TO_EFFORT` | `claude1_protocol.py:1119` `_anthropic_effort` | 合法 thinking `budget_tokens` 存在且没有 effort，映射为 reasoning effort。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_tool_strict_parallel_choice_and_thinking_budget_are_explicit`；P/T 代码路径。 |
| `HUB_DEGRADE_ADAPTIVE_THINKING_TO_EFFORT` | `claude1_protocol.py:1121` `_anthropic_effort` | thinking type 为 adaptive，且没有 budget/effort，映射为默认 effort。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `_anthropic_effort` 分支 + P → T 通用 journal；无专属 Hub E2E 测试。 |
| `HUB_DEGRADE_METADATA_DROPPED` | `claude1_protocol.py:1163` `_apply_cross_request_controls` | openai_chat 请求 metadata 无目标等价载体；Responses 可精确复制，不走该 occurrence。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_request_controls_are_mapped_or_visibly_degraded`；P/T 代码路径。 |
| `HUB_DEGRADE_STOP_SEQUENCES_DROPPED` | `claude1_protocol.py:1534` `anthropic_to_responses` | Responses 目标存在 stop_sequences，目标没有等价字段。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_responses_stop_sequences_and_document_context_are_observable`；P/T 代码路径。 |
| `HUB_DEGRADE_CONTEXT_MANAGEMENT_DROPPED` | `claude1_protocol.py:1703` `_DROPPED_REQUEST_FIELDS` | 顶层存在 context_management，被目标适配器丢弃。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_unknown_top_level_request_fields_degrade_by_default_and_strict_rejects`；P/T 代码路径。 |
| `HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED` | `claude1_protocol.py:2121` `_parse_request_ir` | 顶层字段不在已处理、拒绝或显式丢弃集合中，字段被丢弃。 | `P → T`；count_tokens plan 不形成 turn。 | T 成功 → usage；T HTTP/transform/connect/stream 失败 → errors。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_nonstream_transformed_turn_persists_request_and_response_degrades`、`test_transformed_response_failure_persists_request_degrade`、`test_transformed_connect_failure_persists_request_degrade`。 |
| `HUB_DEGRADE_CACHE_CONTROL_DROPPED` | `claude1_protocol.py:2186` `_parse_request_ir` | 顶层存在 cache_control，目标没有等价请求载体。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_request_controls_are_mapped_or_visibly_degraded`；P/T 通用出口。 |
| `HUB_DEGRADE_TOP_K_DROPPED` | `claude1_protocol.py:2189` `_parse_request_ir` | 顶层 top_k 为正整数，目标没有等价字段。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_request_controls_are_mapped_or_visibly_degraded`；P/T 通用出口。 |
| `HUB_DEGRADE_SYSTEM_METADATA_DROPPED` | `claude1_protocol.py:2217` `_parse_request_ir` | system text block 含 type/text 之外的字段，字段被丢弃。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_system_block_metadata_is_preserved_in_ir_and_visibly_degraded`、`test_transformed_upstream_error_carries_request_protocol_warnings`。 |
| `HUB_DEGRADE_DEFERRED_TOOL_EAGERLY_LOADED` | `claude1_protocol.py:2404` `_parse_request_ir` | client tool 的 defer_loading 为 true，改为 eager loading。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_deferred_tools_are_eagerly_loaded_and_native_search_control_is_omitted`；P/T 通用出口。 |
| `HUB_DEGRADE_TOOL_METADATA_DROPPED` | `claude1_protocol.py:2421` `_parse_request_ir` | client tool 含 cache_control 或 input_examples，目标没有等价载体。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `_parse_request_ir` 分支 + P/T 通用 journal；无专属 Hub E2E 测试。 |
| `HUB_DEGRADE_BATCH_TOOL_OMITTED` | `claude1_protocol.py:2435` `_parse_request_ir` | tool type 为 BatchTool，目标没有可安全表达的等价 tool。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `_parse_request_ir` 分支 + P/T 通用 journal；无专属 Hub E2E 测试。 |
| `HUB_DEGRADE_TOOL_SEARCH_OMITTED` | `claude1_protocol.py:2450` `_parse_request_ir` | tool type 以 tool_search 开头，搜索控制被省略。 | `P → T`；count_tokens plan 不形成 turn。 | T 的成功/失败 journal。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_deferred_tools_are_eagerly_loaded_and_native_search_control_is_omitted`；P/T 通用出口。 |
| `HUB_DEGRADE_SYSTEM_ROLE_PROMOTED` | `claude1_protocol.py:2529` `_normalize_native_system_roles` | native 请求 messages 中出现 system role，提升为目标可接受的消息形式。 | `P → N`；native count_tokens validation/estimate 不形成 P0 turn。 | N 成功 → usage；N HTTP/stream/connect 失败 → errors。 | 已落盘 usage / errors；strict reject；count_tokens P0 范围外。 | `test_native_nonstream_success_persists_request_degrade_without_rewriting_response`、`test_native_stream_success_persists_request_degrade_and_forwards_real_terminal`、`test_native_connect_failure_persists_request_degrade`。 |
| `HUB_DEGRADE_CITATION_METADATA_DROPPED` | `claude1_protocol.py:2804` `_record_response_citation_degradation` | Chat/Responses 非流响应 content part 含 annotations/citations，文本保留、引用丢弃。 | `response plan P → T`；合成流沿同一 plan。 | response plan 成功 → usage；若转换失败，未交付的 response warning 不冒充 journal，已有 request warnings 进 errors。 | 已落盘 usage（成功交付）；errors 只记录实际失败证据和已发送 request warnings；非流 response adapter 不启用 strict gate。 | `test_real_upstream_citations_are_not_fabricated_into_anthropic_locations`；`prepare_response` → record_usage。 |
| `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED` | `claude1_protocol.py:2817` `_record_response_metadata_degradation` | 非流 response 的未知但可安全忽略的元数据、有效 id、usage 扩展字段等没有 Anthropic 载体。 | `response plan P → T`；合成流沿同一 plan。 | 成功 → usage；response transform failure → response error journal，未交付 response warning 不计为已发生。 | 已落盘 usage（成功交付）/ errors（失败）；非流 response adapter 不启用 strict gate。 | `test_nonstream_transformed_turn_persists_request_and_response_degrades`、`test_synthesized_sse_persists_request_and_response_degrades`；response plan 通用路径。 |
| `HUB_DEGRADE_UNSIGNED_THINKING` | `claude1_protocol.py:3054` `chat_to_anthropic` | Chat 非流 message 的 reasoning_content 被转成 thinking，但没有可验证 signature。 | `response plan P → T`；合成流沿同一 plan。 | 成功 response/合成流 → usage；失败 → errors。 | 已落盘 usage / errors；非流 response adapter 不启用 strict gate。 | `test_chat_reasoning_and_content_bearing_fields_are_observable`；非流 occurrence 由 response plan 通用出口承载。 |
| `HUB_DEGRADE_UNSIGNED_THINKING` | `claude1_protocol.py:3563` `responses_to_anthropic` | Responses 非流 reasoning summary 有 summary_text，转成无 signature 的 thinking。 | `response plan P → T`；合成流沿同一 plan。 | 成功 → usage；失败 → errors。 | 已落盘 usage / errors；非流 response adapter 不启用 strict gate。 | `responses_to_anthropic` response plan → `record_usage`；无该 occurrence 专属 Hub E2E 测试。 |
| `HUB_DEGRADE_STREAM_SEQUENCE_METADATA_DROPPED` | `claude1_protocol.py:4406` `AnthropicStreamBridge._validate_responses_event_envelope` | Responses SSE 已知事件有合法 sequence_number，但 Anthropic 没有对应 carrier。 | `B.observations → B.warning_codes → T`。 | 真流成功 → usage；断流/转换失败/error terminal → errors。 | 已落盘 usage / errors；strict reject。 | `test_responses_event_wrappers_reject_unknown_fields_and_track_metadata_loss`；bridge warning → 通用 stream journal。 |
| `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED` | `claude1_protocol.py:4411` `AnthropicStreamBridge._validate_responses_event_envelope` | Responses SSE 事件含 logprobs 或 obfuscation，无 Anthropic carrier。 | `B.observations → B.warning_codes → T`。 | T stream 的 usage/errors journal。 | 已落盘 usage / errors；strict reject。 | envelope validator + `_handle_transformed_messages` 通用 stream carrier；无专属 Hub E2E。 |
| `HUB_DEGRADE_LATE_INPUT_USAGE` | `claude1_protocol.py:4438` `AnthropicStreamBridge._update_base_usage` | stream 已 started 后首次收到 input usage，改放 message_delta 并记观测。 | `B.observations → B.warning_codes → T`。 | 真流成功 → usage；失败或 error terminal → errors。 | 已落盘 usage / errors；strict 不拒绝该 accounting placement。 | `test_late_input_usage_is_carried_by_message_delta_observably`；stream runtime warning 通用出口。 |
| `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED` | `claude1_protocol.py:4484` `AnthropicStreamBridge._consume_stream_usage` | usage 有可丢弃的未知/扩展字段，计数仍可用。 | `B.observations → B.warning_codes → T`。 | T stream 的 usage/errors journal。 | 已落盘 usage / errors；strict reject。 | `test_stream_usage_registries_drop_unknown_fields_with_warning`、`test_extra_usage_field_does_not_abort_finished_stream`。 |
| `HUB_DEGRADE_UNSIGNED_THINKING` | `claude1_protocol.py:4610` `AnthropicStreamBridge._close` | thinking block 关闭时上游没有真实 signature。 | `B.observations → B.warning_codes → T`。 | 成功 → usage；断流、finish 失败或 error terminal → errors。 | 已落盘 usage / errors；strict reject。 | `test_thinking_signature_is_emitted_only_when_upstream_supplies_it`、`test_transformed_stream_persists_request_and_runtime_degrades`、`test_transformed_stream_failure_persists_observed_degrades`。 |
| `HUB_DEGRADE_CITATION_METADATA_DROPPED` | `claude1_protocol.py:5222` `AnthropicStreamBridge._response_content_part` | Responses structural content part 的 annotations/citations 非空。 | `B.observations → B.warning_codes → T`。 | T stream usage/errors journal。 | 已落盘 usage / errors；该 metadata-only 分支不启用 strict gate。 | `_response_content_part` → B → generic stream journal；无专属 Hub E2E。 |
| `HUB_DEGRADE_CITATION_METADATA_DROPPED` | `claude1_protocol.py:5615` `AnthropicStreamBridge._response_message_item_snapshot` | Responses terminal message snapshot 的 output text part 含 annotations/citations。 | `B.observations → B.warning_codes → T`。 | T stream usage/errors journal。 | 已落盘 usage / errors；该 metadata-only 分支不启用 strict gate。 | `_response_message_item_snapshot` → B → generic stream journal；非流 citation 有 `test_real_upstream_citations_are_not_fabricated_into_anthropic_locations`。 |
| `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED` | `claude1_protocol.py:5774` `AnthropicStreamBridge._validate_response_snapshot_fields` | Responses 合法 response snapshot 含未映射到 Anthropic 的允许字段。 | `B.observations → B.warning_codes → T`。 | T stream usage/errors journal。 | 已落盘 usage / errors；strict reject。 | snapshot validator → B；错误终态由 `test_translated_error_event_is_journaled_as_failure_not_usage` 锁住不进 usage。 |
| `HUB_DEGRADE_SYNTHETIC_TOOL_ID` | `claude1_protocol.py:5906` `AnthropicStreamBridge._observe_synthetic_response_tool_id` | Responses function call 同时缺 upstream id 与 call_id，生成安全的本地标识。 | `B.observations → B.warning_codes → T`。 | T stream usage/errors journal。 | 已落盘 usage / errors；strict reject。 | `test_responses_zero_output_index_is_a_tool_identifier`、`test_responses_terminal_only_output_index_tool_id_is_observable`；B 通用出口。 |
| `HUB_DEGRADE_DUPLICATE_TERMINAL_SKIPPED` | `claude1_protocol.py:6223` `AnthropicStreamBridge.feed` | 流已关闭后上游重放终态：尾随 `[DONE]`、再来一个 bare error 帧、或重复的 finish_reason。重放不丢语义也不破因果，跳过而非拒绝；终态之后的**内容**帧仍抽 `HUB_SSE_LATE_EVENT`。 | `B.observations → B.warning_codes → T`。 | 成功流 → usage（回合仍到达记账出口）；error terminal 流 → errors 并保留上游真因。 | 已落盘 usage / errors；strict reject。 | `test_trailing_done_keeps_the_success_turn_and_its_usage`、`test_trailing_done_after_error_frame_keeps_the_upstream_reason`。 |
| `HUB_DEGRADE_DUPLICATE_TERMINAL_SKIPPED` | `claude1_protocol.py:6278` `AnthropicStreamBridge.feed` | Responses 在 terminal 之后重放终态类事件（`response.completed` / `response.incomplete` / `response.failed` / `error`）；同位置的非终态事件仍抽 `HUB_SSE_LATE_EVENT`。 | `B.observations → B.warning_codes → T`。 | T stream 的 usage/errors journal。 | 已落盘 usage / errors；strict reject。 | `test_terminal_late_duplicate_and_usage_regression_contract`（同一测试仍钉住终态后内容帧 fail-closed）。 |
| `HUB_DEGRADE_UPSTREAM_ERROR_DETAIL_DROPPED` | `claude1_protocol.py:6309` `AnthropicStreamBridge._feed_chat` | Chat bare error event 可转为下游 error，但安全 code/message 无法完整提取。 | `B.observations`，并设置 error-terminal code/message（仅安全 evidence，不含 payload）。 | bridge.finish() 后 `record_error(phase="stream")`；不写 usage；wire 保持 event:error。 | 已落盘 errors；strict reject；绝不写 usage。 | `test_translated_error_event_is_journaled_as_failure_not_usage`；protocol error-terminal interface tests。 |
| `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED` | `claude1_protocol.py:6402` `AnthropicStreamBridge._feed_chat` | Chat stream wrapper 有 object/created/service_tier/system_fingerprint 等无 carrier 字段。 | `B.observations → B.warning_codes → T`。 | T stream usage/errors journal。 | 已落盘 usage / errors；strict reject。 | `test_chat_stream_wrapper_fields_are_rejected_or_observably_degraded`；B 通用出口。 |
| `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED` | `claude1_protocol.py:6464` `AnthropicStreamBridge._feed_chat` | Chat choice 含 logprobs，无 Anthropic carrier。 | `B.observations → B.warning_codes → T`。 | T stream usage/errors journal。 | 已落盘 usage / errors；strict reject。 | `test_chat_stream_wrapper_fields_are_rejected_or_observably_degraded`；B 通用出口。 |
| `HUB_DEGRADE_CITATION_METADATA_DROPPED` | `claude1_protocol.py:6541` `AnthropicStreamBridge._feed_chat` | Chat delta 含 annotations/citations 且已有开放 text block，元数据丢弃但文本交付。 | `B.observations → B.warning_codes → T`。 | T stream usage/errors journal。 | 已落盘 usage / errors；该 metadata-only 分支不启用 strict gate。 | `_feed_chat` citation branch + B → generic stream journal；无专属 Hub E2E。 |
| `HUB_DEGRADE_CITATION_METADATA_DROPPED` | `claude1_protocol.py:6710` `AnthropicStreamBridge._feed_responses` | Responses citation/annotation SSE 事件顺序合法，但没有 Anthropic 等价载体。 | `B.observations → B.warning_codes → T`。 | T stream usage/errors journal。 | 已落盘 usage / errors；strict reject。 | `test_stream_citation_metadata_requires_open_text_and_is_observable`；B 通用出口。 |
| `HUB_DEGRADE_UPSTREAM_ERROR_DETAIL_DROPPED` | `claude1_protocol.py:6903` `AnthropicStreamBridge._feed_responses` | Responses response.failed/error 有 detail，但无安全 code/message 可保留。 | `B.observations`，并设置 error-terminal code/message（仅安全 evidence）。 | bridge.finish() 后 `record_error(phase="stream")`；不写 usage；wire 保持 event:error。 | 已落盘 errors；strict reject；绝不写 usage。 | `test_translated_error_event_is_journaled_as_failure_not_usage`、`test_translated_error_event_preserves_safe_error_evidence_without_usage`；Responses bridge error branch。 |
| `HUB_DEGRADE_LATE_INPUT_USAGE` | `claude1_protocol.py:6984` `AnthropicStreamBridge.finish` 注释 | 仅说明 late input usage 的可观测性，不执行。 | 无载体。 | 无 Hub 出口。 | 注释非生产点。 | `rg` occurrence；注释正文本身不运行，不以它作为交付证据。 |
## 2. 对账统计与未声称事项

- **49 total occurrences**：与 `grep -c 'HUB_DEGRADE_' claude1_protocol.py` 精确相等。
- **48 executable occurrences**：请求 27、非流 response 4、流区 17；最后一行 `:6984` 是注释，因此不算 executable。
- **34 distinct `HUB_DEGRADE_*` codes**。
- 48 个 executable occurrence 均有可到达的 message-turn 载体和通用 journal 代码路径证据，故“有通用落盘代码路径佐证”为 **48/48**。这不表示每个 occurrence 都有专属端到端测试。
- 流 runtime warning 在 `bridge.finish()` 后统一取出；translated/native application error terminal 现在只写 errors，不写 usage。已发送 request 的 response-transform/connect 失败和最终 route target exhaustion 会带实际 request warning codes；validation reject、非最终 route candidate 和 count_tokens 不制造 P0 turn degradation。
- **usage 计数与 errors 归因是两套口径，不要互相压制**：usage 行的 `deg` 会被 `claude1 usage` 按回合计数，所以非 turn 的路径（count 探针）根本不写 usage；errors 行的 `deg` 只归因某一次失败、从不进计数器，所以 pre-send pool reject、count 探针失败、route target exhaustion 都照常带上请求侧 warning codes。曾经为压制计数而在 errors 侧抹掉 `deg` 的做法已移除——它掩盖降级却保护不了任何计数器。
- 本表没有声称每个 occurrence 都有专属 E2E：证据链由协议触发测试、通用 carrier journal 测试和源码路径组成；明确无专属测试的行已直接标注。

## 3. 不以 `HUB_DEGRADE_` 命名但 disposition=DEGRADED 的旁注

- `HUB_USAGE_PROVENANCE_UNAVAILABLE`：`claude1_protocol.py:2645` 的非流 usage 缺 input/output counter 分支，以及 `claude1_protocol.py:6968` 的 stream `finish()` 缺 usage provenance 分支。它们可随已交付成功回合进入 usage，或随失败回合进入 errors，但**不计入上面的 49 行、48 executable occurrence 和 34 distinct code**。

## 4. 文档自查脚本

以下脚本检查表格行数、源文件 occurrence 数、distinct code 数，以及**逐行的 code 与行号对齐**；不修改工作树：

```bash
python3 - <<'PY'
from pathlib import Path
import re
import subprocess

source = Path("claude1_protocol.py")
doc = Path("docs/degrade-inventory.md").read_text(encoding="utf-8")
table = doc.split("## 1. occurrence 表", 1)[1].split("## 2. 对账统计", 1)[0]
rows = [line for line in table.splitlines() if line.startswith("| `HUB_DEGRADE_")]
assert len(rows) == 49, len(rows)
occurrences = int(subprocess.check_output(["grep", "-c", "HUB_DEGRADE_", str(source)]))
text = source.read_text(encoding="utf-8")
codes = set(re.findall(r"HUB_DEGRADE_[A-Z0-9_]+", text))
assert occurrences == 49, occurrences
assert len(codes) == 34, len(codes)
assert sum("注释非生产点" in row for row in rows) == 1

# 逐行校验。只数总量发现不了写错的行号，而协议层每次改动都会让行号整段漂移
# ——2026-08-17 两轮修复各让全表过期一次。
actual = [
    (index + 1, re.search(r"HUB_DEGRADE_[A-Z0-9_]+", line).group(0))
    for index, line in enumerate(text.splitlines())
    if "HUB_DEGRADE_" in line
]
documented = [
    (
        int(re.search(r"claude1_protocol\.py:(\d+)", row).group(1)),
        re.match(r"\| `(HUB_DEGRADE_[A-Z0-9_]+)`", row).group(1),
    )
    for row in rows
]
drift = [
    (position, doc_entry, real_entry)
    for position, (doc_entry, real_entry) in enumerate(
        zip(documented, actual, strict=True), 1
    )
    if doc_entry != real_entry
]
assert not drift, drift
print(
    f"inventory_rows={len(rows)} source_occurrences={occurrences} "
    f"distinct={len(codes)} drift=0"
)
PY
```
