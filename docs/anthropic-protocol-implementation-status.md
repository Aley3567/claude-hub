# Anthropic 多协议内核实施状态

> 实施合同：`docs/anthropic-protocol-compatibility-research.md`
> 基线冻结日期：2026-08-10

## 阶段 0：已冻结基线

- `python3 -m unittest tests.test_claude1_protocol -v`：35/35 通过。
- `python3 -m unittest discover -s tests -p 'test_*.py' -v`：339/339 通过。
- `zsh tests/test_install.zsh`：6/6 通过。
- `zsh tests/test_shell_integration.zsh`：11/11 通过。
- 开始实施时工作树已有 15 个已修改文件和 5 个未跟踪文件；这些内容均为需保留的既有工作。

## 合同测试 seam

测试只从下列稳定公开边界观察协议行为：

1. 请求：`prepare_request` / `transform_request`；
2. 非流响应：`prepare_response` / `transform_response`；
3. 流响应：`AnthropicStreamBridge` / `translate_sse_chunks`；
4. Hub HTTP：`POST /v1/messages` 与 `POST /v1/messages/count_tokens`。

## 阶段 0 审计发现与当前处置

下列是开始实施时发现的缺口；本节记录当前代码已经落到的边界，而不是把历史问题误写成现状。

| 审计发现 | 当前处置 | 仍有的边界 |
| --- | --- | --- |
| 原生 Hub 曾把 `messages[].role == "system"` 原样送往严格 Anthropic/SGLang。 | `prepare_request(..., "anthropic")` 会把合法的 Claude Code system-role 扩展提升、合并到顶层 `system`，并记录 `HUB_DEGRADE_SYSTEM_ROLE_PROMOTED`。Hub 的 native `/v1/messages` 路径已调用该 seam。 | `claude1 <native provider>` 的直连 launcher 不经过 Hub；见“Launcher 边界与未覆盖 beta”。包含 message 级额外字段或非 text system block 的提升会拒绝，避免伪造。 |
| Chat/Responses 上游在干净 EOF 缺 terminal 时可能被补成正常 `message_stop`。 | 协议 bridge 与 Hub 生产流都在 EOF 调用显式 parser/FSM 的 `finish()`；没有有效 terminal、UTF-8 无效、JSON 不完整或 terminal 后继续内容时，已开始的下游传输会中止，而不是补成功。 | 下游可能已经收到有效的前缀字节；这只能通过断开传输表达失败，不能在已提交的 SSE 头后改写为 JSON 4xx。 |
| 跨协议循环会静默跳过未知块或元数据。 | Chat/Responses 请求 IR 只接受登记的字段、block 与 tool；未知请求字段/已知 block 的未知字段/未知 block 均返回稳定 `HUB_UNSUPPORTED_*` 错误。未知上游 output item/event 也 fail closed。 | 原生 Anthropic 保持透明（除了 system-role 兼容规范化）；它不能替严格上游验证未来原生扩展。 |
| `content_filter`、thinking signature、citation、cache 用量曾有伪造或静默丢失风险。 | refusal/content filter 统一为 `stop_reason: "refusal"`；signature 仅在上游真实提供时发出；citation 不能无损映射时只记录降级；usage 只复制实际的上游计数。 | 跨协议的 citation/cache-control/server-tool 语义仍不等价，按下表降级或拒绝。 |
| `/count_tokens` 估算没有 provenance。 | Hub 对估算返回 `source=estimate`、`method=json_utf8_bytes_div_4`、`exact=0`、`error-bound=unbounded`；原生 count endpoint 成功时标记 `source=upstream`、`method=anthropic_count_tokens`、`exact=1`。 | 估算不是模型 tokenizer 的精确结果。 |

## 适配器支持矩阵

下表是 `protocol_capability_matrix()` 的能力摘要，不单独充当每个 wire field 的完整合同；紧随其后的请求、非流响应和 SSE 三张 disposition registry 才是方向明确的实施边界。`observable degradation` 指默认 `visible_lossy` 模式会记录稳定 warning code；请求阶段 Hub 会将可在响应前得知的 warning 写入 `x-hub-protocol-warnings`，流运行时新增的 warning 写入日志。`strict` 模式会将可降级项转为明确错误，而不是静默继续。

