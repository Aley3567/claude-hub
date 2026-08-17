# 审查发现清单：T0.1–T0.5 与已提交地基

> 2026-08-17。四路并行代码审查（degrade 落盘主实现 · inventory 对账 · 已提交地基 4 commit · 测试有效性）的收敛结果，按 AGENTS.md 第 30 行"缺陷要么修要么记 `docs/`"落账。
>
> **审查范围**：未提交工作树（`claude-hub.py` +414 / `tests/` +1885，即 T0.1–T0.5）+ 已进历史但未经独立 review 的 4 个 commit（`0270718` `90ad994` `5174724` `a3b3244`）。
>
> **行号纪律**：本文行号是 2026-08-17 工作树快照，**会漂移**（审查过程中已经踩过一次：地基那份用的是 HEAD 的 `git archive` 快照，行号与工作树全部错位）。定位一律**以符号名为准**，行号只作粗略指引。
>
> **队列关系**：R1–R6 是 `p0-tasks.md` T0.6 的前置——带着 C1/C2 做端到端验收，会把带缺陷的行为记成基线。R7–R9 不阻塞 T0.6。
>
> **进度**（2026-08-17）：**R1–R6 已修**，各卡附修复记录；R7–R9 未动。
>
> 第一轮（R1/R2）引入新 code `HUB_DEGRADE_DUPLICATE_TERMINAL_SKIPPED`（两处 occurrence），`degrade-inventory.md` 同步到 47→49 occurrence、33→34 distinct、46→48 executable。
>
> 第二轮（R3–R6）不增删 code，occurrence／distinct 数不变；`sanitize_error_text` 扩写使 `:3675` 之后偏移 +43，**全表 18 行行号已重算**。R3 的修法由用户拍板为「count 路径不写 usage 行」。
>
> 一处此前的文档与实现不符已修：头部曾声称 inventory 自查脚本"含逐行行号校验"，而第 4 节脚本实际只数总量——现已把逐行 code+行号对齐写进脚本，声称与实现一致。
>
> 验证：**675 tests OK**（664 + 9 + 2 新增）· inventory 自查 drift=0 · secret_guard 通过 · `git diff --check` 干净。新测试全部做过变异验证（撤掉对应修复即变红）。
>
> 第三轮（R5/R6 的复审补修，2026-08-17）：R5 的修法自身留了两个口子——宽容不足的引号匹配仍会漏凭证尾段、header 值规则漏了形状判定而吃掉普通长单词，均已修并各带测试；R6 补了外层 `except` 的注释；流式 count 的错误终态补了回归测试。不增删 code，occurrence／distinct 数不变；协议层再偏移 +32，**全表 18 行行号与 §2、§3 的两处引用一并重算**。

## 证据分级

本文每项标注证据等级，不混淆"已复现"与"报告主张"：

- **【已复现】**——本轮独立跑过脚本或读码确认了因果链，可直接动手。
- **【报告】**——子代理主张，附了它的复现方式，但本轮未独立核实。动手前先自己验一遍，不要照搬结论。

---

## R1 ✅ 2026-08-17 尾随 `data: [DONE]` 把成功回合变成客户端可见的失败 【已复现】

**症状**：openai_chat 流正常收尾（`finish_reason:stop` + usage + `[DONE]`）后，上游再发一个 `[DONE]`（中转/relay 常见），下游事件序列变成：

```text
message_start … content_block_stop, message_delta, message_stop, error
                                                                 ^^^^^ message_stop 之后又来一个终态
```

usage 一行不落（token 全丢），errors 落 `HUB_SSE_LATE_EVENT`。

**根因**：`AnthropicStreamBridge.feed`（`claude1_protocol.py`，约 `:6075`）第一件事是 `if self.stopped: raise ... HUB_SSE_LATE_EVENT`，而 `[DONE]` 的处理分支在这个检查**之后**（约 `:6081`）。第一个 `[DONE]` 调 `finish()` 置 `stopped`，第二个即抛。随后 hub 的流式异常处理（`claude-hub.py` 约 `:3042-3074`）`record_error` 之后**无条件**写 `sse_event("error", …)`，没有"下游是否已收到终态"的守卫。

**三重违宪**（AGENTS.md）：

1. 第 12 行——尾随 `[DONE]` 既非安全事故也非因果事故，属"未知事件跳过 / 有损放行"档，却被 reject。
2. 第 19 行——"终态只能来自上游的真实终态"，这里 hub 在真实终态之后**自造了第二个终态**。同样形状在 native 路径被 hub 自己判为 `protocol_error`（见 `_SSETerminalTracker._consume_line`）。
3. 第 17 行的镜像——"失败绝不伪装成功"反过来了：成功被伪装成失败。

