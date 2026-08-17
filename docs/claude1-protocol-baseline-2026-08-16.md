# claude1 协议实现只读基线调查报告

> 仓库：`/Users/admin/Desktop/claude-hub`
> 调查日期：2026-08-16
> 基线冻结：`docs/anthropic-protocol-implementation-status.md` 记载的 2026-08-10
> 测试结果：`python3 -m unittest discover -s tests -p 'test_*.py'` → **639 tests OK**

---

## 1. 整体架构与数据流

### 1.1 模块边界

| 文件 | 职责 | 不做什么 |
| --- | --- | --- |
| `claude1_protocol.py` | Anthropic Messages ↔ OpenAI Chat/Responses 的请求、非流响应、SSE 流转换； capability matrix；错误脱敏与证据提取 | provider 路由、凭证、账号池、HTTP 传输 |
| `claude1_protocol_types.py` | 共享类型：`ProtocolTransformError`、`ProtocolRequestError`、`ConversionPlan`、IR 容器 | 无线 shape 转换 |
| `claude1_protocol_usage.py` | usage counter、cache carrier、一致性校验、`UsageReceipt` | 协议字段映射 |
| `claude1_transport.py` | `UpstreamExecutor`、传输策略、探测、冷却、安全重试 | 协议转换 |
| `claude-hub.py` | HTTP 网关：`/v1/messages`、`/v1/messages/count_tokens`、日志/遥测/错误日志、配置与路由 | 具体字段语义转换由 `claude1_protocol` 完成 |
| `claude_hub_catalog.py` | 命名 Hub catalog 校验与解析 | 运行时代理 |
| `claude1_account_pool.py` | 账号池状态机 | 协议转换 |

### 1.2 请求数据流

```text
Claude Code (Anthropic Messages JSON)
    ↓
claude-hub.py handle_messages / handle_count_tokens
    ↓ 本地校验 (JSON、model、auth、Connection 头、Content-Encoding)
route(model_in, cfg, providers) → (alias, model_out)
    ↓
_forward_to_channel
    ↓
api_format = provider.get("api_format", "anthropic")
    ├─ anthropic: prepare_request(payload, "anthropic") → 字节透传
    │              (仅做 system-role extension normalization)
    ├─ openai_chat: prepare_request(payload, "openai_chat")
    │              → /v1/chat/completions
    └─ openai_responses: prepare_request(payload, "openai_responses")
                   → /v1/responses
    ↓
_post_with_account_failover → UpstreamExecutor
    ↓
上游响应
```

### 1.3 响应数据流

| 上游格式 | 下游形态 | 关键 seam |
| --- | --- | --- |
| Anthropic (native) | 字节/头透传；SSE 仅校验 terminal 与 usage | `prepare_request(..., "anthropic")`、`_SSETerminalTracker` |
| OpenAI Chat 非流 | Anthropic JSON message | `prepare_response(body, "openai_chat")` |
| OpenAI Responses 非流 | Anthropic JSON message | `prepare_response(body, "openai_responses")` |
| OpenAI Chat SSE | Anthropic SSE | `AnthropicStreamBridge("openai_chat")` |
| OpenAI Responses SSE | Anthropic SSE | `AnthropicStreamBridge("openai_responses")` |

非流 JSON 若请求了 `stream=true`，Hub 会调用 `_synthesize_message_stream()` 把完整 message 一次性模拟成 SSE。

### 1.4 流式转换核心

- `SSEParser`：`claude1_protocol.py:3807`，按 SSE 规范解析任意 chunk 边界、UTF-8 跨 chunk、CRLF/LF/CR 混合行尾。
- `AnthropicStreamBridge`：`claude1_protocol.py:4150`，显式状态机管理 `message_start`、内容块生命周期、工具参数聚合、thinking signature、terminal/error。
- `StreamStateMachine`：`claude1_protocol.py:4037`，保证 block index 单调、start/stop 配对、delta 只能指向开放 block。

---

## 2. id 处理

### 2.1 message id

| 场景 | 行为 | 文件:行号 |
| --- | --- | --- |
| SSE 转换（Chat/Responses） | bridge 自身生成 `msg_<uuid>`；上游 `id` 作为 `upstream_message_id`，首次观察后锁定，变化则 `HUB_SSE_DUPLICATE_CONFLICT` | `claude1_protocol.py:4153`, `4231` |
| 非流 Chat/Responses 转 Anthropic | 上游 `body.id` 存在则用，否则生成 `msg_<uuid>` | `claude1_protocol.py:3246`, `3591` |
| native Anthropic | 完整透传上游 message id，不做重写 | `claude-hub.py:3316-3324` |

