# cc-switch 代理/格式转换实现调研 — 对照 claude1 的 id/response 兼容问题

> 调研日期：2026-08-16
> 动机：claude1 的协议桥频繁出现 id/response 及各类不兼容问题；cc-switch(farion1231/cc-switch) 对 OpenAI 及各供应商格式兼容良好、且能完整暴露上游错误。本文记录 cc-switch 的实现构造原理，并对照本仓库 `claude1_protocol.py` 找出差距与可落地改进点。
> 配套文档：本仓库实现基线见 [claude1-protocol-baseline-2026-08-16.md](claude1-protocol-baseline-2026-08-16.md)。

---

## 1. 调研对象与版本

- 本机克隆：`~/Documents/Codex/2026-06-07/cc-switch`,HEAD `27c41f7`(2026-06-07,约 v3.16.2 时期),**已包含完整本地代理 + Anthropic↔OpenAI 转换**,不是旧版纯配置切换器。
- GitHub 最新：**v3.19.2(2026-08-06)**。代理/转换代码全部自研内建于 `src-tauri/src/proxy/`(Rust),无外部 converter 依赖(`transform.rs` 注释提到参考 anthropic-proxy-rs,但非运行时依赖)。
- 代理监听 `127.0.0.1:15721`,路由:`/v1/messages`、`/claude/v1/messages`(Claude)、`/v1/chat/completions`、`/v1/responses`、`/v1/models`(Codex)、`/v1beta/*`(Gemini)。注册点 `src-tauri/src/proxy/server.rs:287-356`。

### 转换触发条件(重要前提)

cc-switch 的格式转换**只在请求经过它的本地代理、且 provider 的 `meta.apiFormat` 为非 anthropic 时触发**;直连上游不做任何转换。这正是当年火山 GLM thinking 关不掉(2026-06 调研)、以及 claude1 早期直连绕过转换层(2026-07 调研)的根因。claude1 因为"单次会话、不污染全局配置"的需求自建协议桥,与 cc-switch 转换层是**平行实现**,所以两者实现细节的差异直接决定兼容表现。

---

## 2. cc-switch 的核心做法(代码级)

### 2.1 message id 策略:直接复用上游 id

- OpenAI Chat 响应的 `id`(`chatcmpl-xxx`)**原样作为** Anthropic 响应的 `id`,不生成 `msg_` 前缀(`transform.rs:524-700`)。
- Anthropic→Responses 方向:空 id → `resp_ccswitch`;已有 `resp_` 前缀则保留;否则加 `resp_` 前缀(`transform_codex_anthropic.rs`)。
- **对照 claude1**:bridge 自己生成 `msg_<uuid>`(`claude1_protocol.py:4153`),上游 id 只作 `upstream_message_id` 首见锁定,流中途 id 变化直接抛 `HUB_SSE_DUPLICATE_CONFLICT`(`:4231-4258`)。cc-switch 的做法更简单、对"上游流中途换 id/model 的网关"天然免疫;代价是不满足严格依赖 `msg_` 前缀的客户端(Claude Code 不依赖)。

### 2.2 tool call 映射:以 OpenAI `index` 为 key 的 HashMap

- 流式转换维护 `tool_blocks_by_index: HashMap<usize, ToolBlockState>`(`streaming.rs:161, 365-379`):OpenAI `tool_calls[].index` 为 key,内部分配递增的 Anthropic content block index。**同一 index 的乱序/分片 delta 都能正确归位**。
- 请求侧 Anthropic `tool_use.id` 原样透传为 `tool_calls[].id`,不重新生成(`transform.rs:402-413`),保证 tool_result 链路可追踪。
- Copilot 无限空白防护:tool 参数连续空白 >500 字符强制中止该 tool call 流(`streaming.rs:93-100, 414-427`)。
- **对照 claude1**:用显式 FSM 保证 block index 单调、start/stop 配对;Responses 匿名 tool call 合成 id 并记 `HUB_DEGRADE_SYNTHETIC_TOOL_ID`,strict 拒绝(`:5783, 6594-6602`);tool 参数最终必须是 JSON object,否则 `HUB_SSE_TOOL_ARGUMENTS_INVALID`(`:4494-4507`)。claude1 更严格,遇到"参数是字符串/数组"的网关直接拒,cc-switch 选择宽容转发。

