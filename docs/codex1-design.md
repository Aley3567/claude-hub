# codex1 设计

`codex1` = 启动 Codex CLI 前选一个 CC Switch 渠道，本次会话生效。它**不是** hub：没有本地网关、协议转换、账号池、成本护栏、健康检查、failover。要的只是"随时选一个喜欢的渠道随开随用"。

## 为什么走"影子 CODEX_HOME + profile 层叠 + env_key"

cc-switch 切 codex 渠道的做法是**整体重建** `~/.codex/config.toml` 和 `~/.codex/auth.json`：把当前渠道的 config 片段写进去，把凭证写进 auth.json。这对"切换"是对的，对"临时用一次"是灾难——它是全局的、有状态的，两个终端不能各用各的渠道，退出后还得记得切回来。

所以 codex1 不写用户的任何文件，改用三段机制：

1. **影子 `CODEX_HOME`**：`mkdtemp(prefix="codex1-", mode 0700)`，把真实 `~/.codex` 下**所有**条目（含点开头的）`symlink` 进去，只有 `config.toml` / `auth.json` 特殊处理。codex 不拒绝 symlink，sessions/history/日志照常写真实目录，1GB 级 sqlite 经 symlink 读写正常（`-wal/-shm` 落在真实路径）。退出时 `shutil.rmtree` 整个删掉。
2. **profile 层叠**：`config.toml` 仍 symlink 到真实的，用户的 mcp_servers / agents / tui / features 原样继承；渠道差异只写进影子里的 `codex1.config.toml`，靠 codex 原生的 `codex -p codex1` 层叠上去。**我们不自己 merge TOML**——merge 语义由 codex 负责，我们只负责生成一个正确的覆盖层。
3. **段重命名 + `env_key`**：把渠道 config 里 `model_provider` 指向的那个段（`custom` / `chatgpt_http` / …）重命名成 `[model_providers.codex1]`，profile 顶层写 `model_provider = "codex1"`。这样不管基础 config 当前是哪个渠道，层叠结果都一定是我们选的这个，不会和基础段撞名。API key 类在段内写 `env_key = "CODEX1_API_KEY"`：0.148 里 `env_key` 优先级最高，设了它就只认环境变量，`requires_openai_auth = true` 也不会回退 auth.json；变量缺失时直接报 `Missing environment variable` 而不是静默换渠道。

OAuth 渠道（`auth_mode == "chatgpt"`）没有 API key 可注入，整块 `auth` 写进影子 `auth.json`（0600，退出即删）；API key 渠道的影子 `auth.json` 写 `{}`，防止意外回退到真实凭证。

## 必须自检 profile 文件存在

codex 对**不存在的 profile 名不报错**，直接静默回落到基础 config——用户会以为切了渠道，其实还在用默认渠道跑，账单和数据都去了错地方。因此启动前断言影子里的 `codex1.config.toml` 存在且可读，缺失就 fail-fast。同理：渠道 config 里 `model_provider` 指向的段缺失时拒绝启动，绝不"兜底成用默认渠道"。

## 凭证三条红线

- **只读**：凭证只从 `~/.cc-switch/cc-switch.db` 以 `mode=ro` URI 读；真实 `~/.codex/config.toml`、`~/.codex/auth.json`、cc-switch DB 全程一个字节都不改。
- **不落盘、不进日志、不进 argv**：API key 只活在子进程环境变量 `CODEX1_API_KEY` 里；`experimental_bearer_token` 在生成 profile 时就被删掉，永不写文件；MRU（`~/.cc-switch/codex1-mru.json`，0600）只记 provider id 和时间戳；确认行只打渠道名、认证类型和 base_url。
- **异常路径同样干净**：settings_config 解析失败时不带原始异常（它含凭证片段），只报"不是合法 JSON"。

## 已知取舍

渠道 config 里的嵌套表（`[tui]` / `[features]` / `[projects]` 等）不搬进 profile：它们是用户级设置，已经由 symlink 过来的基础 config.toml 提供，重复搬运还要一个完整的嵌套 TOML writer。被跳过的键名会打在 stderr 上，不静默。