`Gemini generateContent` 仅是独立 reserved seam：它不在 `API_FORMATS`、Hub 配置或路由中，调用 `prepare_request(..., "gemini_generate_content")` 会得到 `HUB_ADAPTER_UNAVAILABLE`。表中的 `reject` 不表示已有 Gemini 支持。

| Anthropic Messages 字段/内容能力 | Native Anthropic | OpenAI Chat | OpenAI Responses | Gemini reserved seam |
| --- | --- | --- | --- | --- |
| 顶层 `system` text | `exact` | `exact` | `exact` | `reject` |
| Claude Code `messages[].role="system"` 扩展 | `observable degradation` | `exact` | `exact` | `reject` |
| system block metadata（例如 `cache_control`） | `exact` | `observable degradation` | `observable degradation` | `reject` |
| text / image | `exact` | `exact` | `exact` | `reject` |
| document | `exact` | `observable degradation` | `observable degradation` | `reject` |
| `search_result` | `exact` | `observable degradation` | `observable degradation` | `reject` |
| citations / annotations | `exact` | `observable degradation` | `observable degradation` | `reject` |
| client custom tool / `tool_use`（基础字段） | `exact` | `exact` | `exact` | `reject` |
| tool `strict` | `exact` | `exact` | `exact` | `reject` |
| tool metadata / `BatchTool` | `exact` | `observable degradation` | `observable degradation` | `reject` |
| 平坦、非 error 的 `tool_result` | `exact` | `exact` | `exact` | `reject` |
| 嵌套 `tool_result.content` | `exact` | `observable degradation` | `observable degradation` | `reject` |
| `tool_result.is_error` | `exact` | `observable degradation` | `observable degradation` | `reject` |
| thinking text | `exact` | `observable degradation` | `observable degradation` | `reject` |
| thinking control / budget → effort | `exact` | `observable degradation` | `observable degradation` | `reject` |
| 请求中的 Anthropic thinking signature（默认 visible-lossy；strict 拒绝） | `exact` | `observable degradation` | `observable degradation` | `reject` |
| 任意来源的 `redacted_thinking` | `exact` | `reject` | `reject` | `reject` |
| Hub namespaced reversible Responses redaction carrier 回放 | `exact` | `reject` | `exact` | `reject` |
| server tool | `exact` | `reject` | `reject` | `reject` |
| MCP / `mcp_servers` | `exact` | `reject` | `reject` | `reject` |
| tool search / deferred loading | `exact` | `reject` | `reject` | `reject` |
| code execution / computer/web server execution | `exact` | `reject` | `reject` | `reject` |
| request / content `cache_control` | `exact` | `observable degradation` | `observable degradation` | `reject` |
| `metadata` / `service_tier` | `exact` | `exact` | `exact` | `reject` |
| `top_k` | `exact` | `observable degradation` | `observable degradation` | `reject` |
| `stop_sequences` | `exact` | `exact` | `observable degradation` | `reject` |
| JSON Schema `format: uri` normalization | `exact` | `observable degradation` | `observable degradation` | `reject` |
| `container` / `inference_geo` | `exact` | `reject` | `reject` | `reject` |
| 基础 usage（上游计数存在；Chat/Responses 的 input 计数扣除 cache-read 后映射） | `exact` | `exact` | `exact` | `reject` |
| 基础 usage（上游计数缺失） | `exact` | `observable degradation` | `observable degradation` | `reject` |
| 已登记、合法且一致的 cache usage counter / split detail | `exact` | `exact` | `exact` | `reject` |
| 已登记且合法的 server-tool usage counter / detail | `exact` | `exact` | `exact` | `reject` |
| reasoning/audio/prediction usage detail | `exact` | `observable degradation` | `observable degradation` | `reject` |
| 未知、malformed 或冲突的 usage/cache/server carrier | native passthrough* | `reject` | `reject` | `reject` |
| `/count_tokens`（标准原生上游 endpoint 可用） | `exact` | `reject` | `reject` | `reject` |
| `/count_tokens` 本地估算 fallback | `observable degradation` | `observable degradation` | `observable degradation` | `reject` |
| 未登记的请求字段、内容字段或内容块 | `exact` native passthrough* | `reject` | `reject` | `reject` |