### 2.2 tool_use / tool_result id

| 方向 | 行为 | 文件:行号 |
| --- | --- | --- |
| 请求：Anthropic → Chat | `tool_use.id` 原样作为 Chat `tool_calls[].id` | `claude1_protocol.py:1395` 附近 adapter |
| 请求：Anthropic → Responses | `tool_use.id` 原样作为 Responses function `call_id` | `claude1_protocol.py:1411` 附近 |
| 响应：Chat → Anthropic | Chat `tool_calls[].id` → Anthropic `tool_use.id`；重复 id 拒绝 | `claude1_protocol.py:3191-3204`, `3221` |
| 响应：Responses → Anthropic | `call_id` 优先，否则 `id`；重复拒绝 | `claude1_protocol.py:3443-3456`, `3473` |
| 请求：tool_result 因果校验 | 每个 `tool_result.tool_use_id` 必须对应更早且未消费的 `tool_use.id` | `claude1_protocol.py:897-923` |

### 2.3 request id

- Hub 本身不生成 request id；Claude Code 自带的 `anthropic-…` request id 请求头原样透传给 native Anthropic。
- 转译路径使用 OpenAI 兼容头，不携带 Anthropic request id。

### 2.4 已知不一致问题

1. **Responses function_call 可能只有 `output_index` 或完全匿名**：bridge 会合成 id，标记 `HUB_DEGRADE_SYNTHETIC_TOOL_ID`；strict 模式拒绝。合成键不会泄漏到 Anthropic wire，但 downstream `tool_use.id` 是 Hub 生成的 `responses:output:0` 或 `response_function_call_N`，不是上游真实 id。见 `claude1_protocol.py:5783`, `6598-6602`。
2. **非流 Chat/Responses 缺失 `id` 时 Hub 重新生成**：这意味着上游若本来不发 message id，Claude Code 看到的 id 是本地生成的，无法用于关联上游账单/日志。见 `claude1_protocol.py:3246`, `3591`。
3. **SSE 上游 id 变化直接抛错**：不会尝试合并或重写，而是 `HUB_SSE_DUPLICATE_CONFLICT`。见 `claude1_protocol.py:4231-4258`。
4. **native 路径 message id 完全透明**：如果 strict upstream 对 id 格式有新要求，Hub 不会规范化。见 `claude-hub.py:3316-3324`。

---

## 3. 格式兼容性

### 3.1 后端支持矩阵

来自 `docs/anthropic-protocol-implementation-status.md` 的能力摘要：

| 能力 | Native Anthropic | OpenAI Chat | OpenAI Responses |
| --- | --- | --- | --- |
| text / image / client tool | exact | exact | exact |
| `system` 顶层 / embedded system role | exact / observable degradation | exact | exact |
| document / search_result / citations | exact | observable degradation | observable degradation |
| thinking text / thinking control | exact | observable degradation | observable degradation |
| thinking signature | exact | observable degradation (默认丢弃) | observable degradation (仅 namespaced reversible carrier exact) |
| redacted_thinking | exact | reject | reject (仅 provenanced exact) |
| tool_result nested / is_error | exact | observable degradation | observable degradation |
| cache_control / metadata / stop_sequences | exact / 部分 degradation | 部分 degradation | 部分 exact |
| server tool / MCP / code execution / container / inference_geo | exact | reject | reject |
| usage base / cache / server | exact | exact | exact |
| count_tokens | exact upstream | estimate fallback | estimate fallback |

### 3.2 Anthropic ↔ OpenAI 关键字段转换

| 方向 | Anthropic | OpenAI Chat | OpenAI Responses | 备注 |
| --- | --- | --- | --- | --- |
| 请求 system | 顶层 `system` 或 messages 内 role=system | system message | `instructions` | embedded system role 会被提升到顶层 `system` |
| 请求 tool_choice | `auto/any/none/tool{name}` | `auto/any→required/none/tool{name}` | 同左 | parallel 控制同步映射 |
| 请求 thinking | `thinking.budget/adaptive` | `reasoning_effort` | `reasoning.effort` | budget→effort 是 degradation |
| 响应 stop_reason | `end_turn/max_tokens/stop_sequence/tool_use/refusal/pause_turn/…` | `stop/tool_calls/content_filter` | `completed/incomplete` + `incomplete_details.reason` | 未知 reason 拒绝，不伪装 |
| 响应 thinking | `thinking` block | `reasoning_content` | `reasoning.summary` | 无 signature 时标记 `HUB_DEGRADE_UNSIGNED_THINKING` |
| 响应 tool_use | `tool_use` block | `tool_calls[].function` | `output[].function_call` | Responses 匿名 tool 合成 id |
| usage input | `input_tokens`（不含 cache read） | `prompt_tokens`（含 cache read） | `input_tokens`（含 cache read） | 转换时 `input_tokens = base - cache_read` |

