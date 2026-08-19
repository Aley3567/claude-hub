# AGENTS.md

给在本仓库干活的 agent。本文件只立规矩,不记项目状态——状态会过期,规则不会。细节都在 `docs/`,需要时现读原文,不要凭本文件的转述或自己的记忆行事。

## 总规则:默认放行,例外才拒

本仓库协议桥历史上 fail-closed 过头:上游流中途换 id、响应多个新字段、SSE 来个自定义事件、tool 参数不是 object,一律拒绝——上游的任何方言都变成用户可见的"id/response 不兼容"。

写或改协议代码时,对每一段上游数据只做三选一:

1. **能无损转** → 转。
2. **有损但能用** → 放行,记 `HUB_DEGRADE_*` warning,保证事后能在 errors/usage 里查到。这是默认档位。
3. **会出安全或因果事故** → 才拒,且必须能一句话说清防的是什么灾难。"我不认识这个字段"不是理由。

## 三条覆盖大多数场景的判断

- **错误**:上游的状态码和错误体尽量原样还给下游,不包装、不裁剪语义(脱敏只针对凭证);但失败绝不伪装成成功——断流不补 `message_stop`,工具调用丢了不伪装 `completed`。
- **id**:上游给了 message id / tool_use id 就用上游的;确实没有才本地生成,并记 `HUB_DEGRADE_SYNTHETIC_*`。
- **流**:收到什么转什么,未知事件跳过;终态只能来自上游的真实终态。

## 仍然 fail-closed 的边界

凭证(只读 CC Switch DB、不落盘、不进日志)、tool_use/tool_result 因果校验(防止错乱调用真实工具)、本地鉴权与 `0600` 文件权限。宽容只针对上游数据形状的多样性,不针对安全边界。

## 硬约束

- **继承优先于发明**:实现思路默认采用 cc-switch 已验证的做法(调研:`docs/cc-switch-implementation-research.md`;本机克隆:`~/Documents/Codex/2026-06-07/cc-switch`),动手前先对照对应实现。偏离必须一句话写明理由——防什么、或超在哪;原创只投在 cc-switch 架构上做不到的事(降级可观测、槽位路由、成本护栏)。
- 协议层与启动器零第三方依赖;`claude-hub.py` 依赖走 PEP 723 内联声明。
- 改协议层必须同步补测试。验证:`python3 -m unittest discover -s tests -p 'test_*.py'`。
- 仓库不留 TODO/FIXME 注释,缺陷要么修要么记 `docs/`。

## docs/ 索引

只列名字,不转述内容(转述会过期)。按文件名自取:

- `claude1-refactor-design.md` — 重构设计
- `codex1-design.md` — codex1 渠道启动器设计
- `product-definition.md` — 产品定位
- `cc-switch-implementation-research.md` — cc-switch 实现调研
- `claude1-protocol-baseline-2026-08-16.md` — 协议层基线与薄弱点
- `anthropic-protocol-implementation-status.md` — 协议能力矩阵与 disposition registry
- `transport-routing-design.md` — transport 路由设计（已落地）
- `维护与兼容指南.md` — 架构与维护约定
- `p0-tasks.md` — 任务队列;存在即有效,从顶部拿活
- `review-findings-2026-08-17.md` — 审查发现清单;R1–R6 是 T0.6 前置
- `publishing.md` — npm 发布 runbook
