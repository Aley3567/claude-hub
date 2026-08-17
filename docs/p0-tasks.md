# P0 任务队列：观测出口（曳光弹式）

> 2026-08-16。本文是 `claude1-refactor-design.md` P0 期的可执行队列，是唯一合法的"当前该干什么"来源。
> 方法论沿用 `tracer-bullet-audit.md`：**先让一个 degrade code 打穿全链路（曳光弹），再沿链路逐段加宽**；每卡带编号验收合同；已验证事实与待办分写，不凭想象排任务。
>
> **队列规则**
> - 从顶部未完成的卡拿活，一张卡 = 一次专注工作能闭环的单位。
> - 卡自带全部上下文：目的、依据、锚点、验收合同。执行者不需要先读四份文档。
> - 做完一张：卡首行打 `✅ YYYY-MM-DD (commit)`，或直接删卡（git 归档）。不留"做了一半"的隐形状态——做一半的卡改写验收合同拆成两张。
> - 队列为空 = P0 完成，此时才允许回宪法展开 P1，新建 `p1-tasks.md`。
> - 行号是 2026-08-16 快照，会漂移；以符号名为准。
>
> **设计继承原则**（AGENTS.md 硬约束）：实现选择先对照 cc-switch 已验证做法（本机克隆 `~/Documents/Codex/2026-06-07/cc-switch`），偏离写理由；原创只投在它做不到的事：降级可观测、槽位路由、成本护栏。P0 本身就是第一个超越点——cc-switch 宽容但静默，降级不可查。
>
> **P0 期 DoD（宪法原文）**：任意一次降级发生后，用户能在 errors/usage 命令里查到，不靠响应头和截图。
>
> **全局约束**（AGENTS.md）：协议层零第三方依赖；改协议层同步补测试；journal 绝不写 payload；落盘绝不能搞挂转发主路径（异常静默）。

## 全链路（降级的生命周期）

```text
上游方言行为
  → 协议层记 code（claude1_protocol.py，47 处 HUB_DEGRADE_*）
  → 载体冒泡：plan.warning_codes（请求/非流响应/native）· bridge.warning_codes（流式运行时）
  → 回合收尾：record_usage（成功）/ record_error（失败）
  → JSONL journal（~/.cc-switch/logs/，0600，按大小轮转）
  → claude1 usage / errors 命令可查
```

## 已验证事实（2026-08-16 读码确认，不用重查）

- 47 处 code 全部在 `claude1_protocol.py`；不同 code 22+ 种，代表例：`HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED`（`claude1_protocol.py:2121`，请求带未知字段即确定性触发）。
- 载体已通到 hub 层四个点：请求 `claude-hub.py:2748`、非流响应 `:2853`、流式 `:2945`、native `:3263`。
- `record_usage`（`:388`）每回合调用，四个调用点 `:2862`/`:2958`/`:3526`/`:3545`；`record_error`（`:444`）仅失败回合。
- journal 纪律现成：0600、O_NOFOLLOW、轮转、异常静默（`_open_usage_log` / `_open_errors_log`）。
- 现有出口仅响应头 `x-hub-protocol-warnings`（`:2836`/`:2884`/`:2925`/`:3425`）+ hub.log——**这就是要修的断点**。
- `claude1_usage_report.py` 读当前 + 一个轮转文件；`cli_errors` 渲染测试模式见 `tests/test_claude_hub.py:469-530`。

---

## T0.1 曳光弹：一个 code 打穿全链路

**弹头**：`HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED`。选它的理由：请求侧、确定性触发（请求体塞一个未知字段即可）、走 openai_chat 非流路径、不依赖上游任何配合。

**做法**（最小闭环，schema 被真实遍历验证后再定型）：
1. `record_usage` 接受 degrade codes，成功回合的行加 `deg: [...]` 字段；无降级不写字段；旧格式行向后兼容。
2. 接通一条路径：`claude-hub.py:2748`（请求）+ `:2853`（非流响应）的 warning_codes → `:2862` 的 `record_usage`。
3. `claude1_usage_report.py` 加最朴素的 degrade 计数段（按 code 计数即可，暂不做渠道/模型交叉）。

**验收合同**：
1. 含未知字段的请求走非流 openai_chat 成功回合，usage JSONL 对应行含 `deg: ["HUB_DEGRADE_UNKNOWN_REQUEST_FIELD_DROPPED"]`。
2. `claude1 usage` 输出可见该 code 计数 ≥ 1。
3. 无降级回合的行不含 `deg` 键；旧格式行读取不炸（向后兼容测试）。
4. 落盘异常静默测试（journal 不可写时转发主路径不受影响）。
5. 全量 `python3 -m unittest discover -s tests -p 'test_*.py'` 绿。