**修法**：`feed()` 里 `stopped` 且 `data == "[DONE]"` 时返回 `[]`（可记一个 `HUB_DEGRADE_*`，尾随终态属"有损但能用"）；hub 侧写 error 事件前加"下游已收到终态则不再追加"的守卫。

**验收合同**：

1. 双 `[DONE]` 的成功流：下游事件以 `message_stop` 结束，**无**追加的 `event: error`。
2. 同一回合 usage 落一行，token 与单 `[DONE]` 情形一致。
3. errors 不落该回合。
4. 全量 `python3 -m unittest discover -s tests -p 'test_*.py'` 绿。

**修复记录（2026-08-17，未提交）**：新增 `AnthropicStreamBridge._repeats_terminal`；`feed()` 在 `stopped` 时对重放终态返回 `[]` 并记新 code `HUB_DEGRADE_DUPLICATE_TERMINAL_SKIPPED`，对终态之后的**内容**帧仍抽 `HUB_SSE_LATE_EVENT`；`claude-hub.py` 流式 except 块在写 error 事件前加 `bridge.stopped` 守卫，不再追加第二个终态。新测试 `test_trailing_done_keeps_the_success_turn_and_its_usage` 实证 `terminal=complete` + usage 落盘（`in=11`）+ 下游无 `event: error`。

---

## R2 ✅ 2026-08-17 `{"error":…}` → `[DONE]` 顺序销毁上游真因 【已复现】

**症状**：内容 delta → `data: {"error":{"type":"rate_limit_error","message":"quota exhausted for org","code":"insufficient_quota"}}` → `data: [DONE]`。下游收到**两个** `event: error`；errors 行的 code 是 `HUB_SSE_LATE_EVENT`，上游真因 `insufficient_quota` / "quota exhausted for org" 被彻底销毁。

**根因**：与 R1 同一处。error 帧置 `stopped` 后，`[DONE]` 抛的 `HUB_SSE_LATE_EVENT` 先赢，覆盖了刚落地的 `error_terminal` 真因捕获分支（`claude-hub.py` 约 `:2977-2988`）——那条分支在这个最常见顺序下**永远走不到**。

**这是回归**：直接抹掉 `90ad994`（"forward sanitized upstream error reasons instead of dropping them"）的全部意图。

**修法**：随 R1 一并解决。

**验收合同**：

1. `{"error":…}` → `[DONE]` 回合，errors 行保留上游 `code` 与脱敏后 `message`，**不是** `HUB_SSE_LATE_EVENT`。
2. 下游只收到**一个** `event: error`。
3. 全量绿。

**修复记录（2026-08-17，未提交）**：随 R1 同一处修复解决。新测试 `test_trailing_done_after_error_frame_keeps_the_upstream_reason` 实证 errors 行保留 `quota exhausted for org`、下游 `event: error` 计数为 1、usage 不落盘。

**一处 pinned invariant 重定向**（照 `90ad994` 的先例：同轮补替代断言 + 显式声明行为变更）：`tests/test_protocol_sse_invariants.py` 的 `test_terminal_late_duplicate_and_usage_regression_fail_closed` 改名为 `..._contract`——它对"终态后**内容**帧 → `HUB_SSE_LATE_EVENT`"和"usage 倒退 → `HUB_SSE_USAGE_REGRESSION`"两条 fail-closed 断言原样保留，只把"重复 `response.completed` → `HUB_SSE_DUPLICATE_CONFLICT`"改成宽容 + 记码，并在测试内注明变更日期与原因。

---

## R3 ✅ 2026-08-17 count_tokens 预检使降级计数翻倍 【已复现】

**症状**：`claude1 usage` 的"协议降级"段计数是真实值的 2 倍。

**根因**：native 非流成功分支的 `record_usage(..., degrade_codes=protocol_warning_codes)`（`claude-hub.py` 约 `:3631-3647`）落在 `elif not streamed and upstream.status == 200:` 里，**count_tokens 也满足这个条件**。Claude Code 每个真实回合前都发一次 `POST /v1/messages/count_tokens`，同一个请求侧降级（如 `HUB_DEGRADE_SYSTEM_ROLE_PROMOTED`）被记两行，而 `_degrade_counts` 每行计一次。

**两处自相矛盾**：

- 同一个 `protocol_warning_codes` 变量，在 exhaustion 分支写的是 `protocol_warning_codes if not is_count else ()`（约 `:3420-3422`，主动抑制），在 usage 分支不抑制。
- `degrade-inventory.md` 明写"count_tokens 的 validation-only 和本地估算不是 message turn，P0 范围外"。

现有测试抓不到，因为 T0.1 验收合同只要求"计数 ≥ 1"。

