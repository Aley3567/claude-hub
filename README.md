# claude1

`claude1` 是一个轻量的 Claude Code 渠道启动器：渠道与凭证继续由 CC Switch
管理，`claude1` 只为本次会话选择渠道。可选的 `claude-hub` 还能在同一个
Claude Code 会话里沿用原生 `/model` 切换渠道和模型。

本仓库的桌面主工作目录、常改文件、兼容层入口和安全发布流程见
[维护与兼容指南](docs/维护与兼容指南.md)。提交和推送前建议先运行
`./scripts/install-git-guards.sh`，启用凭证、私有渠道名和本机配置拦截。

默认安装提供 `claude-hub`、`claude1` 和 `switchctl` 三个 Python 包入口，
不会改写普通 `claude`、shell 配置或本机 Provider 配置。

## 第一次使用：3 步完成

### 1. 准备依赖

安装器只要求 Python 3.11 或更高版本，以及该 Python 可用的 pip。安装时不要求
HOME、zsh、Claude Code 或 CC Switch，也不会读取它们的配置。

### 2. 安装

```bash
git clone https://github.com/Aley3567/claude-hub.git
cd claude-hub
./install.sh
```

这等价于用检测到的 Python 执行
`python -m pip install --upgrade claude-hub-kit`。再次运行会升级现有安装；
需要桌面依赖时显式运行 `./install.sh --desktop`，安装
`claude-hub-kit[desktop]`。安装器会在执行前显示经过 shell 安全引用的准确
命令。

### 3. 选择渠道

```bash
claude1
```

使用 `↑↓` 或 `j/k` 移动，按 Enter 启动；前 10 项也可以用 `1–9` 和 `0`
直接选择。列表会显示每个 Provider 的主模型摘要；前三项保持 CC Switch
顺序和数字编号稳定，其余项按最近使用频率与时间衰减排序。最近使用的渠道仍会
成为默认光标。

选中普通 Provider 后按 `→` 可进入模型页。模型页默认是 `NORMAL`：

- `↑↓` 或 `j/k` 选择该 Provider 已存在的主模型、Opus、Fable、Sonnet、
  Haiku 或 Reasoning 字段；
- `Enter` 或 `i` 进入 `INSERT`；直接输入会替换整个旧值，先按 `←→`、Home
  或 End 定位则保留旧值并从光标处修改，`Ctrl+U` 可清空当前输入；
- `Enter` 校验并保存到 CC Switch，然后回到 `NORMAL`；
- `Esc`（或 `Ctrl+C`）取消本次输入不保存；`NORMAL` 下按 `Esc`、`←` 或
  `q` 返回 Provider 菜单。

保存只更新选中的模型字段。Token、Base URL、Provider 元数据和未知 JSON
字段保持不变；Hub 的独立模型列表也不会被改写。写入前会在
`~/.cc-switch/backups/claude1-model-editor/` 创建权限为 `0600` 的 SQLite
备份，并使用事务和原始配置对比检测 CC Switch 的并发修改。当前 Provider
会同步对应的 Claude live 模型字段；代理接管时只同步 CC Switch 的恢复备份，
不会把模型写进代理占位配置。

## 高频使用速查

```bash
claude1                         # 打开渠道选择器
claude1 mimo                    # 按 provider 名或唯一别名直达
claude1 direct                  # 本次直接启动原生 Claude Code
claude1 current                 # 本次使用 CC Switch 当前渠道
claude1 any                     # 本次使用已有的 AnyRouter settings
claude1 hub                     # 本次通过可选 claude-hub 启动
claude1 hub --model lab,model   # 指定 Hub 渠道与模型后启动
claude1 list                    # 按与选择器一致的顺序列出可见渠道
claude1 doctor                  # 本机只读检查，不连接 provider
claude1 --help                  # 查看完整命令与快捷键
CLAUDE1_NO_ANIMATION=1 claude1  # 关闭启动动画
```

大 Logo 的入场动画最长 `240ms`。入场和每次交互后会以约 `6.7fps` 继续低帧率
呼吸，默认最多 `8s`，随后恢复零定时唤醒的阻塞等待；可用
`CLAUDE1_BREATH_SECONDS=0` 只保留入场动画。选中渠道时先清除界面，按 `q`
或 `Esc` 退出则清屏后只留下简短的 Bye 欢迎语。需要关闭全部动画时使用
`CLAUDE1_NO_ANIMATION=1`。