\* 原生透传的唯一兼容预处理是上表的 system-role normalization；它不把未知原生 block 解释成已支持功能。

### 请求字段 disposition registry

以下 registry 适用于 Anthropic Messages → Chat/Responses 请求方向。Native Anthropic 除 system-role extension normalization 外保持透明；Chat/Responses 对未列入 `_CROSS_REQUEST_FIELDS` 的字段一律 `HUB_UNSUPPORTED_REQUEST_FIELD`。

| 请求字段 / union | Chat | Responses | 精确边界或拒绝条件 |
| --- | --- | --- | --- |
| `model`、`max_tokens` | `exact` | `exact` | 非空 model、正整数 token；malformed 分别拒绝。 |
| 普通 user/assistant role、text、image | `exact` | `exact` | role、content、source shape 必须通过 IR allowlist；空 content 数组以 `HUB_INVALID_CONTENT_BLOCK` 拒绝。 |
| `system` text、embedded system role | `exact` | `exact` | embedded role 会规范化；空/null/空 block 或额外 message 字段拒绝。Native 的 role promotion 是 observable degradation。 |
| `temperature`、`top_p`、`stream` | `exact` | `exact` | 类型/范围先在 IR 校验；未知控制字段拒绝。 |
| `stop_sequences` | `exact` | `observable degradation` | Responses 默认记录 `HUB_DEGRADE_STOP_SEQUENCES_DROPPED`，strict 拒绝。 |
| `top_k` | `observable degradation` | `observable degradation` | 目标无等价 carrier；strict 拒绝。 |
| `tool_choice`、`parallel_tool_calls`、`disable_parallel_tool_use` | `exact`（登记子集） | `exact`（登记子集） | `auto/any/none/tool(name)` 与一致的 parallel 控制可映射；未知 enum、malformed、互相冲突拒绝。 |
| `tools[].strict`、基础 function schema | `exact` | `exact` | metadata/BatchTool 是 observable degradation；server/MCP/tool-search/code execution/deferred loading 明确拒绝。 |
| `output_config.format` | `exact`（JSON Schema 子集） | `exact`（JSON Schema 子集） | 只接受 `json_schema`；未知 format/字段拒绝；schema `format: uri` 清理是 observable degradation。 |
| `output_config.effort` / `thinking` control | `observable degradation` | `observable degradation` | 使用 target reasoning effort carrier；budget/adaptive 映射有稳定 warning，strict 拒绝 lossy 分支。 |
| `metadata`、`service_tier` | `exact`（adapter payload shape） | `exact`（adapter payload shape） | 仍受 provider/model capability profile 约束；非 object metadata、空 tier 拒绝。 |
| `cache_control`（request/system/content/tool） | `observable degradation` | `observable degradation` | 删除并记录稳定 warning；strict 拒绝。 |
| document/search/citation/nested tool_result/is_error | `observable degradation` | `observable degradation` | 使用可见 provenance text/JSON envelope；无法登记的 source/part/field 拒绝。 |
| signature / redacted thinking | signature 降级、redaction `reject` | signature 降级；仅 namespaced reversible carrier `exact` replay | 不生成 signature；任意 opaque redaction 不冒充 adapter provenance。 |
| `container`、`inference_geo`、`mcp_servers` 及未知顶层字段 | `reject` | `reject` | 分别返回稳定 `HUB_UNSUPPORTED_*`。 |

### 非流响应 disposition registry