### 2.3 SSE 流式事件序列(Chat → Anthropic)

`streaming.rs:138-654` 的状态机:

1. 首个 chunk 到达才发 `message_start`(usage 先填 0)。
2. reasoning/text/tool_calls 按 delta 类型切换 content block,分别发 `content_block_start` + 对应 delta。
3. **收到 `finish_reason` 时只缓存 `message_delta`,等 `[DONE]` 再发**——保证 usage 完整且只发一次,避免多 finish chunk 产生重复 `message_delta`。
4. `[DONE]` 时发缓存的 `message_delta` + `message_stop`。
5. **流异常结束(无 finish_reason)不发 `message_delta`/`message_stop`**,不把失败伪装成成功;上游流出 Err 时发 `event: error` + `{"type":"error","error":{"type":"stream_error",...}}`(`streaming.rs:611-625`)。

### 2.4 SSE 解析鲁棒性(`sse.rs:1-86`)

- 同时识别 `\n\n` 和 `\r\n\r\n` 块分隔;
- `data: {...}` 与 `data:{...}`(无空格)都兼容;
- `append_utf8_safe` 处理跨 TCP chunk 截断的多字节 UTF-8,避免中文变 U+FFFD。
- 首字节超时 60s、静默期 120s(`response_processor.rs:678-799`),超时向客户端 yield io::Error。

### 2.5 错误处理:透传优先 + 结构化富化

这是用户感知"cc-switch 能完整暴露问题"的直接原因:

- **上游错误透传**(`error.rs:79-174`):HTTP 状态码保持上游原码;body 是合法 JSON **原样透传**;非 JSON 包装为 `{"error":{"message":"<body>","type":"upstream_error"}}`。
- **错误码映射**(`error_mapper.rs:19-63`):超时/流空闲 504、连接失败 502、无可用 provider/熔断 503、认证 401、配置/请求 400、转换错误 422、其他 500。
- **Codex 端点富化**(`handlers.rs:981-1120`):统一包装为 OpenAI 风格错误体,保留 `provider`/`model`/`endpoint`/`upstream_status` 结构化字段;413 时把上游 nginx HTML 换成明确的人话说明;非 JSON 错误体截断 1024 字节包装。
- **fail closed 的边界**:Anthropic error envelope 在请求转换入口就抛 TransformError;Responses `status: failed/cancelled` 在响应转换入口抛错;v3.19.2 起一轮工具调用全部被丢弃时发 `response.failed` 而不是伪装 `completed`。
- **对照 claude1**:转译路径上游 ≥400 经 `transform_error()` 整形为 Anthropic error shape,真实 reason 脱敏后入 message/header(`claude1_protocol.py:3768-3789`);native 路径原码原 body 透传(`claude-hub.py:3561-3581`)。两者方向一致,差异在 claude1 的脱敏更激进(512 字符截断 + token/URL/key shape 剥离),cc-switch 更倾向于保留上游原始错误体。

### 2.6 请求转换的"保洁"细节(`transform.rs`)

这些不起眼的小处理是兼容性好的重要来源:

- `system` 开头的 `x-anthropic-billing-header:` 行剥离,避免 prefix cache 失效(`:18-47`);
- 多条 system 的 cache_control 一致才合并,冲突则丢弃 cache_control(`normalize_openai_system_messages`, `:274-344`);
- `clean_schema` 移除 tool schema 里的 `format: "uri"`,避开部分上游校验失败(`:502-521`);
- 流式请求注入 `stream_options.include_usage`,防第三方上游漏报 token;
- o-series 模型 `max_tokens` → `max_completion_tokens`;
- `tool_choice`: `any`→`required`、`{"type":"tool","name"}`→`{"type":"function","function":{"name"}}`;过滤 BatchTool;
- `thinking` 按模型能力映射 `reasoning_effort`(low/medium/high/xhigh)。

### 2.7 供应商适配:按"平台"而非"模型名"推断