### 3.3 已知缺陷 / hack / 降级

1. **`cache_control` 跨协议被整体删除**：目标协议没有等价 carrier，只能降级。见 `claude1_protocol.py:2183-2200`。
2. **`document`/`search_result` 文本化**：用 provenance text envelope 包裹，不能解码的 document 用 placeholder。见 `claude1_protocol.py:587-707`。
3. **嵌套 `tool_result.content` 和 `is_error` 使用私有 envelope**：`{"type":"anthropic_tool_result",...}`，不是目标协议原生字段。见 `claude1_protocol.py:424-481`。
4. **thinking signature 默认丢弃**：跨协议请求中真实 signature 会被删除（visible_lossy），strict 拒绝。见 `claude1_protocol.py:891` 测试与实现。
5. **`redacted_thinking` 仅支持 Responses namespaced reversible carrier**：任意 opaque redaction 拒绝。见 `claude1_protocol.py:198-203`, `3524-3530`。
6. **`stop_sequences` 在 Responses 中被丢弃**：strict 拒绝。见 `_PROTOCOL_CAPABILITY_MATRIX` 与请求 registry。
7. **`top_k` / `context_management` / `metadata`(Chat) 等字段降级丢弃**：见 `_DROPPED_REQUEST_FIELDS`、`_CROSS_REQUEST_FIELDS` 处理。
8. **非 Anthropic `/count_tokens` 本地估算**：`len(json_utf8_bytes)//4`，误差无界，必须带 `x-hub-estimated: 1` 等 provenance 头。见 `claude-hub.py:2435-2462`。

---

## 4. 错误处理

### 4.1 上游错误如何暴露给 Claude Code

| 场景 | 行为 | 文件:行号 |
| --- | --- | --- |
| 请求阶段校验失败 | `protocol_request_error()` → 400 + `x-hub-protocol-code` | `claude-hub.py:1355-1363` |
| 转译路径上游 HTTP ≥400 | `transform_error()` → Anthropic shape `{type:"error", error:{type, message}}`；真实 reason 经 `upstream_error_evidence()` 脱敏后写入 message/头 | `claude1_protocol.py:3768-3789`, `claude-hub.py:2803-2846` |
| 转译路径 SSE 中错误帧 | bridge 生成 `event: error`，携带脱敏后的 code/message；标记 terminal | `claude1_protocol.py:6145-6184` (Chat), `6698-6775` (Responses) |
| native 路径上游 HTTP ≥400 | 原状态码 + 原 body 透传；body 经 shadow decode 后提取 reason 记入 error journal | `claude-hub.py:3561-3581` |
| native 路径 SSE 中断/缺 terminal | 直接 abort transport；error journal 记录 `phase=stream`、异常类型、渠道 | `claude-hub.py:3435-3521` |
| 转译路径 SSE 中断 | 尝试向下游写 `event: error` 后 `write_eof`；写失败则 abort | `claude-hub.py:2967-3029` |
| 路由组 target 耗尽 | 返回最后一个 target 的错误（last-error-wins），带 `x-hub-route` | `claude-hub.py:3167-3205` |

### 4.2 错误脱敏

- `sanitize_error_text()`：512 字符截断、剥离控制字符、脱敏 Bearer token、URL、`token=`/`api_key=` 等赋值形状、`sk-…` key shape。见 `claude1_protocol.py:3687-3701`。
- `upstream_error_evidence()`：支持 `{"error":{...}}`、`message`、`detail`、`msg`、字符串 error、双重 JSON 包装。code 必须匹配 `[A-Za-z0-9_.-]{1,64}`。见 `claude1_protocol.py:3704-3765`。

### 4.3 日志与 journal

- `record_error()` 只写入：ts、phase、channel、model、format、status、code、message、route、exc_type；不写请求/响应 payload。见 `claude-hub.py:444-487`。
- `record_usage()` 写入：in/out、cache、server_tool_use、source、method、exact、channel、model、account、instance。见 `claude-hub.py:388-443`。

---

## 5. TODO / FIXME / HACK / XXX 与 xfail

**结论：仓库中不存在 `TODO`、`FIXME`、`HACK`、`XXX` 注释，也不存在 `xfail`。**