| 上游响应字段 / output union | Chat → Anthropic | Responses → Anthropic | 拒绝边界 |
| --- | --- | --- | --- |
| wrapper `id` / `model` | `exact`（存在时） | `exact`（存在时） | 非空 string；未知 wrapper 字段或 malformed core 为 `HUB_UPSTREAM_RESPONSE_INVALID`。 |
| `object` / `created` / tier / fingerprint / logprobs 等 wrapper metadata | `observable degradation` | `observable degradation` | shape 先校验；无 Anthropic carrier时记录 `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED`。 |
| assistant text | `exact` | `exact` | 非 assistant role、未知 part 或未知 item fail closed。 |
| reasoning summary / reasoning text carrier | unsigned thinking，`observable degradation` | unsigned summary，`observable degradation` | signature 不伪造；Responses 非空 `reasoning.content` / `reasoning_text` 仍拒绝。 |
| encrypted reasoning | `reject` | namespaced reversible redaction carrier `exact` | 非 string、未知字段或任意未登记 opaque replay 拒绝。 |
| refusal / content filter | `exact` stop semantics | `exact` stop semantics | 输出 text，并固定 `stop_reason=refusal`。 |
| completed client tool call | `exact` | `exact` | id/name/JSON object arguments 必须完整；空 JSON 文本、orphan、server-tool discriminator 或 index/identity 冲突拒绝。 |
| terminal stop/status | `exact`（登记 reason） | `exact`（登记 status/reason） | 未知 reason、缺 terminal reason、成功态携带 error，以及 truncation/refusal 与 tool output 冲突均拒绝。 |
| citation/annotation metadata | `observable degradation` | `observable degradation` | 正文保留，location 不猜测；strict 流模式拒绝。 |
| base usage | 有真实 counter 时 `exact`；缺失 `observable degradation` | 同左 | malformed、负数、bool 或未知 carrier 拒绝；OpenAI base input 计数先扣除 cache-read，差值为负拒绝。 |
| `total_tokens` | 两项 base 都在时校验一致；缺 base 不反推 | 同左 | 完整 snapshot 中总数冲突为 `HUB_UPSTREAM_USAGE_INVALID`。 |
| cache/server usage | 已登记、合法且一致时 `exact` | 同左 | nested/direct alias、cache total/split 冲突、detail 无 total、未知字段或 malformed shape 拒绝。 |
| reasoning/audio/prediction detail | `observable degradation` | `observable degradation` | 逐路径记录 response metadata warning；strict 流模式拒绝。 |

### SSE disposition registry

| SSE 能力 | disposition | 明确边界 |
| --- | --- | --- |
| 任意 transport chunk boundary、UTF-8 code point 分割、CRLF/LF/CR 混合行尾、流首 BOM | `exact` | invalid UTF-8、partial JSON、incomplete event、单事件超限分别以稳定 `HUB_SSE_*` 拒绝；解析按扫描偏移保持 O(n)。 |
| Chat/Responses terminal lifecycle | `exact` | 缺 terminal、terminal/status 冲突、重复/迟到事件、terminal 后内容均 fail closed。 |
| response `id` / `model` 跨 chunk identity | `exact` | 首次观察锁定；变化或 message_start 后迟到冲突为 `HUB_SSE_DUPLICATE_CONFLICT`。 |
| text/refusal/reasoning delta + done snapshot | `exact`；unsigned reasoning degraded | 任意合法坐标配对、suffix repair 与幂等；错序、负坐标、snapshot 冲突拒绝。 |
| terminal-only `response.output` message/reasoning/function call | text/tool `exact`；unsigned reasoning degraded | done 与 terminal snapshot 幂等；output item type/id/content 冲突拒绝。 |
| Responses tool id 仅有 output index 或 anonymous fallback | `observable degradation` | 记录 `HUB_DEGRADE_SYNTHETIC_TOOL_ID`；strict 为 `HUB_SSE_TOOL_CALL_INVALID`，内部 alias 不泄漏。 |
| streamed tool arguments | `exact` | 必须最终形成显式 JSON object；空 string、partial final JSON、2 MiB 超限、identity/order conflict 拒绝。 |
| citation/annotation event | `observable degradation` | 必须关联正确 item/output/content、开放的 output_text part、合法 metadata carrier/index；strict 拒绝。 |
| sequence/logprobs/obfuscation、known wrapper metadata | `observable degradation` | 稳定 metadata warning；unknown wrapper/event 字段拒绝。 |
| upstream error detail | sanitized error + `observable degradation` | detail shape 校验，不回传 vendor secret；成功 terminal 携带 error 拒绝。 |
| usage stream snapshots | 与非流 registry 相同；未观测字段省略不伪造 | counter regression、unknown/malformed/conflict 拒绝；结束时缺 base 标记 provenance unavailable；terminal 才到达的 input usage 由 message_delta 如实携带并记录 `HUB_DEGRADE_LATE_INPUT_USAGE`。 |

