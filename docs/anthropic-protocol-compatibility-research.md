# Anthropic Messages 多协议兼容层研究与实施边界

> 更新日期：2026-08-10  
> 范围：`claude1_protocol.py`、`claude-hub.py`，以及 Anthropic Messages 与 OpenAI Chat Completions、OpenAI Responses、后续 Gemini 适配之间的请求、响应、SSE、工具调用、缓存和错误语义。

## 结论

当前 `claude1_protocol.py` 已经是一个可靠的协议桥基线，不应推倒重写。它已经覆盖 Chat/Responses 双向转换、工具调用因果校验、thinking/reasoning、图片、usage、错误整形，以及对异常流结束、重复 terminal 和迟到事件的 fail-closed 处理；现有 35 个协议测试全部通过。

下一阶段真正需要解决的不是“再加几个字段”，而是把转换器升级为一个有明确语义合同的协议内核：

1. 以 Anthropic 官方 SDK 类型为协议面真相源，不以 Claude Code 当前恰好发送的 payload 为全部规范；
2. 在 Anthropic、OpenAI Chat、OpenAI Responses 和未来 Gemini 之间引入规范化中间表示（IR），避免每一对协议各写一套互相漂移的转换；
3. 每个字段和内容块必须落入三类之一：**精确支持、可观察降级、明确拒绝**；不得静默丢弃；
4. 流式转换使用显式有限状态机，验证 block 生命周期、索引、工具参数增量、thinking signature、terminal 和 error 顺序；
5. 原生 Anthropic → Anthropic 继续尽量透明透传；跨协议转换只承诺目标协议能表达的语义，不伪造 citation、signature、cache write 或 server-tool 结果；
6. `count_tokens` 必须明确区分上游精确值和本地估算值，现有 `JSON 字节/4` 只能作为标记过的 fallback，不能宣称等价于 Anthropic token counting。

因此，“近乎完美兼容”的可验收定义应是：常用 Claude Code 会话无回归；所有官方稳定内容块都有确定行为；无法无损表达的功能会稳定、可测试、可观察地降级或失败；任何协议异常都不会被伪装成成功响应。

## 已验证的本地基线

### 已有能力

本地协议层的主要入口是：

```text
provider_api_format
anthropic_to_chat
anthropic_to_responses
transform_request
chat_to_anthropic
responses_to_anthropic
transform_response
transform_error
SSEParser
AnthropicStreamBridge
translate_sse_chunks
```

已验证行为包括：

- Anthropic `system` 顶层字段转换为 Chat 的 system message 或 Responses `instructions`；
- text、base64/URL image、client `tool_use`、`tool_result`；
- Chat `reasoning_content` 和 Responses reasoning summary 到 Anthropic `thinking`；
- Responses `redacted_thinking` 历史块到 `reasoning.encrypted_content`；
- tool-use ID 唯一性、tool-result 必须引用更早且未消费的 tool-use；
- Chat 和 Responses 非流式响应转为 Anthropic message shape；
- Chat/Responses SSE 重建 `message_start`、内容块、`message_delta`、`message_stop`；
- tool arguments 的增量拼接和多个上游事件别名去重；
- 干净 EOF 但缺少 terminal、terminal 后迟到内容、重复 done 等边界条件；
- OpenAI 两种 usage shape 的基础 input/output/cache-read/cache-write 映射；
- Anthropic shape 的脱敏上游错误。

基线命令：

```text
python3 -m unittest tests.test_claude1_protocol
...................................
Ran 35 tests in 0.002s
OK
```

### 已确认的缺口

以下不是推测，而是从当前实现的分支覆盖直接得到：