**待决**：抑制 count 路径的 `deg`，还是干脆不为 count_tokens 写 usage 行——见文末"存疑待决"。两种都要求口径统一（本地估算分支目前也丢弃 plan）。

**验收合同**：

1. 一个真实回合 + 一次 count_tokens 预检，同一 code 在 `claude1 usage` 里计 **1** 次。
2. 三条 count 路径（native 转发 / 本地估算 / exhaustion）的落盘口径一致，并在 `degrade-inventory.md` 写明。
3. 全量绿。

**审查未记的第二处污染**：`claude1_usage_report.py` 的 `请求数` 直接取 `len(rows)`，所以 count 空壳行同样让**请求数翻倍**。这改变了修法取舍——只抑制 `deg` 的话请求数仍翻倍，缺陷只修一半。

**修复记录（2026-08-17，未提交）**：用户拍板修法「count 路径不写 usage 行」。事实依据：count 响应体是 `{"input_tokens": N}` 而非 `usage` 对象；流式方言即使附带 `message_start.usage`，也仍是 pre-flight probe，不代表真实 message turn。另两条 count 路径（本地估算 / 上游 404-405-501 回退）本就直接 `return`、从不调 `record_usage`，所以这是向既有口径收敛而非新增例外。

改动：native 非流 200 分支加 `and not is_count` 守卫；流式 usage 结算分支同样加 `and not is_count`，因此上游把 count 结果错误地用 SSE 返回时仍透明转发，但不写 usage 行。
测试：`test_count_tokens_preflight_is_not_accounted_as_a_turn`（真实回合 + JSON 预检 → usage 一行、`_degrade_counts` 计 1）、`test_streaming_count_tokens_is_not_accounted_as_a_turn`（SSE 方言照常转发但不写 usage）、`test_count_tokens_failure_still_carries_its_degrade_codes`、`test_count_tokens_route_exhaustion_keeps_the_request_degrade`（钉住移除抑制的决定）。原三条均经变异验证；SSE 回归测试直接复现 reviewer 找到的漏口。

**第二轮复审的补测（2026-08-17，未提交）**：`not is_count` 只加在 usage 分支上，而流式 count 的**错误终态**分支此前零测试。新增 `test_streaming_count_tokens_error_terminal_is_still_journaled`：SSE count 探针以 `event: error` 收尾 → 字节照常逐帧转发、errors 落一行 `UpstreamSSEError` 并带 `deg`、usage 不落盘。变异验证：把 `not is_count` 上提到两个分支共享的条件上，errors 文件根本不存在——探针失败连一行归因都没有。无实现改动，纯补测。

---

## R4 ✅ 2026-08-17 账号池耗尽漏传 `degrade_codes` 【已复现】

**症状**：请求已降级 + 路由目标账号池 cooldown/disabled → `phase="route"` 的 errors 行没有 `deg`，降级查不到。

**根因**：两处 `RouteTargetExhausted(...)` 构造（`claude-hub.py` 约 `:3095-3100` 与 `:3698-3703`）只传了 status / retry_after / alias / evidence_message，**没传 `degrade_codes`**，而 `request_warning_codes`（前者）/ `protocol_warning_codes`（后者）都在作用域内。`_route_target_exhausted` 那条路径已经传了，所以这是不对称的漏接。

这是四路审查里**唯一确凿的漏接线**——其余 15 个 journal 调用点全部接了。

**验收合同**：

1. 两处各有测试：请求带降级 + 账号池耗尽 → errors 行含 `deg`。
2. 全量绿。

**修复记录（2026-08-17，未提交）**：两处 `RouteTargetExhausted(...)` 各补 `degrade_codes=`（transformed 传 `request_warning_codes`，native 传 `protocol_warning_codes`）。测试 `test_transformed_pool_exhaustion_keeps_the_request_degrade`、`test_native_pool_exhaustion_keeps_the_request_degrade`，均经变异验证（撤掉传参即红）。为让池 fixture 也能驱动 transformed handler，`_write_pool_db` 加了可选 `meta` 参数。

---

## R5 ✅ 2026-08-17 凭证脱敏漏 4 类形状 【已复现】

**症状**：`sanitize_error_text`（`claude1_protocol.py`）实测输出：

```text
LEAK  'rejected Authorization=[redacted] dXNlcjpzdXBlcnNlY3JldA=='   ← Basic base64 存活
LEAK  'cookie session=SUPERSECRETSESSIONVALUE expired'                ← cookie 不在 alternation
LEAK  'invalid token eyJhbGciOiJIUzI1NiJ9.eyJrIjoiU0VDUkVU…SIGNATURE' ← 空格分隔，正则强制要 [=:]
LEAK  'x-api-key header abc123def456ghi789 is revoked'                ← 裸值
ok    'Authorization=[redacted] [redacted-token] rejected'            ← Bearer 有特判
ok    'see [redacted-url] for detail'                                 ← URL 覆盖到了
```