### 请求与内容块的具体语义

- **Canonical IR 与 fail-closed。** Chat/Responses 先解析为 `RequestIR`，再由独立 adapter 编码。消息、tool、已知 content block 的额外字段不能绕过 IR；未知类型以 `HUB_UNSUPPORTED_CONTENT_BLOCK`、`HUB_UNSUPPORTED_CONTENT_FIELD`、`HUB_UNSUPPORTED_TOOL_*` 或 `HUB_UNSUPPORTED_REQUEST_FIELD` 拒绝。
- **System normalization。** 合法的 embedded system text block 会按原顺序追加到已有顶层 `system`，保留 block metadata；输入对象不原地修改。native `strict` 模式拒绝此客户端扩展，避免把隐含重写伪装成严格官方 payload。Chat/Responses 本身有 system role carrier，因此该 role 是 exact；但 block metadata 没有等价 carrier，必有可观察降级。
- **`tool_result`。** tool causality（唯一、此前的 `tool_use`、role 正确）在跨协议前验证；违反时是 request-phase `ProtocolRequestError`（`HUB_INVALID_TOOL_CAUSALITY`,HTTP 400，带 path)，不会冒泡成非结构化 500。嵌套 text/image/document/search_result 与 `is_error=true` 采用明确 JSON envelope：`{"type":"anthropic_tool_result","is_error":...,"content":...}`。该 envelope 保留原始值但不是目标协议的原生 error 字段，故为 observable degradation；不受支持的嵌套 part 直接拒绝。
- **Document、search 与 citation。** 对 Chat/Responses，文本 document/search context 使用带标题/source 的 provenance text envelope；无法抽取的 document 用明确 placeholder。`document.source.type=content` 与 `search_result.content` 的 nested text block 递归执行 allowlist：citation metadata 记录 `HUB_DEGRADE_CITATION_METADATA_DROPPED`，`cache_control` 记录 `HUB_DEGRADE_CONTENT_METADATA_DROPPED`，strict 均拒绝，未知 nested field fail closed。响应 citation/annotation 不猜测 Anthropic location，也不把上游 URL 伪造进输出；SSE 还要求 metadata event 精确关联开放的 item/output/content part。
- **Thinking、signature 与 redaction。** thinking text 使用 Chat reasoning carrier 或 Responses reasoning carrier，并记录降级。默认 `visible_lossy` 模式会删除跨协议请求中的 Anthropic signature 并给出 `HUB_DEGRADE_THINKING_SIGNATURE_DROPPED`；`strict` 模式明确拒绝。非流 Chat 上游若给出无法验证的 signature 会拒绝；流中只有真实 `reasoning_signature`/`signature_delta` 才成为 Anthropic `signature_delta`，从不生成假值。无 signature 的 thinking 记录 `HUB_DEGRADE_UNSIGNED_THINKING`。Chat 无安全 redacted carrier 而拒绝；Responses 的 namespaced prefix + Base64 是可逆 carrier，不是 MAC、签名或来源认证，只允许这一登记格式回放，任意其他 opaque `redacted_thinking` 仍拒绝。
- **Refusal。** Chat `refusal`、`content_filter` 与 Responses refusal event/output 统一映射为 Anthropic text + `stop_reason: "refusal"`；未知上游 finish/stop reason 拒绝，绝不降格为 `end_turn`。
- **服务器执行能力。** server tool、MCP、tool search、code execution，以及相应 server result block，在跨协议请求进入上游前按精确错误码拒绝。此拒绝不影响上游已经提供的 `server_tool_use` **usage counter** 的保留；counter 不是 server-tool invocation 的伪造支持。

### Usage、cache 与 count-token provenance