Provider 名称匹配不区分大小写；如果多个名称都匹配，会要求再次选择，避免
静默走错渠道。别名不能与 `hub`、`list`、`doctor` 等保留命令冲突。

## 可选：指定 Provider 的空结束保护

Turn Guard 默认关闭。只在本机
`~/.cc-switch/claude1-config.json` 中为某一个 Provider 显式设置
`"turn_guard": true` 后启用，例如：

```json
{
  "version": 2,
  "providers": {
    "Provider Display Name": {
      "hidden": false,
      "turn_guard": true
    }
  },
  "backends": {
    "target-settings-backend": {
      "turn_guard": true
    }
  }
}
```

`providers` 项控制菜单 Provider；如果同一目标还有独立 settings backend，可
在 `backends` 中对它单独 opt-in。claude1 只会把 Guard 注入这些目标本次进程
使用的临时 settings，不修改原 settings 文件或 `~/.claude/settings.json`，
也不影响普通 `claude`、`current`、`direct`、其他 Provider 或其他 backend。
Guard 仅在响应以 `end_turn` 结束、包含 `thinking` 且没有 `text`/`tool_use`
时续跑一次；如果续跑仍为空，则熔断并正常停止，避免循环。

状态日志默认写入 `~/.claude/claude1/turn-guard/watch.log`，权限为 `0600`。
日志只包含时间和固定状态，不记录 prompt、thinking、正文、工具参数、Provider
名称、地址或凭证；达到 256 KiB 后轮转为一个同权限备份。

## 默认隔离边界

- `claude1` 的普通启动只影响本次 Claude Code 会话；
- 不切换 CC Switch 的全局 current provider；
- 不接管普通 `claude`；
- provider 凭证只进入本次 Claude Code 子进程使用的临时 settings；临时文件
  权限为 `0600`，进程结束后删除；
- custom `ANTHROPIC_BASE_URL` 必须同时带有该 Provider 明确配置的
  `ANTHROPIC_AUTH_TOKEN` 或 `ANTHROPIC_API_KEY`；缺少时直接拒绝启动，不回退
  本机 Claude.ai OAuth 或其他官方凭证；
- 每次启动都会清除继承的 Anthropic/Claude 路由状态，只注入当前 Provider
  的 URL、凭证和模型字段，并设置 `CLAUDE_CODE_SUBPROCESS_ENV_SCRUB=1`，
  避免 Bash、hooks 和 MCP stdio 子进程继承 Provider 凭证；
- 模型编辑器是唯一会写 CC Switch 的交互；它只写选中的模型字段，并在写入前
  创建私有数据库备份和执行并发冲突检测；
- Hub 以只读方式从 CC Switch DB 获取上游地址和凭证，配置示例中不保存上游
  token。

## Provider Capability Profile

Provider 不再只由 `api_format` 描述。普通 Provider、一次性 OpenAI 协议桥和
多渠道 Hub 共用 `claude1_provider.py` 中的同一份 schema：

```json
{
  "protocol": "anthropic",
  "tool_search": "unsupported",
  "count_tokens": "estimated",
  "context_window": "unknown",
  "thinking": "unsupported",
  "reasoning_round_trip": "unsupported",
  "prompt_cache": "unknown",
  "stream_terminal_usage": "unsupported",
  "beta_policy": "filtered",
  "background_worker_safe": "unverified",
  "model_id_strategy": "opaque"
}
```

可在 `~/.cc-switch/claude1-config.json` 的单个
`providers.<Provider 名>.capabilities`，或 Hub 的单个
`channels.<别名>.capabilities` 中覆盖。Hub 渠道配置优先于 CC Switch
Provider settings/metadata；未声明项使用安全默认值，不会借用其他 Provider
的探测结果。`context_window` 只接受正整数或 `"unknown"`，未知上限不会显示为
已验证的 200K；`[1m]` 只有在声明至少 `1000000` 后才允许路由。
同一 Provider 的模型能力不同时，可增加
`capabilities.models.<精确模型 ID>` 覆盖；匹配区分大小写且不按模型名称猜测。

`beta_policy=filtered` 默认移除额外 beta；需要保留特定值时配置
`beta_allowlist`。`mapped` 必须同时提供 `beta_map`。OpenAI Chat/Responses
没有标准精确 token-count endpoint，因此只能声明 `estimated` 或
`unsupported`，不能伪装成 `exact`。`claude1 doctor` 和
`claude-hub.py doctor` 会离线、脱敏显示每个字段的来源与
verified/declared/unverified 状态。