`Basic` 的根因是 `\S+` 只吃掉了 `Basic` 这个词本身，base64 载荷留在原地。

**为什么是阻塞级**：踩的是 AGENTS.md 第 23 行的凭证 fail-closed 边界，且**"sanitizer 覆盖够了"正是 `90ad994` 敢转发上游 error detail 的前提**——前提不成立，整个放宽的安全论证就不成立。传播面三处：下游 error body、流式终态 error 事件、`*-errors.jsonl`（0600 但明文落盘）。

**修法**：alternation 补 `cookie|set-cookie|session`；加 `Basic\s+\S+` 特判（同 `Bearer`）；`[=:]` 放宽到 `[=:\s]`，或另加 JWT 形状 `eyJ[A-Za-z0-9_-]{10,}\.`。

**验收合同**：

1. 上列 4 条 LEAK 输入全部脱敏，2 条 ok 输入行为不变。
2. 每类形状一条测试，断言原始凭证子串不出现在输出中。
3. 全量绿。

**修复记录（2026-08-17，未提交）**：四类全部堵住，实测输出见下。

**一处修法偏离**：卡里建议的「`[=:]` 放宽到 `[=:\s]`」没有采用——它解决不了第 4 类（`x-api-key header abc123…` 里关键词后跟的是 `header`，放宽只会把 `header` 这个词脱敏、真凭证照样留下），却会把 `invalid token format`、`api key for model claude-opus-4-20250514` 这类正常措辞一并吃掉，踩 AGENTS.md 第 17 行「不裁剪语义」。改为：

- `Bearer` 特判扩成 `(Bearer|Basic)`，保留原词、只换掉后面的载荷；
- alternation 补 `set-cookie|cookie|session`；
- 新增 JWT 形状 `eyJ…\.…`，不依赖关键词（上游常写成裸的 `invalid token <jwt>`）；
- 新增 `_ERROR_TEXT_NEARBY_VALUE`：关键词后**至多跳 1 个普通词**再取值，且该值须经 `_is_credential_shaped`（≥12 字符且含数字或结构字符）判定。跳 1 个词足够覆盖 `x-api-key header <value>`，跳 2 个就会吞掉 `api key for model <model-id>`。

实测（`sanitize_error_text` 直接调用）：

```text
OK  Basic base64       -> rejected Authorization=[redacted] [redacted-token]
OK  cookie/session     -> cookie session=[redacted] expired
OK  bare JWT           -> invalid token [redacted-token]
OK  bare header value  -> x-api-key header [redacted] is revoked
OK  'invalid token format'                                → 原样
OK  'api key for model claude-opus-4-20250514 is invalid'  → 原样
OK  'your token specification is malformed'                → 原样
OK  'session expired, please retry'                        → 原样
```

测试：`test_sanitizer_redacts_credential_shapes_without_an_assignment`（4 类各一个 subTest，断言原始凭证子串不出现）、`test_sanitizer_keeps_prose_that_merely_names_a_credential`（防过度脱敏）。既有 `test_sanitizer_redacts_assignment_and_key_shapes` 无回归。凭证字面量按仓库规矩拼接构造，secret_guard 通过。

**第二轮复审的两处补修（2026-08-17，未提交）**：上一轮的修法自身留了两个口子，均已堵上。

1. **引号形状**：`(['\"])[^'\"\r\n]*\2` 的同引号 backreference 只认工整配对。闭合引号换成另一种（relay 拼接）、引号被反斜杠转义（JSON 编码的错误体）、或 512 字符截断把闭合引号切掉，三种情况全部匹配失败——而失败不是无害的，值会落到 `\S+` 赋值规则，只脱敏到第一个空格，其余原样留在文本里。实测 `token='SUPER SECRET VALUE0123"` → `token=[redacted] SECRET VALUE0123"`。改为共用 `_ERROR_TEXT_QUOTED_VALUE`：允许前导反斜杠、闭合引号可为任一种、无闭合引号时收到行尾。值体本身跨不过引号，所以宽容闭合仍停在最近的那个引号上。
2. **header 值过度脱敏**：`_ERROR_TEXT_HEADER_VALUE` 是唯一不过形状判定的规则，于是 `authorization header verification failed` → `authorization header [redacted] failed`，`cookie authentication is not configured`、`api key authentication unavailable` 同样被吃掉——踩的正是 R5 自己写下的「不裁剪语义」边界。新增 `_is_opaque_header_value`：带数字或结构字符仍走 `_is_credential_shaped`；纯字母值改看大小写形态——散文是全小写或首字母大写，凭证是全大写或 camelCase。全大写的普通词会被脱敏，这是该边界上主动选的 fail-closed 侧，已写进 docstring。

