# claude1 重构设计

> 2026-08-16。本文是 claude1 重构的宪法：所有后续代码改动对照本文与 `AGENTS.md` 执行。
> 依据文档：[cc-switch 实现调研](cc-switch-implementation-research.md)、[协议层基线与薄弱点](claude1-protocol-baseline-2026-08-16.md)、[产品定义](product-definition.md)。

## 0. 一句话

把系统从"Anthropic 规范的守门员"重构为**多渠道路由 + 按渠道挂方言适配的会话交付管道**。

与 cc-switch 的关系写死在第一句：**共生，不是替代**。cc-switch 管渠道、凭证、预设、GUI——它的强项，本仓库不重复造；本仓库只做 cc-switch 架构上做不到的事：多模型槽位同时在线、会话内按模型路由。

## 1. 为什么重构（问题陈述）

1. **fail-closed 过头**：协议层历史上对上游方言（流中途换 id、新增字段、自定义 SSE 事件、tool 参数非 object）一律拒绝，上游多样性直接变成用户可见故障。详见基线文档 10 个薄弱点。
2. **三条启动路径行为不一致**:launcher 直连（无协议层）/ 临时协议桥（短命）/ 常驻 hub（可选）——同一 provider 走不同路径，协议处理、错误整形、usage 记录能力都不同（例：system-role normalization 只在 bridge 路径生效）。
3. **降级观测断裂**：协议层 47 处 `HUB_DEGRADE_*` 记录的出口只有响应头（`x-hub-protocol-warnings`）和 hub.log;`record_error` 只在失败回合写持久 journal，成功回合的降级永不落盘，临时桥日志随会话销毁。**降级实际不可见**——宽容化之前必须先修这个出口，否则宽容等于吞错。
4. **测试自证**:639 个测试断言的是自己想象的上游行为，没有真实流量语料反哺机制（`scripts/observe-claude1.sh` 只记会话成败类别，不录协议流量）。
5. **方言知识无处沉淀**：没有 preset/适配数据层，供应商方言只能散落代码分支或强行按规范假设。
6. **韧性薄**:failover 只认 401/403/429 且仅在响应开始前；超时单旋钮（`claude-hub.py:2764`,`connect=15, sock_read=600, total=None`)，无首字节/静默分级；无熔断器。

## 2. 底层定义重构（宪法）

### 2.1 一等公民是回合（turn)

系统的正确性单位不是"消息字段"，而是**回合**：一个回合的终态、usage、错误必须从上游真实到达下游；中间的一切字段形状都是可协商的。协议转换降级为"渠道的属性"——渠道只讲 OpenAI 方言就挂转换器，有原生 Anthropic 端点就裸连。

### 2.2 错误 = 交付失败，不是规范违反

- 上游行为怪异但回合完成 = 成功 + 降级记录。
- 回合未完成 = 失败 + 完整证据（状态码、上游错误语义、phase、渠道、异常类型）。
- 失败绝不伪装成成功：断流不补 `message_stop`，工具调用丢了不伪装 `completed`。
- 错误暴露的边界修正：**"原样"以下游可解析为限**——转译路径的错误体最终必须整形为 Anthropic error shape（语义保留、形状合规），只有 native 路径才字节级原样透传。

### 2.3 provider = endpoint + 凭证 + 格式 + 方言画像

provider 定义必须长出"方言画像"这一维，且是**数据不是代码分支**。画像草案字段：

- `api_format`:anthropic / openai_chat / openai_responses（沿用 CC Switch `meta.apiFormat` 优先级）
- `reasoning`:参数形式（`reasoning_effort` / `thinking` / `enable_thinking` / `reasoning_split`)、effort 值域映射、输出载体（`reasoning_content` / `reasoning_details`)
- `capabilities`:tools、vision、thinking、cache_control 支持情况——**failover 前必须用能力画像判断备胎是否接得住当前请求**，接不住就明确报错，绝不静默阉割
- `quirks`:已知怪癖（SSE 事件名方言、schema 洁癖、id 习惯）

### 2.4 模型槽位 = 一等路由单元

模型不是字符串，是"槽位 → 渠道 → 能力"的解析链。便宜/均衡/旗舰/长上下文四槽位同时绑不同渠道、会话内按模型路由，是本产品对 cc-switch 的核心差异化。自动分槽只是初始默认值，判断错了必须一秒钟可改、不造成不可逆代价。

### 2.5 宽容三档（与 AGENTS.md 呼应）