Claude Code 官方产品控制面与第三方 API 兼容能力是两回事：Claude.ai OAuth、
Remote Control、Fast Mode 和 Claude.ai connectors 不属于普通 Messages API
capability，Claude1 不会为第三方 Provider 宣称或模拟这些能力。Claude Code
官方文档也说明 custom `ANTHROPIC_BASE_URL` 默认关闭 Tool Search，只有网关
明确支持 `tool_reference` 时才应显式开启 `ENABLE_TOOL_SEARCH=true`。

仓库中的 `zsh-sticky-integration.sh` 是显式 opt-in 功能，默认安装器不会复制
或 source 它。只有手动接入该文件后，`claude1 use <backend>` 写入的
`~/.cc-switch/claude1-backend` 才会改变普通 `claude` 的后续路由。无需这项
行为时不要接入该文件。

## 可选：启用 claude-hub

Hub 适合需要在一个长会话里频繁切换渠道和模型的人。它监听本机
`127.0.0.1`，Claude Code 仍使用原生 `/model` 选择器。

1. 安装 uv。
2. 从仓库复制示例配置，并将 provider 名改成 CC Switch 中的真实名称：

   ```bash
   cp examples/claude-hub.example.json ~/.cc-switch/claude-hub.json
   chmod 600 ~/.cc-switch/claude-hub.json ~/.cc-switch/cc-switch.db
   ${EDITOR:-vi} ~/.cc-switch/claude-hub.json
   ```

3. 生成并保存一个仅供本机 Claude Code 连接 Hub 使用的 token，再启动。Hub
   持续运行期间应复用同一个 token：

   ```bash
   umask 077
   [[ -s ~/.cc-switch/claude-hub-token ]] || \
     python3 -c 'import secrets; print(secrets.token_urlsafe(32))' \
       > ~/.cc-switch/claude-hub-token
   export CLAUDE_HUB_LOCAL_TOKEN="$(< ~/.cc-switch/claude-hub-token)"
   ~/.claude/scripts/claude-hub.py doctor
   claude1 hub
   ```

`doctor` 只检查本机配置、数据库、渠道映射和文件权限，不会连接 provider，也
不会显示上游地址或 token。Hub 会要求配置、CC Switch 数据库以及当前存在的
`-wal`、`-shm` 文件权限不超过 `0600`；检查失败时按输出修正后再启动。

进入 Claude Code 后运行 `/model`，模型以 `渠道别名,模型名` 的形式出现。Hub
会在请求发生时从 CC Switch DB 只读获取对应 provider 的凭证，不修改 DB、
provider 或 current 状态。

## 安装位置

包与三个命令入口的安装位置由所选 Python/pip 环境决定，与直接运行
`python -m pip install --upgrade claude-hub-kit` 一致。`install.sh` 不建立
独立安装目录、不备份或改写 HOME，也不检查 CC Switch。

## 项目结构

| 路径 | 作用 |
| --- | --- |
| `claude-provider-once.py` | 一次性 provider 选择、TUI 与 Claude Code 启动 |
| `claude-hub.py` | 可选的本地 Anthropic gateway |
| `claude1_protocol.py` | Anthropic / OpenAI Chat / OpenAI Responses 协议转换 |
| `claude1_provider.py` | 统一 Provider capability profile、来源审计与凭证隔离 |
| `claude1-turn-guard.py` | 指定 Provider opt-in 的 thinking-only Stop Guard |
| `zsh-functions.sh` | 默认安全的 `claude1` shell 集成 |
| `zsh-sticky-integration.sh` | 需要人工接入的普通 `claude` 粘性路由 |
| `examples/claude-hub.example.json` | 无凭证 Hub 配置示例 |
| `install.sh` | 核心包与显式 desktop extra 的 pip 兼容包装器 |
| `tests/` | launcher、Hub、shell 与安装器的隔离测试 |
| `docs/product-research.md` | 产品边界、同类产品研究与验收合同 |
| `docs/release-verification.md` | 0.1.0 的测试、实机隔离与发布验证记录 |

产品取舍和协议范围见
[docs/product-research.md](docs/product-research.md)，发布证据与已知限制见
[docs/release-verification.md](docs/release-verification.md)。

## 开发验证

```bash
python3 -m pip install "aiohttp>=3.9"
./scripts/install-git-guards.sh
python3 -m unittest discover -s tests -p "test_*.py" -v
./tests/test_shell_integration.zsh
./tests/test_install.zsh
```

测试使用临时目录、fixture DB、fake Claude 和 fake upstream，不需要访问真实
provider。

## License

MIT