新测试 `test_sanitizer_redacts_quoted_credentials_with_broken_quoting`（8 个 subTest，断言凭证**尾段**而非整串不出现，否则部分脱敏也会通过），`test_sanitizer_keeps_prose_that_merely_names_a_credential` 增补 5 条 header 措辞。两条变异验证：退回 backreference → 引号测试红、散文测试仍绿；撤掉形状判定 → 散文测试红、泄漏测试仍绿（两条测试确实在测不同的东西，且形状判定没有削弱泄漏覆盖）。

---

## R6 ✅ 2026-08-17 journal 读侧一个坏字节静默丢数据 【已复现】

**症状**：实测 `_load_error_rows`（`claude-hub.py` 约 `:4209`），journal 中间一行含孤立 `0xE9`：

```text
小文件(<8KB):  写入 3 行   → rows=0    skipped=0
大文件(>8KB):  写入 202 行 → rows=140  skipped=0
```

小文件场景 `claude-hub errors` 直接印 "no error records yet"——**满载的 journal 被报成从没出过错**。大文件场景静默截断 62 行。两种情况 `skipped` 都是 **0**，连"有行被跳过"都不提示。

**根因**：约 `:4239` 用 `os.fdopen(fd, encoding="utf-8")`（strict），约 `:4241` 迭代时抛 `UnicodeDecodeError`（`UnicodeError` 子类），被约 `:4256` 的兜底 `except (OSError, UnicodeError): pass` 吞掉。`TextIOWrapper` 按 8KB 块解码，所以坏字节所在块的起点之后全丢。

**可达性**：写侧用 `ensure_ascii=False`，中文限额原因就是裸多字节 UTF-8；写入被撕裂（ENOSPC / SIGKILL / 两个 hub 共用一个 log 路径）即命中。

**附带**：约 `:4247` 的 `except (json.JSONDecodeError, UnicodeError)` 是**死分支**——`line` 已是 `str`，`json.loads` 不会抛 `UnicodeError`。它让人误以为解码已被处理。

**修法**：改 `errors="replace"`，或按 bytes 逐行解码让坏行计入 `skipped`；删掉死分支里的 `UnicodeError`。

**验收合同**：

1. 含非 UTF-8 字节的 journal：坏行前后的合法行**全部**读出，坏行计入 `skipped` 并在 CLI 可见。
2. 小文件与大文件（>8KB）两个场景各有测试——现有畸形样本用的是 `"not-json"`，那是合法 UTF-8，恰好绕过解码分支。
3. 全量绿。

**修复记录（2026-08-17，未提交）**：`os.fdopen(..., errors="replace")`，并删掉 `json.loads` 那处死的 `UnicodeError`（外层 `except (OSError, UnicodeError)` 保留——它兜的是 `os.open` 一侧）。

**合同第 1 条按实测细化**：坏字节的落点决定结果，"坏行一律计入 skipped"并不成立，也不该成立。

| 坏字节落点 | 结果 | 依据 |
|---|---|---|
| 字符串值内（如中文被撕裂） | 变 U+FFFD 后**仍是合法 JSON**，整行照常读出，`skipped=0` | AGENTS.md 第 12 行"有损但能用 → 放行" |
| 撕裂了 JSON 结构（写入被截断） | 替换后仍语法错误 → 计入 `skipped`，CLI 显示 | 合同原意 |

两种落点下，**坏行前后的合法行都全部读出**——这才是 R6 的核心危害所在。

实测对比（修复前 → 修复后）：

```text
小文件(<8KB)  3 行   rows=0   skipped=0  →  rows=3   skipped=0（坏行经 U+FFFD 存活）
大文件(>8KB)  202 行 rows=0   skipped=0  →  rows=202 skipped=0
撕裂行 3 行            rows=0   skipped=0  →  rows=2   skipped=1（CLI 可见）
```

修复前大文件实测是 0 而非卡里记的 140，因为坏行位置不同导致撞上的解码块不同；同一根因。

测试：`test_error_journal_reads_past_non_utf8_bytes`（small / multi-block 两个 subTest，后者断言文件 >8192 字节以确保跨解码块）、`test_error_journal_counts_a_torn_row_as_skipped`（含 CLI "1 stale record(s) skipped" 断言）。变异验证：改回严格解码后三条断言全红，且精确复现"满载 journal 报成从没出过错"。