**本卡明确不做**：流式路径、record_error、人话翻译、schema 终稿、其余 46 处的核对。

---

## T0.2 失败分支：record_error 带 deg

**沿同一链路的失败岔路**：非流回合失败时，已产生的 warnings 不能随回合消失。

**做法**：`record_error` 接受并落 `deg` 字段；非流失败回合的 warning_codes 传入对应 `record_error` 调用点；`cli_errors` 渲染错误行的 `deg`。

**验收合同**：
1. 非流回合"先降级后失败"（如未知字段被丢弃 + 上游 500），errors JSONL 该行同时有错误字段和 `deg`。
2. `claude-hub errors` 渲染该行的 degrade codes。
3. 仿 `test_cli_errors_renders_reasons_and_skips_stale_debug_rows` 模式的渲染测试通过；全量绿。

---

## T0.3 加宽：转译流式路径

**做法**：`bridge.warning_codes`（`:2945`，回合末收集）→ `record_usage`（`:2958`）。**断流回合是易漏点**：流中途失败时，bridge 已收集的 warnings 随 `record_error` 落盘，不随 transport abort 丢弃。合成流（上游回非流、客户端要 SSE）与非流同组 warnings，一并覆盖。

**验收合同**：
1. 转译流式成功回合（请求带未知字段触发同一弹头 code）落 `deg`。
2. 断流回合（上游 SSE 中途断开）errors 行带已收集的 `deg`。
3. 合成流回合落 `deg`。
4. 三条路径各有测试；全量绿。

---

## T0.4 加宽：native 透传路径

**做法**：`prepared.plan.warning_codes`（`:3263`）→ `record_usage`（`:3526`/`:3545`，流式/非流两个调用点）。native 路径的降级来源主要是请求 normalization（如 system-role 提升），测试用对应触发方式。

**验收合同**：
1. native 非流与流式成功回合各落 `deg`。
2. 两调用点各有测试；全量绿。

---

## T0.5 收尾对账：47 处全盘点

**曳光弹已通、链路已宽之后**，盘点从"设计输入"变成"实证审计"：逐处核对 47 个 code 是否真的会落盘，产出 `docs/degrade-inventory.md`。

**做法**：列：code | 位置 | 触发条件 | 冒泡载体 | 落盘状态（已落盘 / 到不了 hub 层+原因）。到不了的逐个补接通，或写明"不到 hub 层"的正当理由（如 strict 模式专属路径）。

**验收合同**：
1. 表行数与 `grep -c "HUB_DEGRADE_" claude1_protocol.py` 对账一致（当前 47）。
2. 每行落盘状态有测试或代码路径佐证；"已落盘"行数统计写出来。
3. 全量绿。

---

## T0.6 P0 验收（宪法 DoD 端到端）

**前置**：T0.1–T0.5 全部 ✅，且 `review-findings-2026-08-17.md` 的 R1–R6 已清。**R1–R6 已于 2026-08-17 全部修复并附验证**（675 tests OK · inventory drift=0 · secret_guard 通过），前置已清空。

四路审查（2026-08-17）证明 T0.1–T0.5 的**接线主干是对的**（15 个 journal 调用点无遗漏、流式两分支各自重算 warnings 合集、去重语义读写侧一致），但带着 R1/R2（尾随 `[DONE]` 使成功回合被报错、上游真因被 `HUB_SSE_LATE_EVENT` 销毁）或 R3（count_tokens 预检使降级计数翻倍）做端到端验收，记下的是带缺陷的基线——所以 T0.1–T0.5 暂不打 ✅。

**验收前需要知道的两处口径变化**（R3 引入，见 `review-findings-2026-08-17.md` R3 卡）：

- count_tokens 预检**不再写 usage 行**，所以 `claude1 usage` 的请求数与降级计数就是真实回合数，不再翻倍。
- errors 行的 `deg` 是单次失败的归因、不进任何计数器；usage 行的 `deg` 才被按回合计数。挑代表 code 时两侧分别验。

**做法**：从 `degrade-inventory.md` 挑覆盖全部冒泡通道（请求/非流响应/流式运行时/native）和成功/失败两分支的代表 code 各至少 1 个，本地真实触发，逐一用 `usage` / `errors` 命令查到——全程不看响应头、不看 hub.log。

**验收合同**：
1. 端到端验收记录附在本文末尾（日期 + code 列表 + 查询命令输出摘要）。
2. 全量测试绿。
3. 回 `claude1-refactor-design.md` P0 节标注完成日期。

---

## 验收记录

（空，等 T0.6 填写）