- 平台级 reasoning 配置以 `name + base_url` 识别(DeepSeek/Kimi/GLM/硅基流动/OpenRouter/MiniMax/百炼/阶跃各有 override),**不按模型名猜**,避免托管平台(A 平台 host B 家模型)误判(`providers/codex.rs:210-333`)。
- Moonshot/Kimi/DeepSeek/MiMo:`normalize_anthropic_tool_thinking_history_for_provider` 修复 assistant tool_use 历史缺失的 thinking block(`providers/claude.rs:98-147`)。
- GitHub Copilot:注入整套指纹头(editor-version、x-initiator、x-interaction-type 等),warmup 降级 gpt-5-mini(`providers/claude.rs:840-885`、`forwarder.rs:1141-1234`)。
- Codex OAuth:强制 `store:false` + `include:["reasoning.encrypted_content"]` + `stream:true`;客户端发非流请求时**上游 SSE 聚合为完整 JSON 再转非流响应**(`handlers.rs:298-309, 1256-1321`)。
- 模型映射独立成层(`model_mapper.rs:19-141`),剥离 Claude Code 的 `[1M]` 上下文后缀后再进转换。

---

## 3. cc-switch 踩过的坑(修复历史,与格式/id/stream/error 相关)

| 版本(日期) | 修复 |
|---|---|
| v3.19.2 (2026-08-06) | Chat→Responses 工具调用全被丢弃时不再伪装 `completed`,改发 `response.failed`;响应缓冲封顶 128MiB |
| v3.19.1 (2026-07-31) | Grok Build 非 Responses 后端 404 且每请求生成新 session id;dedup id 命名空间不一致致重复计数 |
| v3.19.0 (2026-07-30) | 工具结果图片从字符串化文本抽取为原生媒体(避免 ~9000 倍 token 膨胀);畸形上游响应 fail closed 不 panic |
| v3.18.0 (2026-07-21) | Codex 转换层四项:工具 schema 归一化 object、reasoning 跨轮回传、**流式工具调用 identity 与顺序保持**、catalog 字段兜底 |
| v3.17.0 (2026-07-13) | Responses↔Anthropic 桥 fail closed;reasoning/tool results 无损往返;cache-write 记账修复 |
| v3.16.4 (2026-06-27) | zstd 请求/错误体解压;OAuth-over-proxy 修复 |
| v3.16.3 (2026-06-14) | 标签错误的 SSE body 加固;OAuth token 与接管残留恢复 |
| v3.16.2 (2026-06-07) | 流式截断、tool_choice/custom-tool 边界、系统消息规范化、**更清晰的上游错误** |

观察:cc-switch 的兼容性不是一次设计出来的,而是**持续按真实上游的畸形行为打补丁**;整体策略是"宽容解析、保守生成、错误不伪装"。

---

## 4. 关键差异对照:为什么 claude1 老炸、cc-switch 没事