- cache read/write、`cache_creation` 和 `server_tool_use` 只在上游实际提供、字段已登记、counter 合法且多 carrier 一致时保留。read/write alias、cache total/split detail 冲突，未知 usage/detail 字段或 malformed shape 均为 `HUB_UPSTREAM_USAGE_INVALID`。`cached_tokens` 精确保留；reasoning/audio/prediction detail 逐字段记录 `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED`。`cache_creation` split detail 存在而 total counter 缺失同样拒绝，不输出无 total 的半截 cache 结构。
- OpenAI 的 base input 计数（`prompt_tokens`/`input_tokens`）包含 cached tokens，而 Anthropic `input_tokens` 语义不含 cache read；二者同时观测到时输出 `input_tokens = base - cache_read`，差值为负按 `HUB_UPSTREAM_USAGE_INVALID` 拒绝。base counter 缺失时不做减法，走缺失 provenance 分支。
- 流式 usage 只发出上游实际观测到的字段：`message_start` 不再恒发 `input_tokens: 0`/`output_tokens: 0`，`message_delta` 不再恒发 `output_tokens: 0`，客户端因此能区分真实 0 与未观测；一个计数器都未观测到时（含上游显式发空 `usage={}`），终态 `message_delta` 省略整个 `usage` 键而非携带空对象。terminal 才到达的晚期 input usage 由 `message_delta` 如实携带，并保留 `HUB_DEGRADE_LATE_INPUT_USAGE` 可观测性；`message_start` 已携带的 input usage 不重复。上游 wrapper 缺 `model` 时输出空串并逐响应记录 `HUB_DEGRADE_UPSTREAM_RESPONSE_METADATA_DROPPED`(`$.model`)，不猜测模型名。
- `total_tokens` 始终校验为非负 counter；同一 snapshot 的 input/output 两项都存在时必须等于二者之和。缺任一 base 时不反推、不分摊、不伪造，缺项记录 `HUB_USAGE_PROVENANCE_UNAVAILABLE`。对客户端 schema 必须存在的基础计数可使用带 warning 的 `0` 占位，但 usage JSONL 会移除这些占位，只落实际观察到的上游 counter，并将完全不可用的记录标为 `source=unavailable`。
- `cache_control` 的跨协议请求语义不能无损表达，因此被移除并带 `HUB_DEGRADE_CACHE_CONTROL_DROPPED`；这与“缓存 usage counter 已从上游返回”的 exact 保留是不同能力。
- Hub 的 native `/v1/messages/count_tokens` 成功透传结果标记为 upstream/exact。对于 Chat/Responses、native full-URL provider，或 native endpoint 返回 404/405/501 的 fallback，Hub 不联想上游 tokenizer：只返回上述带 provenance 的本地估算。
- Native 非流响应即使使用 gzip/deflate，客户端仍收到原始 bytes/header；Hub 只为 usage 遥测做有界 shadow decode。无效、超限或不支持的 representation 不影响透明响应，但该 usage row 标为 unavailable。

### SSE 状态机与 golden / invariant 覆盖