| 领域 | 当前行为 | 风险 | 目标行为 |
| --- | --- | --- | --- |
| 未知请求字段/内容块 | 多个转换循环没有匹配分支时直接跳过 | 新版 Claude Code 字段可能无提示消失 | capability policy 决定透传、降级或 4xx 拒绝，并输出稳定错误码 |
| message role | 为兼容 Claude Code，接受 messages 内 `system`；官方稳定 API 只定义 `user`/`assistant` | 直接发给严格 Anthropic/SGLang 会触发 400 | 入站兼容预处理，将 messages 内 system 提升/合并到顶层 system；严格模式可拒绝 |
| system blocks | 只拼接 text | block 级 `cache_control` 等元数据丢失 | IR 保留 block 元数据；目标不支持时给出降级记录 |
| 请求参数 | `metadata`、`service_tier`、`cache_control`、`container`、`inference_geo`、部分 `output_config`、`top_k` 等未完整处理 | 调度、结构化输出、缓存或地域语义静默丢失 | 按后端能力逐项映射；无法映射时降级/拒绝 |
| document/search_result | Chat/Responses 请求转换无分支 | 文档和搜索上下文静默丢失 | 支持目标原生附件；否则经过有边界的 text extraction/placeholder 降级 |
| tool_result 内容 | 常被整体 JSON stringify | 文本、图片、文档、`is_error` 语义被压平 | IR 保留嵌套块和 error flag；按目标协议分别编码 |
| server tools/MCP | 普通 tools 循环可能把非 client tool 当 function tool，或生成空名工具 | 请求失真、错误执行、错误归因 | 对 server tool、MCP tool、tool search 建独立类型与 capability gate |
| thinking | Chat 历史只保留文字；流式 thinking block 的 signature 为空且没有 `signature_delta` | 回放或严格客户端可能拒绝；不能伪造签名 | 有真实签名才透传；目标不支持时记录不可逆降级，严格模式拒绝 |
| citations | 无请求/响应/SSE 映射 | 引文消失，文本来源不可追踪 | 原生支持则保持结构；否则文本可保留但 citation metadata 明确降级 |
| refusal | OpenAI refusal 被当普通 text；`content_filter` 映射为 `max_tokens` | stop semantics 错误 | 映射到当前 Anthropic refusal/stop details；未知映射不伪装为长度截断 |
| usage | 只生成四个扁平 token 字段 | 缺少 cache 5m/1h split、server tool usage 等 | 保留可验证的细分项；不存在的数据不估造 |
| token counting | 非 Anthropic或 full URL 走 `len(JSON)//4` 本地估算并加 `x-hub-estimated: 1` | 中英文、图片、工具 schema 偏差很大 | 优先原生 count endpoint/模型 tokenizer；估算始终带 provenance 和误差语义 |
| schema 清洗 | 当前只移除 `format: uri` | 不同后端 JSON Schema 子集不同 | 按 provider capability 做递归、可解释的 schema normalization |
| SSE 内容块 | 状态机覆盖 text/thinking/tool-use | 新内容块、citation delta、signature delta、服务端工具结果无状态 | generic block registry + 显式状态迁移和协议不变量 |