- 已用 `Grep` 扫描所有 `.py` 文件：无匹配。
- 存在的跳过只有平台相关的 `@unittest.skipUnless`：
  - `tests/test_account_pool.py:450` — POSIX 文件权限
  - `tests/test_claude_hub.py:293, 436` — `O_NOFOLLOW`
  - `tests/test_claude_hub.py:529, 574` — POSIX 权限
  - `tests/test_launcher.py:1363, 1729, 4267, 4285` — POSIX 文件/锁安全

这些不是功能 xfail，是跨平台能力守卫。

---

## 6. 最可能导致 id/response 不兼容的 10 个薄弱点

| # | 薄弱点 | 风险 | 文件:行号 |
| --- | --- | --- | --- |
| 1 | **Responses function_call 匿名时合成 tool_use id** | Claude Code 用该 id 调用 tool，回传 `tool_result.tool_use_id` 时 Hub 必须能反向识别；目前通过 `responses:output:N` / `response_function_call_N` 别名维持，但若上游同时省略 `output_index` 且存在多个匿名 tool，可能冲突或无法回溯。 | `claude1_protocol.py:5783`, `6594-6602` |
| 2 | **非流 Chat/Responses 缺失 `id` 时生成新 UUID** | 下游无法关联上游账单/日志；native 路径无此问题，但转译路径常见。 | `claude1_protocol.py:3246`, `3591` |
| 3 | **SSE 上游 message id/model 首见锁定，变化即抛错** | 某些网关会在流中途变更 model/id（如路由切换），会直接触发 `HUB_SSE_DUPLICATE_CONFLICT` 而非平滑处理。 | `claude1_protocol.py:4231-4258` |
| 4 | **非流 Chat/Responses 响应 wrapper metadata 未知字段 fail closed** | 上游新增字段（如 OpenAI 新加的 `accepted_prediction_tokens` 等）会被直接拒绝，而不是降级，导致兼容版本落后时所有请求失败。 | `claude1_protocol.py:2882-2894`, `3271-3306` |
| 5 | **Responses `_RESPONSES_STREAM_RESPONSE_FIELDS` 是硬编码 allowlist** | 上游新增响应字段会被当成 unsupported 拒绝；这是设计上的 fail-closed，但需要频繁更新字段清单。 | `claude1_protocol.py:3984-4016` |
| 6 | **Chat SSE 只接受 `event: message`** | 某些 OpenAI 兼容网关使用自定义事件名（如 `ping`、`tool_call`）会被 `HUB_SSE_UNKNOWN_EVENT` 拒绝。 | `claude1_protocol.py:6097-6101` |
| 7 | **非流响应 `model` 缺失时输出空串** | Claude Code 可能期望有效 model 名用于后续调用；虽然记录了降级，但 wire 上空 model 不友好。 | `claude1_protocol.py:3249`, `3594` |
| 8 | **`_synthesize_message_stream` 生成的 message id 来自上游 body.get("id")** | 若上游 body 也没有 id，则合成 UUID；与真实 SSE 路径生成的 id 语义不一致。 | `claude-hub.py:2577-2600` |
| 9 | **native Anthropic 路径完全字节透传，不校验未来扩展** | 若 Claude Code 发送新原生字段且 strict upstream 返回新响应 shape，Hub 不会拦截或适配，错误直接暴露。 | `claude-hub.py:3316-3324`, `3408-3428` |
| 10 | **工具参数最终必须是 JSON object** | 某些网关可能把 tool 参数当字符串/数组返回，会被 `HUB_SSE_TOOL_ARGUMENTS_INVALID` 拒绝；虽有安全理由，但降低了兼容性。 | `claude1_protocol.py:4494-4507`, `2781-2794` |

---

## 7. 附加说明

- **测试规模**：`tests/test_claude1_protocol.py` 35 个；`tests/test_protocol_contract.py` 覆盖请求/响应合同；`tests/test_protocol_sse_invariants.py` 覆盖 SSE 不变量；`tests/test_claude_hub.py` 覆盖网关生产路径；总计 639 个测试通过。
- **Launcher 边界**：`claude1 <native provider>` 的直连路径不经过 Hub，因此 `prepare_request(..., "anthropic")` 的 system-role normalization 对 direct native provider 不生效。这是文档明确记录的未覆盖 beta。见 `docs/anthropic-protocol-implementation-status.md` §Launcher 边界。
- **Gemini**：只有 reserved seam，无真实 adapter，调用 `prepare_request(..., "gemini_generate_content")` 返回 `HUB_ADAPTER_UNAVAILABLE`。