- `SSEParser` 与 `AnthropicStreamBridge` 是跨协议流的显式状态机；native raw SSE 使用 `_SSETerminalTracker`/`_SSEUsageTracker` 验证 terminal 与 usage，而非把不完整流补成成功。
- Responses 的 `content_part.added/done`、`reasoning_summary_part.added/done`、text/summary delta 与 done snapshot 会按 item/index 生命周期显式配对；合法 snapshot 只补缺失 suffix，冲突、错序或未知 part fail closed。terminal item 快照调和成功即视为对应 part done（省略 `*.done` 事件的兼容路径），并记录 done snapshot 供幂等比对。Chat content→reasoning→content 交错时，开启 thinking 前会先关闭已打开的 text block，保证 Anthropic block 序列合法。无 structural wrapper 的兼容上游 delta/done 仍走独立坐标验证路径。citation/annotation event 同样校验 item/output/content 坐标、part open 状态、metadata carrier shape/index，strict 模式拒绝有损 metadata。
- Terminal `response.output` 会重建此前未出现的完整 message、reasoning 或 function call snapshot；与 `output_item.done` 重复时幂等，identity enrichment 只补充缺失 id。相同 output index 的 type/id/content 或 encrypted snapshot 冲突会拒绝。只有 output index/anonymous identity 的 function call 使用公开 synthetic id 并记录 `HUB_DEGRADE_SYNTHETIC_TOOL_ID`，strict 拒绝；namespaced 内部 alias 不进入 Anthropic wire output。
- Chat/Responses wrapper 的 response id/model 首见锁定，跨 chunk 改变拒绝；sequence、logprobs、obfuscation、known wrapper metadata 与 sanitized error detail 走稳定 observable degradation，unknown/malformed wrapper fail closed。
- 每个 tool arguments 聚合上限 2 MiB，text/summary 每 part 2 MiB、每 bridge 合计 16 MiB，内容块总数 4096；上限按 UTF-8 bytes 计数，超限返回稳定 `HUB_SSE_*_TOO_LARGE` / `HUB_SSE_TOO_MANY_BLOCKS`。Responses 的 output item/output item id/redacted snapshot 注册表同样以 4096 条为上限，output item id 另限 1024 字符。tool arguments 即使为空也必须最终形成显式 JSON object，不能把 `""` 伪造成 `{}`；SSE 单事件边界使用 `HUB_SSE_EVENT_TOO_LARGE`。
- Golden fixtures 位于 `tests/fixtures/anthropic_protocol/sse/`：`chat_utf8_crlf`、`responses_refusal`、`responses_tool_partial`，并与 request 的 `claude_code_system_role` golden fixture 一同覆盖稳定序列化。
- `tests/test_protocol_sse_invariants.py` 覆盖任意及 fuzzed transport chunk 边界、UTF-8 分割、CRLF、partial JSON/tool arguments、响应 snapshot suffix 修复、invalid UTF-8/JSON、资源上限、重复/冲突 terminal、乱序/迟到事件、usage regression、无 terminal EOF、refusal、citation 与 cache/server usage。
- `tests/test_claude_hub.py` 额外覆盖生产 Hub 的原生/转译 SSE：有效 `message_stop` 或 `error` terminal、CR-only、BOM、压缩流、terminal 后事件、缺 terminal 的真实 loopback 连接中止。已开始 SSE 无法回滚时只 abort transport，绝不追加伪造的 `message_stop`。

## Launcher 边界与未覆盖 beta

- **Hub 路径已覆盖，native direct 未覆盖。** `claude-hub.py` 的 native `/v1/messages` 在转发前调用 `prepare_request(..., "anthropic")`，所以严格 Hub 上游可获得 system-role normalization。`claude-provider-once.py` 的 `launch_provider()` 对 `api_format == "anthropic"` 仍是 `apply_native_account_pool()` → `ensure_local_gateway()` → `launch_with_settings()` 的直连路径；它不会启动 Hub，也不会经过该 normalizer。
- **不能把现有 protocol bridge 直接套到 native direct。** bridge config 只带 primary provider selector，Hub 的账号池按请求重新 acquire；而 native launcher 的账号池契约是会话开始选择一次 credential、整个 Claude session 固定。直接复用会改变会话和故障切换语义。
- **严格 Anthropic/SGLang direct provider。** 当前没有 `sglang`/strict-system/upstream capability metadata，也没有经测试的 fixed-selected-credential bridge。因此不得按 URL、模型或 provider 名猜测并自动改路；该 direct 场景是未覆盖 beta，不能以 Hub 修复宣称已解决。
- **Gemini。** 只有 matrix/profile reserved seam；没有 endpoint、认证、Hub config、request/response/SSE adapter 或真实 provider integration，故仍是未覆盖 beta。
- **微信 Coding Plan smoke。** 已通过正式透传语法 `launcher.main(["--hub", "--", "-p", "微信 Coding Plan"])` 做隔离 smoke：临时 HOME/0600 SQLite provider、fixture token、fake Claude 与 `127.0.0.1` upstream 实际走过两轮 launcher → Hub → Chat，请求文本和 tool schema，接收 `tool_use`，再回传 nested text/document `tool_result(is_error=true)` 并验证显式 envelope。字面 `claude1 "微信 Coding Plan"` 仍会把首个位置参数解释为 provider hint，不是 prompt 语法；smoke 不使用真实 Claude 二进制或真实 provider token。  <!-- secret-guard: allow private-provider-name 65941246a1 -->
- **尚未引入的原生 beta 功能。** 跨协议的 server tool/MCP/tool search/code execution、container、inference geo 仍按上表明确拒绝；新未知 native extension 仅在 native passthrough 路径透明，不能被写成已转换支持。
- **Responses 新 reasoning/content 事件。** 当前稳定覆盖 reasoning summary 与 adapter-tagged encrypted reasoning；非空 `reasoning.content`、`reasoning_text` part/event 及其他未来 content-part union 会以稳定 code 拒绝，尚未建立独立 carrier 与 golden。
- **Citation location。** 可读正文保留、metadata 降级可观察，但没有跨 Chat/Responses 的 Anthropic page/char/web-search location round-trip，绝不猜测 URL、页码或 offset。
- **Provider/model capability profile。** 当前 profile 只区分 adapter endpoint/availability；`metadata`、`service_tier`、JSON Schema 子集、reasoning carrier 等仍可能被具体 OpenAI-compatible provider 拒绝。矩阵描述 adapter shape，不是对所有 provider/model 的无条件保证。