官方 SDK 的 `MessageCreateParams` 已包含 `cache_control`、`container`、`inference_geo`、`metadata`、`output_config`、`service_tier`、`thinking`、`tool_choice`、`tools`、`top_k` 等字段；其流式 delta union 包含 `text_delta`、`input_json_delta`、`citations_delta`、`thinking_delta`、`signature_delta`。官方 content-block start union 还包括 `redacted_thinking`、`server_tool_use`、web search/fetch result、code execution result、tool search result 和 container upload。这些类型是协议面扩张的直接证据，而不是第三方项目的猜测。[官方 MessageCreateParams 源码](https://github.com/anthropics/anthropic-sdk-python/blob/009b035/src/anthropic/types/message_create_params.py)；[官方 RawContentBlockDelta 源码](https://github.com/anthropics/anthropic-sdk-python/blob/009b035/src/anthropic/types/raw_content_block_delta.py)；[官方 RawContentBlockStartEvent 源码](https://github.com/anthropics/anthropic-sdk-python/blob/009b035/src/anthropic/types/raw_content_block_start_event.py)

官方 `messages` 文档同时明确：system prompt 应使用顶层 `system`，消息 role 是 `user` 或 `assistant`。因此本项目接受 Claude Code 偶发的 messages 内 `system` 应被定义为**客户端兼容扩展**，不能把该非法 role 原样送给严格上游。[官方 MessageCreateParams 注释](https://github.com/anthropics/anthropic-sdk-python/blob/009b035/src/anthropic/types/message_create_params.py)

## 外部项目证据与取舍

研究时固定了以下源码版本，避免引用随 main 漂移的行为：

| 项目 | 固定提交/版本 | 最值得借鉴 | 不宜直接照搬 |
| --- | --- | --- | --- |
| `anthropics/anthropic-sdk-python` | `009b035` | 官方稳定/beta 类型、字段和 union 的真相源 | SDK 类型不是跨协议降级策略 |
| `openai/openai-python` | `0c09a3f` | Chat/Responses 官方事件和对象 shape | 不负责 Anthropic 语义 |
| `dwgx/WindsurfAPI` | `ed1ac35` | 文档块处理、cache attribution、thinking signature 顺序、token count 边界；测试面最广 | 部分行为依赖其 Cascade/Windsurf 上游，不能直接视为通用协议 |
| `musistudio/claude-code-router` | `186fa61` | 路由、模型发现、Hosted Web Search、可观测性和 adapter 组织 | 核心转换已委托 `@the-next-ai/ai-gateway`，仓库本身不是全部实现 |
| `@the-next-ai/ai-gateway` | npm `1.0.16` | 多 source/target adapter、工具排序屏障、异常断流 fail-closed、原生 server-tool/citation 转发 | 发布包只有编译产物，内部 API 不适合直接复制或依赖私有符号 |
| `1rgs/claude-code-proxy` | `5e45ba6` | 单文件、易读的 LiteLLM 转换路径 | `tool_result` 等存在文本化降级，协议面较窄 |
| `fuergaosi233/claude-code-proxy` | `7ea4177` | request/response converter 分层清晰 | 新版内容块和流式细节覆盖不足 |
| `m0n0x41d/anthropic-proxy-rs` | `59eb97b` | `Idle/Thinking/Text/ToolUse` 显式状态机 | 状态类型仍只覆盖基础内容块 |

### WindsurfAPI 的高价值细节

`WindsurfAPI/src/handlers/messages.js` 明确处理了：

- document source 解码与带 provenance 的文本包裹；不能解码时输出可见 placeholder，而不是静默删除；
- cache creation/read 以及 5m/1h split 的归因；
- thinking block 必须按 `thinking_delta* → signature_delta → content_block_stop` 关闭；
- `input_json_delta` 只能发送到仍打开的 tool block；
- 图片/文档在 token counting 中采取不同估算路径；
- `content_filter → refusal`，而不是映射为 `max_tokens`。

这些行为可作为测试与不变量的参考，但缓存 token 的估算仍需标为估算，不能冒充上游账单真值。[WindsurfAPI messages handler](https://github.com/dwgx/WindsurfAPI/blob/ed1ac35/src/handlers/messages.js)

### Claude Code Router 的真实转换依赖

`claude-code-router` 的协议转换不是完全实现在其仓库内，而是依赖 npm 包 `@the-next-ai/ai-gateway`。研究时检查到版本 `1.0.16`；其文档描述 optimistic stream 下的关键合同：client tool 和 internal tool 经过排序屏障，Anthropic server tools、结果块和 citation 可原生流式转发；`event:error` 是终止事件；缺少 `message_stop` 的异常断流以错误结束，不能补一个成功尾帧。这与本地现有 fail-closed 方向一致，应扩展而不是削弱。[ai-gateway usage 文档](https://www.npmjs.com/package/@the-next-ai/ai-gateway)；[Claude Code Router 仓库](https://github.com/musistudio/claude-code-router/tree/186fa61)

### Rust 状态机的启发

`anthropic-proxy-rs` 使用 `Idle / Thinking / Text / ToolUse` 枚举管理当前开放内容块，并在切换块类型时先发 stop，再分配递增 index。这证明显式状态机比依赖事件 if/else 的隐含顺序更容易验证；但本项目需要把状态扩展为通用 block registry，并单独表达 terminal、error、signature pending、tool argument assembly 和 native passthrough。[stream.rs](https://github.com/m0n0x41d/anthropic-proxy-rs/blob/59eb97b/src/translate/stream.rs)

## 建议架构

### 1. 协议内核与 adapter

建议拆为以下深模块，模块名可在实施时按仓库风格调整：

```text
Anthropic request
       ↓ parse + validate
Canonical Conversation IR
       ↓ capability planning
Target adapter: Anthropic passthrough / Chat / Responses / Gemini
       ↓
Upstream request

Upstream response or events
       ↓ target decoder
Canonical Output IR / Stream transitions
       ↓ Anthropic encoder
Anthropic response or SSE
```

IR 至少需要：

- ordered messages 与 content blocks；
- system block 和 block-level metadata；
- text、image、document、search result；
- client tool call/result 与 `is_error`；
- server tool/MCP/tool-search/code-execution 的独立节点；
- thinking、signature、encrypted/redacted thinking；
- citations 及其 location；
- request controls、output config、cache markers；
- usage provenance；
- stop reason、stop details 和 error；
- unknown extension bag，仅用于同协议安全透传，不能盲目跨协议发送。

### 2. Capability planning

每个 provider/model profile 应声明能力，而不是在转换函数里散落模型名判断：

```text
native | exact-map | lossy-map | unsupported
```

一次请求先生成 conversion plan，再执行。计划应能产出：

- 实际使用的 adapter；
- 发生的降级及稳定 code；
- 被拒绝的字段/块和 JSON path；
- 是否允许流式；
- token count 来源是 upstream/tokenizer/estimate；
- 是否可以安全 retry/fallback。

默认策略建议：

- 原生 Anthropic：保留未知字段和 `anthropic-*` header，尽量透明；
- Chat/Responses：稳定常用字段 exact-map；附件或 citation 只能在明确定义时 lossy-map；
- server tool/MCP/signature 等影响执行或回放完整性的字段，在没有原生能力时默认拒绝，不静默文本化；
- 提供显式 compatibility mode 允许部分 lossy-map，但响应/日志必须可观察。

### 3. 流式状态机不变量

至少编码并测试以下不变量：

1. `message_start` 恰好一次，且早于任何 content block；
2. content block index 单调且不复用；
3. 每个 start 恰好有一个 stop，delta 只能指向开放且类型兼容的 block；
4. tool arguments 可跨任意网络 chunk 边界，但最终必须形成目标合同要求的 JSON；
5. thinking signature 若存在，必须在对应 thinking block stop 前发送且只发送一次；
6. citation delta 只能附着于允许 citation 的 text block；
7. terminal/error 之后不再接受内容；
8. upstream error 不生成正常 `message_stop`；
9. 干净 EOF 但缺少合法 terminal 视为协议错误；
10. usage 更新不得倒退、重复累计或凭空制造 cache/server-tool 数字。

### 4. 错误与可观测性

错误应维持 Anthropic shape，同时区分：

- `invalid_request_error`：入站 Anthropic payload 自身非法；
- `unsupported_feature_error`（内部稳定 code，可置于 message/metadata）：目标 adapter 无法表达；
- `upstream_protocol_error`：上游返回 shape/SSE 顺序违法；
- `api_error`：连接或未知上游故障。

日志不得记录 token 或正文，但可以记录 adapter、provider/model、降级 code、JSON path、stream state、terminal 原因和 token provenance。

## 分阶段实施与验收

### 阶段 0：冻结基线

- 保留当前 dirty working tree，不覆盖用户现有演进；
- 为 35 个现有协议测试建立基线；
- 补充当前 Chat、Responses、Claude Code system-role extension 的 golden fixtures；
- 先记录现有行为，再重构，防止“兼容升级”破坏已经可用的微信 Coding Plan。  <!-- secret-guard: allow private-provider-name 65941246a1 -->

### 阶段 1：协议面与策略层

- 建立 IR、capability matrix 和 conversion plan；
- 对所有当前静默跳过路径改为 exact/degraded/rejected；
- 规范化 messages 内 system role；
- 完整处理 system block metadata、tool_result nested content 与 `is_error`；
- 修正 refusal/content-filter stop semantics。

### 阶段 2：内容块与 usage

- document、search result、citation；
- thinking signature/redacted thinking；
- cache 5m/1h split、server-tool usage；
- server tool、MCP、tool search、code execution 的原生/拒绝策略；
- count_tokens provenance 与误差标记。

### 阶段 3：流式强化

- 用显式状态机覆盖全部支持的 block/delta；
- golden SSE fixtures；
- 每个 byte/chunk boundary 的切分测试；
- partial JSON、UTF-8 边界、CRLF、多行 data、注释、重复/乱序/迟到事件测试；
- property/fuzz tests 验证 block 生命周期和 terminal 不变量；
- backpressure、取消和 bounded buffer 测试。

### 阶段 4：更多目标协议

- 先完成 Chat 和 Responses 的稳定合同；
- 再增加 Gemini adapter，不把 Gemini 特例塞进 Chat adapter；
- 用同一组 Anthropic contract fixtures 跑各 adapter，输出支持/降级/拒绝矩阵；
- 对真实 Claude Code 做隔离 smoke，至少覆盖普通文本、thinking、工具调用、图片、取消和错误。

## 最低完成标准

新实现只有同时满足以下条件才可称为“高度兼容”：

- 当前协议、Hub、launcher 测试无回归；
- 官方稳定请求字段和 content block union 均有明确策略；
- Chat、Responses（以及后续 Gemini）分别产出已支持/降级/拒绝矩阵；
- 未知块不会静默消失；
- signature、citation、server tool、MCP 和 cache usage 不被伪造；
- golden、contract、chunk-boundary 和 malformed-stream 测试通过；
- `count_tokens` 返回值携带可验证 provenance；
- 实际 `claude1 "微信 Coding Plan"` 文本与工具调用 smoke 通过；  <!-- secret-guard: allow private-provider-name 65941246a1 -->
- 文档明确仍未支持的 beta 功能，不用“支持所有 Anthropic 格式”空泛收尾。

## 不确定项

- Anthropic beta 类型扩张很快，SDK main 中部分 2026 版本工具可能尚未被 Claude Code 稳定使用；实施优先级应由捕获到的真实客户端 fixtures 和 beta header 决定。
- OpenAI-compatible 上游对 `reasoning_content`、tool schema、usage 和 SSE 事件的实现差异很大；需要 provider capability profile，不能只按 `openai_chat` 一个标签假设完全一致。
- thinking signature 无法从只返回明文 reasoning 的上游推导；除非上游提供可验证 opaque value，否则只能降级或拒绝，不能生成假签名。
- citation 和 document 的文本化可以保存可读内容，但不等价于保留来源语义；是否默认允许应由 compatibility mode 决定。
- 精确 token counting 需要目标模型 tokenizer 或上游 endpoint；对闭源/未知 tokenizer 只能提供明确标记的估算。

## 下一步

从当前 working tree 创建独立实现任务，使用 `gpt-5.6-sol` 与 `ultra` 推理强度。任务应先审计当前实现并把上述合同转为测试，再做模块重构；不得清理或覆盖现有未提交改动。第一轮交付优先完成 capability policy、system-role normalization、unknown-block fail-closed、refusal 修正和流式状态机测试骨架，然后再扩展新版内容块。