**第二轮复审的补注（2026-08-17，未提交）**：外层 `except (OSError, UnicodeError)` 在删掉内层死分支后没有任何说明，读起来就是同一处死代码。补注写明两者不是一回事：这里的 `UnicodeError` 说的是**路径**（不可编码的 journal 路径会从 `lstat`/`open` 抛出），而**内容**解码在 `errors="replace"` 之后已经不可能再抛——这正是内层不再列 `UnicodeError` 的原因。纯注释改动，无行为变化。

---

## R7 测试欠账

变异测试实测（59 个变异 / 47 杀 / 12 存活，kill rate 80%）【报告】。主干可信：14 处 `degrade_codes=` 落盘点里 12 处变异被杀且**红的是不同测试**；双记账与伪造 `message_stop` 两条反向测试真实有效；audit hook 扫真实 `~/.cc-switch` 访问 = 0 次；0 skip。

缺口按重要度：

| # | 缺口 | 证据 |
|---|---|---|
| 1 | `_degrade_counts`（`claude1_usage_report.py`）三个语义全裸：每回合去重、多回合聚合、排序方向的变异**全部存活**。docstring 承诺的 "Count each degradation at most once per turn" 是空话。根因是 fixture 只有 1 行 1 个 code | 【报告】docstring 与实现已读码确认 |
| 2 | `tests/test_claude_hub.py` 约 `:617/:651` 的畸形样本 `{"bad": "HUB_DEGRADE_NOT_A_LIST"}` 把可识别 token 放在 dict **value** 位；放宽守卫成 `(list, dict)` 后渲染出 key `deg=bad`，`assertNotIn` 照样通过 | 【已复现】 |
| 3 | usage 侧 `deg` 去重无测试（`record_usage` 里的 `dict.fromkeys`）。errors 侧有 `test_error_journal_records_unique_degrade_codes` 压住，usage 侧只传了单个 code | 【报告】 |
| 4 | ~~native route-exhaustion 落 `deg` 零测试，含 `is_count` 守卫~~ **已闭环（2026-08-17）**：守卫随 R3 移除，补 `test_count_tokens_route_exhaustion_keeps_the_request_degrade` + R4 两条池耗尽测试 | 【报告】→ 已修 |
| 5 | `_format_protocol_warnings` 三个接线点还原成 `','.join(...)` 全部存活。这函数的存在理由是防日志按回合增长 | 【报告】 |
| 6 | T0.5 对账只是本仓库文档里的手工 shell 片段，不是 `tests/` 断言。加第 48 个 code 不会红，**盘点表当天静默过期**。5 行 unittest 可锁死。**2026-08-17 部分推进**：脚本本身补了逐行 code+行号校验（此前只数总量，改错行号发现不了；头部曾声称有此校验而实际没有）。但它**仍在文档里、不是 `tests/` 断言**，本项未闭环 | 【已复现】读 inventory 确认 |
| 7 | 4 个 code 在 `tests/` 命中数为 0：`HUB_DEGRADE_THINKING_TO_EFFORT`、`HUB_DEGRADE_ADAPTIVE_THINKING_TO_EFFORT`、`HUB_DEGRADE_TOOL_METADATA_DROPPED`、`HUB_DEGRADE_BATCH_TOOL_OMITTED`。删掉对应 `_record_lossy` 调用测试仍绿 | 【报告】 |
| 8 | 跨文件同名测试方法：`test_conflicting_cache_read_carriers_are_rejected` 同时存在于 `tests/test_claude1_protocol.py` 与 `tests/test_protocol_contract.py`。两者在不同类里都会跑（所以静态行数 49→比唯一名多 1），但是否覆盖同一行为、改一处会否漏掉另一处，未核实 | 【已复现】R1 修复时对账发现 |
| 9 | 测试计数异常未查明：R1 修复前三次全量运行都报 `Ran 660`，而静态 `def test_` 行数为 661；改协议层后变 661。当前静态定义与收集清单对账一致、无"写了但未被收集"的测试。若再现，先查是否有条件定义的测试方法 | 【已复现】现象成立，原因未明 |

不阻塞但值得知道的 mock 深度问题【报告】：`tests/test_launcher.py` 的 `_mock_hub_errors_renderer` 把 `spec_from_file_location` / `module_from_spec` 整个换掉，三条测试测的是"路径与 env 传对了"，真实 `cli_errors` 渲染器从未执行（真实渲染由 `tests/test_claude_hub.py` 独立覆盖，两侧靠字面量 `"deg"` 各自钉住）。

---

## R8 文档与清理