| 维度 | cc-switch | claude1(现状) | 影响 |
|---|---|---|---|
| message id | 直接复用上游 id | 自生成 `msg_<uuid>`,上游 id 首见锁定,变化即抛错 | claude1 对流中途换 id/model 的网关直接 `HUB_SSE_DUPLICATE_CONFLICT`(薄弱点 #3) |
| 上游新增响应字段 | 未知字段忽略/宽容 | fail closed 拒绝(`:2882-2894`, `:3984-4016` allowlist) | 上游加字段 → claude1 全量失败(薄弱点 #4/#5) |
| 自定义 SSE 事件名 | 未显式限制 | 只接受 `event: message`,其余 `HUB_SSE_UNKNOWN_EVENT`(`:6097-6101`) | 网关发 `ping` 等事件即被拒(薄弱点 #6) |
| tool 参数非 object | 宽容转发 | 拒绝 `HUB_SSE_TOOL_ARGUMENTS_INVALID` | 兼容性差(薄弱点 #10) |
| 流异常终态 | 不发 message_stop,发 error 事件 | 写 `event: error` 后 EOF,一致 | 两者相当 |
| 上游错误体 | 原样透传 JSON,非 JSON 包装 | 整形为 Anthropic error + 强脱敏 | cc-switch 信息更全,claude1 更安全 |
| 缺失 message id | 复用上游,上游没有就没有 | 生成 UUID 占位 | claude1 的占位 id 无法关联上游账单/日志(薄弱点 #2) |

**核心结论**:cc-switch 的兼容策略是"宽容解析 + 透传优先 + 错误不伪装但信息全量暴露";claude1 是"fail-closed + 严格校验 + 强脱敏"。用户感知的"id/response 不兼容",大头来自 claude1 的严格策略在上游多样性面前不断触发拒绝路径——这些拒绝在安全上是刻意的,但对兼容性就是 bug 级体验。

---

## 5. 对 claude-hub 的可落地建议

按投入产出排序,每条对应基线报告的薄弱点编号:

1. **SSE identity 锁定降级为降级路径而非硬错误**(#3):上游流中途 id/model 变化时,记录 `HUB_DEGRADE_IDENTITY_MIDSTREAM` 并继续(以首发值为准),仅 strict 模式抛错。参照 cc-switch 直接复用上游 id 的做法。
2. **非流响应未知 wrapper 字段从 reject 改为 observable degradation**(#4/#5):保留已知字段转换,未知字段忽略并记 warning code;allowlist 改为"已知危险字段才拒"。这是消除"上游加字段就全挂"的关键。
3. **SSE 未知事件名跳过而非拒绝**(#6):`ping`、注释行、自定义事件直接忽略(可计数记录),只对 `data` 缺失等真异常报错。
4. **缺失 message id 时复用上游任何可用标识**(#2):优先级 上游 id → 上游 request id 头 → 生成 UUID,并在生成时记 `HUB_DEGRADE_SYNTHETIC_MESSAGE_ID`,保持与 `_synthesize_message_stream` 路径语义一致(#8)。
5. **tool 参数非 object 的网关适配**(#10):参数是合法 JSON 字符串时尝试二次解析;解析失败包一层 `{"_raw": ...}` 并记降级,而非直接拒绝。
6. **tool_calls index 乱序归位**:确认 `AnthropicStreamBridge` 对 OpenAI `tool_calls[].index` 乱序/分片的处理与 cc-switch 的 HashMap 方案等价;若有假设顺序到达的代码路径,补乱序测试。
7. **错误暴露做分层**:保留脱敏 journal 现状,但下游响应的 error message 可携带更多上游原始结构(如上游 `error.type`/`error.code`),对齐 cc-switch 的"透传 JSON 错误体"体验——用户抱怨的"暴露不出问题信息"主要在这一层。
8. **请求保洁三件套可直接借鉴**:billing header 剥离、tool schema `format:"uri"` 清理、`stream_options.include_usage` 注入——检查 claude1 是否已有等价处理,没有则在 `transform_request` 补上。

## 6. 资料索引

- 本仓库基线报告:[docs/claude1-protocol-baseline-2026-08-16.md](claude1-protocol-baseline-2026-08-16.md)(639 测试通过,10 个薄弱点带行号)
- cc-switch 本机克隆:`~/Documents/Codex/2026-06-07/cc-switch`(HEAD `27c41f7`)
- cc-switch GitHub:https://github.com/farion1231/cc-switch(最新 v3.19.2,2026-08-06)
- cc-switch 关键源码(仓库内路径):
  - `src-tauri/src/proxy/providers/transform.rs` — Anthropic↔OpenAI Chat 转换
  - `src-tauri/src/proxy/providers/streaming.rs` — OpenAI SSE → Anthropic SSE 状态机
  - `src-tauri/src/proxy/providers/transform_responses.rs` / `transform_codex_anthropic.rs` / `streaming_*.rs` — Responses 各方向桥
  - `src-tauri/src/proxy/sse.rs` / `error.rs` / `error_mapper.rs` / `model_mapper.rs`
  - `src-tauri/src/proxy/providers/claude.rs` / `codex.rs` — 平台级供应商适配
- 历史调研(本机):`~/Desktop/cutesticky/_archive/cc-switch-火山渠道慢-根因调研.md`、`~/Documents/Codex/2026-07-17/r-w/outputs/ReClaude-CCSwitch-配置地图与修复报告.md`
- 对照实现:musistudio/claude-code-router(transformer 机制;issue #1397 曾坏 reasoning→tool_calls 的 delta)、BerriAI/litellm(issue #29491 OpenAI→Anthropic 流式丢 `input_json_delta`)——说明这类桥的 id/delta 状态机是业界共同难点,cc-switch 目前的实现是本调研见过的最完整参考。