## 现有证据入口

- 请求/响应 capability contract：`tests/test_protocol_contract.py`（system golden、未知 block fail-closed、metadata、nested tool result、document/search/citation、server-tool gate、thinking/redaction、refusal、usage provenance）。
- SSE golden 与不变量：`tests/test_protocol_sse_invariants.py`，以及 `tests/fixtures/anthropic_protocol/`。
- 既有协议回归：`tests/test_claude1_protocol.py`。
- 生产 Hub、native forwarding、count-token provenance 与流中止：`tests/test_claude_hub.py`。
- launcher session/账号池/bridge secret 边界与微信 Coding Plan 隔离 smoke：`tests/test_launcher.py`；它仍未提供 strict-native direct bridge 的 integration smoke。  <!-- secret-guard: allow private-provider-name 65941246a1 -->

## 最终验证（2026-08-11,review 修复轮）

- 完整 Python 测试：`PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -p 'test_*.py'`，508/508 通过。
- 协议定向测试：`tests.test_claude1_protocol`、`tests.test_protocol_contract`、`tests.test_protocol_sse_invariants`，合计 176/176 通过。
- 生产路径定向测试：`tests.test_claude_hub`、`tests.test_launcher`、`tests.test_hub_catalog`、`tests.test_statusline_model`、`tests.test_account_pool`，合计 295/295 通过；完整 discover 还覆盖其余 Python 回归。
- 微信 Coding Plan 隔离 smoke：1/1 通过。该用例使用临时 HOME、0600 SQLite provider、fixture token、fake Claude 和 `127.0.0.1` fixture upstream；没有读取、输出或发送真实 provider token。  <!-- secret-guard: allow private-provider-name 65941246a1 -->
- Shell integration：`zsh tests/test_shell_integration.zsh`，11/11 通过。
- 安装回归：`zsh tests/test_install.zsh`，6/6 通过。
- Python 语法：`python3 -m py_compile claude-provider-once.py claude-hub.py claude1_protocol.py claude1_account_pool.py claude_hub_catalog.py statusline-model.py scripts/secret_guard.py` 通过。
- Shell 语法：`sh -n install.sh` 以及 `zsh -n zsh-functions.sh zsh-sticky-integration.sh tests/test_shell_integration.zsh tests/test_install.zsh` 通过。
- 工作树安全检查：`python3 scripts/secret_guard.py --working-tree --no-private-sources` 与 `git diff --check` 均通过。
- 未进行真实第三方 provider 的联网调用；跨 provider 证据来自公开 adapter 边界、loopback fixture 和上述不变量测试，不能据此扩大为所有 OpenAI-compatible 实现都支持相同 beta 子集。