- `degrade-inventory.md` 证据列 5 处失准【报告，附反证】：`THINKING_TO_EFFORT` 引用的测试不断言 `warning_codes`；`SYSTEM_METADATA_DROPPED` 拿响应头 `x-hub-protocol-warnings` 当落盘证据（而 T0.6 DoD 明写"不看响应头"）；response-snapshot 未映射字段那行引用了不喂 snapshot 的测试；logprobs 那行断言与邻居共享同一字符串；`openai_responses` 错误 detail 那行引用了显式 `openai_chat` 的测试——**而真正能钉住它的测试存在却没被引用**（`tests/test_protocol_sse_invariants.py` 里用 `openai_responses` + `response.failed` 断言了该 code），属引用失准而非覆盖缺口。
- 同文件 3 处免责措辞从"无专属 Hub E2E 测试"下调为"全无测试覆盖，仅源码路径"（对应 R7 第 7 项的三个 code）。
- README 的"截断到 512 **字节**"应为 512 **字符**（实现是 `text.strip()[:512]`）。配合 `ensure_ascii=False`，512 字符中文约 1.5KB，按 README 推算 5MB 预算会差 3 倍。【报告】
- README 的"只存脱敏后的 code / message 与渠道、模型"不成立——`model` 既未脱敏也未截长（见"存疑待决"）。【报告】
- `claude-hub.py` 约 `:3335` 续行 f-string 缩进比兄弟行少 4 空格，语法合法（隐式拼接）但是手改残留。【已复现】
- `_format_protocol_warnings` docstring 说原始 `CODE@path` 列表"grows quadratically"不准确——`warning_details` 已按 `(code, path)` 去重，增长是"不同 path 数"的线性；`x{count}` 数的也是不同 path 数而非 occurrence 数。【报告】
- `0270718` 的两个已知 gap（`cli_check` 原样探测、`_transformed_headers` 不发 beta 头）只写在 commit message 里，`docs/` 零命中。AGENTS.md 第 30 行要求记 `docs/`——commit message 不是可检索的缺陷记录。【报告】
- `HUB_USAGE_PROVENANCE_UNAVAILABLE` 经 `bridge.warning_codes` 混进 `deg`，被打印在"协议降级"段下，但它不是 `HUB_DEGRADE_*` 前缀，且 inventory 明确把它排除在 47 行之外。口径需对齐。【报告】
- `~/.cc-switch/logs/claude-hub-usage.jsonl` 有一行 `"model":"fixture-model"`（ts ≈ 2026-08-11），说明隔离在过去泄漏过一次；当前套件复现不出。这行会污染真实 usage 统计，建议手工删。【报告】

---

## R9 提交范围拆分

当前工作树混进三块与 T0.1–T0.5 无关的改动【报告】，按队列规则"一张卡 = 一次专注工作闭环"应拆开：

1. provider snapshot 单飞缓存重构。
2. `[1m]` 对 OpenAI 渠道的转发行为变更——部分回退 `0270718`，且 docstring 理由（"upstreams that recognise it still receive the 1M intent"）**无证据来源**。AGENTS.md 第 27 行要求偏离写明"防什么 / 超在哪"，这条缺证据；不认识该后缀的 OpenAI 兼容上游会拿到一个不存在的 model id。
3. 流失败改为下发 `event: error` 而非 abort——**R1 的爆炸半径就来自这块**。

---

## 存疑待决（需要决定，不是纯技术问题）