每段上游数据三选一：能无损转 → 转；有损但能用 → 放行 + `HUB_DEGRADE_*`（**默认档**)；会出安全或因果事故 → 才拒，且一句话说清防什么灾难。仍然 fail-closed 的边界：凭证、tool_use/tool_result 因果校验、本地鉴权与文件权限。

## 3. 目标架构

- **headless 核心**：网关只暴露 HTTP 接口；curses TUI、将来的网页管理台、桌面壳都是它的客户端。这条决定网页版成本，先定后动。
- **观测出口**:degrade code 进持久 journal 和 usage 统计，成功回合也落；warning code 配"人话翻译 + 建议动作"层。
- **韧性分层**：错误分类重试（超时/连接/5xx 可重试，4xx 不重试）；首字节/静默超时分级（参照 cc-switch 60s/120s)；熔断器只放常驻 hub（临时桥接受"无熔断"作为形态代价）。
- **语料机制**:observe 脚本升级为协议级录制（脱敏后的请求/响应 shape，不录对话内容），真实上游行为变成 golden fixtures——兼容性从自证变实证。
- **preset 数据层**：方言画像 + 渠道预设以数据形式沉淀，持续从 cc-switch 更新中吸收方言情报（共生红利）。
- **三条路径收敛**（待拍板）：让 direct native 也过协议层做 normalization（哪怕零转换），消除路径行为不一致；代价是动 launcher 的轻量定位。

## 4. 分阶段计划

每期完成定义（DoD）写在下面，达不到不进下一期。

### P0 观测出口（宽容化的前置条件）

- degrade code 进持久 journal（成功回合也落），usage 报表可统计降级频率。
- DoD：任意一次降级发生后，用户能在 errors/usage 命令里查到，不靠响应头和截图。

### P1 协议层宽容化 + 人话层

- 基线文档薄弱点 #1-#6、#8、#10 逐条改造：SSE identity 变化降级而非抛错、未知 wrapper 字段降级、未知 SSE 事件跳过、缺失 id 的合成策略统一并记码、tool 参数非 object 二次解析/包裹。
- warning code → 人话 + 建议动作的对照表。
- 测试先行：先改测试的默认预期（未知→放行+warning），再改实现。
- DoD：对照基线 10 个薄弱点逐条销号；新增降级全部有 journal 出口。

### P2 韧性分层

- 错误分类重试；首字节/静默超时分级；常驻 hub 熔断器。
- failover 接能力画像（2.3)，备胎接不住就明说。
- 并发压测：账号池多会话挤兑公平性、单进程多槽位吞吐，出数据。
- DoD：压测报告落 docs/；failover 决策可解释（为什么切、为什么没切）。

### P3 语料与 preset 数据层

- observe 脚本协议级录制；golden fixtures 进测试套件。
- 方言画像 schema 落地，已有渠道逐一建档。
- DoD：每个已支持的渠道至少一条真实流量 fixture;schema 17 之类的外部变更能被测试提前暴露。

### P4 产品面

- 网页管理台（headless 核心的第一个 GUI 客户端）、模型自动识别分槽、成本护栏（预估 + 高价确认 + 预算熔断；**count_tokens 的 `len//4` 估算带 `x-hub-estimated`，绝不做熔断依据**)、Windows 适配、安装/迁移向导。
- DoD：产品定义文档"要建的"一栏逐条销号。

## 5. 明确不做

- 不替代 cc-switch（渠道管理、凭证、预设 GUI 继续用它）。
- 不自称生产级；定位是个人/小团队工具，README 写明。
- 跨机器配置同步不做（配置都在 `~/.cc-switch/` 下，用户可自行同步，文档给迁移指南）。
- 余额预警不做（多数上游无余额 API)；用量趋势推算可以做。
- 宽容化不碰安全边界（凭证、tool 因果、本地鉴权）。

## 6. 重构时对照的已核实事实

- `HUB_DEGRADE_*` 在 `claude1_protocol.py` 有 47 处；出口仅响应头（`claude-hub.py:2884` 附近）与 hub.log。
- 超时单旋钮：`claude-hub.py:2764`。
- failover:`_post_with_account_failover` 仅 401/403/429，仅响应开始前。
- CC Switch DB 只读支持 schema 13–16，版本检测 fail-closed（明确报错，不静默失败）——姿态正确，欠公开声明。
- usage 记录含 account / instance / source / method / exact 字段（对账基础已在）。
- `record_error` 绝不写 payload；但上游错误 message 可能携带敏感路径，脱敏规则需从"只防凭证"扩展到路径。
- `scripts/observe-claude1.sh` 只记成败类别，不录协议流量。
- 仓库现状：639 测试全绿，无 TODO/FIXME 注释传统。