- ~~**R3 的修法**：抑制 count 路径的 `deg`，还是不为 count_tokens 写 usage 行。~~ **已决（2026-08-17，用户拍板）**：不为 count_tokens 写 usage 行，三条 count 路径口径统一。连带确立「usage 计数 / errors 归因」两套口径，见 R3 卡与 `degrade-inventory.md` 第 2 节。
- **错误终态回合是否该记 usage**【报告】：native 场景 `message_start` 已报 `input_tokens:900`（上游一定计费），当前错误终态回合完全不写 usage → 成本护栏漏账。"失败回合进 errors"不等于"计费证据必须丢弃"。改动前这里是记 usage 的，属新引入的欠记账。
- **`model` 字段的约束**【报告】：`message` 截 512、`code` 截 64，`model` 无任何约束，而它在 `alias,model` 选择器与 `route_unknown_to_default` 渠道下是客户端原始字符串。两个失败场景：100KB 模型名打 ~50 个失败请求即填满 5MB 轮转掉全部真实记录（证据驱逐）；`model="evil\x1b[2K\rSPOOFED"` 经 JSON 往返存活并被 `print()` 原样吐到终端（ANSI 擦行改写）。建议在写侧截长 + 剥 `[\x00-\x1f\x7f]`。
- **上游错误文本可能夹带用户 payload**【报告】：hub 从不复制 payload，但 OpenAI 兼容网关的校验错误常回显违规输入，最多 512 字符用户文本会经上游措辞落到磁盘。严格说没违反"绝不写 payload"，但应显式决策并在 README 写明，而非留作隐含行为。
- **0600 告警的承诺**【报告】：文案说"hub 会在下次错误写入时收紧到 0600"，但 `fchmod` 只在 `_open_rotating_jsonl` 里执行，而文件句柄是进程级缓存 → 长驻 hub 永不重新收紧。要么周期性 `fchmod`，要么改文案为"重启 hub 后收紧"。
- **多目标路由的证据盲区**【报告】：`ROUTE_FAILOVER_STATUSES` 恰是 401/403/429——用户最需要归因的三个状态码不走 `phase="response"`，只有最后一个目标经 `phase="route"` 落盘，且那行连 `model` 都没记。"我这 5 个渠道到底哪个限额到了"从 `claude-hub errors` 答不出来。
- **hub 侧拒绝一律不进 journal**【报告】：`protocol_request_error`、502 ambiguous representation headers、502 unsupported SSE encoding、`RouteError`、count 路径的 404/405/501 降级估算全部无 `record_error`，而文档把 journal 描述成事后唯一读取入口。
- **journal 永久失效完全静默**【报告】：全吞异常本身正确（不能搞挂转发），但首次失败没有一条 hub log。`errors_path()` 被指成 symlink → `O_NOFOLLOW` → 每次请求 `ELOOP`，整个进程生命周期 journal 失效且无人知晓。
- **路由切换前下载至多 64MB 错误体**【报告】：exhausted 路径的 `_read_decoded_upstream_body` 用了默认 `limit`。5 目标全 429 且带大 HTML 错误页 → 串行读+解压 5×(至多 64MB)。helper 本身收 `limit` 参数，传几 KB 即可（证据只留 512 字符）。

---

## 接手指南

**一句话**：四路代码审查已完成并固化在本文，**R1–R6 全部已修**，T0.6 端到端验收的前置已清空；剩下 R7（测试欠账）、R8（文档）、R9（提交范围拆分）不阻塞 T0.6。

**全部工作都在未提交的工作树里**，git 历史里没有任何本轮内容。`git status --short` 当前是 12 个 `M` + 8 个 `??`。

**已闭环**：

- T0.1–T0.5 的 degrade 落盘主体。审查确认接线主干无问题：15 个 journal 调用点全部接了 `degrade_codes`，流式成功/失败两分支各自重算 `request + bridge.runtime` 合集，去重语义读写侧一致，断流不补 `message_stop` 有测试钉住。
- R1/R2（重放终态）：含新 code、hub 侧 `bridge.stopped` 守卫、2 条回归测试、inventory 全表同步。
- R3（count 记账）/ R4（池耗尽归因）/ R5（凭证脱敏）/ R6（journal 解码）：9 条新测试，全部经变异验证。inventory 行号二次重算 + 自查脚本补逐行校验。

**本轮做的三个判断**，接手时值得知道（都写在对应卡里）：

1. R3 顺带移除了 `_route_target_exhausted` 的 `is_count` 抑制，确立「usage 的 `deg` 进计数器、errors 的 `deg` 只归因」两套口径。
2. R5 没采纳卡里建议的 `[=:\s]` 放宽——它解决不了第 4 类却会过度脱敏，改用「关键词 + 至多跳 1 词 + 高熵值」。
3. R6 的"坏行计入 skipped"按落点细化：值内坏字节经 U+FFFD 存活（有损但能用），撕裂结构才计 skipped。

**立刻能拿的活**，按顺序：

1. **T0.6** — 前置已清空，可以带着干净行为做端到端验收。
2. **R9** — 提交范围拆分 3 块；本轮又往工作树加了 R3–R6，拆分时一并考虑。
3. **R7**（测试欠账，第 4 项已闭环、第 6 项部分推进）、**R8**（文档 8 项）。

**三个陷阱**：

1. **行号一律以符号名为准。** 本文与 `degrade-inventory.md` 的行号都是快照。修 R1 时全表 47 行行号一次性全部过期——插入一个常量和一个方法就够了。
2. **改协议层必须同步 inventory**：occurrence 数、distinct 数、executable 数、第 4 节自查脚本里的三个 `assert`，以及**全表行号**。自查脚本现在含逐行行号校验（原本只数行数，改错行号发现不了）。
3. **证据分级不要混**：**【已复现】**可直接动手；**【报告】**是子代理主张，动手前自己验一遍，不要当成事实写进 commit message。

**不要重跑四路审查**——结论已固化在本文，重跑只会拿到同样的东西。

**验证**：

```bash
python3 -m unittest discover -s tests -p 'test_*.py'   # 当前 675 tests OK
python3 scripts/secret_guard.py --working-tree
git diff --check
```

inventory 自查脚本在 `degrade-inventory.md` 第 4 节。
