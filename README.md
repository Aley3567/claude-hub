# claude1

`claude1` 是一个轻量的 Claude Code 渠道启动器：渠道与凭证继续由 CC Switch
管理，`claude1` 只为本次会话选择渠道。可选的 `claude-hub` 还能在同一个
Claude Code 会话里沿用原生 `/model` 切换渠道和模型。

本仓库的桌面主工作目录、常改文件、兼容层入口和安全发布流程见
[维护与兼容指南](docs/维护与兼容指南.md)。提交和推送前建议先运行
`./scripts/install-git-guards.sh`，启用凭证、私有渠道名和本机配置拦截。

默认安装只新增 `claude1` 和 `claude1-direct` 两个 zsh 函数，不会替换普通
`claude`。

## 第一次使用：3 步完成

### 1. 准备依赖

安装并确认以下组件可用：

- zsh；
- Python 3.11 或更高版本；
- Claude Code CLI，终端中可以运行 `claude`；
- CC Switch，至少配置一个 Claude provider，并已生成
  `~/.cc-switch/cc-switch.db`。

`claude-hub` 和 OpenAI Chat / Responses 协议桥需要
[uv](https://docs.astral.sh/uv/)。没有 uv 仍可使用 Anthropic 原生 provider，
但选择需要协议转换的 provider 会明确报错，无法启动该会话。

### 2. 安装

```bash
git clone https://github.com/Aley3567/claude1.git
cd claude1
./install.sh
source ~/.zshrc
```

安装器会把 Python 脚本和安全的 zsh 集成复制到 `~/.claude`，并在
`~/.zshrc` 添加一条带有 `# claude1 managed source` 标记的 source 行。
安装器实际复制启动器、Hub、命名 Hub 目录模块、共享协议桥和 statusline 模型
解析器五份 Python 文件。已有目标文件和
`~/.zshrc` 会在改写前备份；重复运行不会重复添加 source
行。安装器只检查 CC Switch 数据库是否存在且可读，不读取或复制其中的配置
与凭证。

### 3. 选择渠道

```bash
claude1
```

使用 `↑↓` 或 `j/k` 移动，按 Enter 启动；前 10 项也可以用 `1–9` 和 `0`
直接选择。最近使用的渠道会成为默认光标，但列表顺序和数字编号保持稳定。

## 高频使用速查

```bash
claude1                         # 打开渠道选择器
claude1 mimo                    # 按 provider 名或唯一别名直达
claude1 direct                  # 本次直接启动原生 Claude Code
claude1 current                 # 本次使用 DB is_current 标记的 CC Switch 当前渠道
claude1 any                     # 本次使用已有的 AnyRouter settings
claude1 hub                     # 从默认命名 Hub 的 launch_slot 直启
claude1 hub --slot sonnet       # 从默认命名 Hub 的指定槽位启动
claude1 hub --model lab,model   # 从默认命名 Hub 的指定渠道与模型启动
claude1 list                    # 稳定顺序列出可见渠道
claude1 doctor                  # 本机只读检查，不连接 provider
claude1 doctor --fix            # 备份 DB 后清理 provider 的子代理模型固定值
claude1 usage                   # 查看 token 用量与缓存命中率曲线
claude1 --help                  # 查看完整命令与快捷键
CLAUDE1_NO_ANIMATION=1 claude1  # 关闭启动动画
```

大 Logo 只在进入时流动并呼吸，最长 `240ms`；随后停在明亮静态帧，并在
选择渠道期间保持显示。选择器阻塞等待按键，没有后台动画和定时唤醒；选中
渠道时先清除界面，按 `q` 或 `Esc` 退出则清屏后只留下简短的 Bye 欢迎语。
需要完全跳过开场动画时使用 `CLAUDE1_NO_ANIMATION=1`。

Provider 名称匹配不区分大小写；如果 CC Switch 中存在重名 provider，列表会
附加短 id，直接按名称启动会拒绝歧义，可改用独立别名或
`claude1 id:<provider-id>`。别名不能与 `hub`、`list`、`doctor` 等保留命令
冲突。隐藏、别名和最近使用状态均按 provider id 保存，重命名不会丢失设置。

## 默认隔离边界

- `claude1` 的普通启动只影响本次 Claude Code 会话；
- 不切换 CC Switch 的全局 current provider；
- 不接管普通 `claude`；
- provider 凭证只进入本次 Claude Code 子进程使用的临时 settings；临时文件
  权限为 `0600`，进程结束后删除；
- Hub 以只读方式从 CC Switch DB 获取上游地址和凭证，配置示例中不保存上游
  token。

仓库中的 `zsh-sticky-integration.sh` 是显式 opt-in 功能，默认安装器不会复制
或 source 它。需要让 `claude1 use <backend>` 改变普通 `claude` 的后续路由
时，显式运行：

```bash
./install.sh --enable-sticky
source ~/.zshrc
claude1 use hub
```

未启用时，`claude1 use` 只保存选择并明确提示普通 `claude` 尚未接管，不再
输出已经生效的误导性承诺。无需这项行为时不要传 `--enable-sticky`。
已启用的安装器管理集成可随时撤销，且不会改动自行添加的 shell 配置：

```bash
./install.sh --disable-sticky
source ~/.zshrc
```

`claude1 current` 与 statusline 的 CC Switch 回退都以数据库中唯一的
`providers.is_current=1` 为准；`~/.cc-switch/settings.json` 只视为缓存，不再
作为启动器的当前 provider 真相源。零个或多个 current 标记会 fail closed。

## 可选：在自定义 statusline 中显示实际上游模型

安装器会复制一个不接管布局的模型解析器：
`~/.claude/scripts/statusline-model.py`。它读取 Claude Code 传给 statusLine
命令的同一个 JSON，并只输出实际模型名。已有 statusline 可以复用：

```bash
input=$(cat)
model=$(printf '%s' "$input" | ~/.claude/scripts/statusline-model.py)
```

解析器优先使用最新 assistant 响应模型，并忽略回合末的 attachment、工具结果、
mode、permission-mode、last-prompt、file-history 和 system 统计元条目；回退
时按 stdin `.model.id` 与各 slot 的实际值精确比对，不再靠
`opus`/`sonnet`/`haiku` 关键词猜测。通过 CC Switch 本地代理时，slot 映射只
读取唯一的 DB current provider。

## 可选：启用 claude-hub

Hub 适合需要在不同工作区或一个长会话里频繁切换渠道和模型的人。每个命名 Hub
都有独立的 v2 配置、端口、进程、日志和用量记录；它们都只监听本机
`127.0.0.1`，Claude Code 仍使用原生 `/model` 选择器。

启动首页只列命名 Hub，不展开模型或四个槽位。使用 `↑↓` 选择 Hub，按 Enter
直接从该 Hub 的 `launch_slot` 启动；按 `m`、`→` 或 `Tab` 进入该 Hub 的
Slots / Channels 管理页。首页按 `a` 或 `n` 会复制当前 Hub 的渠道、四槽和
effort，创建一份端口与进程隔离的新 Hub；按 `r` 只修改显示名，不改变稳定 id、
配置路径或运行身份。

进入 Channels 页后，新增渠道使用“渠道 → 模型 → 设置 → 确认”四阶段面板。选择
CC Switch 渠道后，该渠道声明的模型默认全部勾选，可用空格取消或重新勾选；候选
列表没有目标模型时，选择“手动添加模型 ID”。确认后只新增当前 Hub 的渠道，不会
替换四个原生槽位；槽位绑定统一在 Slots 页完成。

1. 安装 uv。
2. 从仓库复制示例配置，并将 provider 名改成 CC Switch 中的真实名称：

   ```bash
   cp examples/claude-hub.example.json ~/.cc-switch/claude-hub.json
   chmod 600 ~/.cc-switch/claude-hub.json ~/.cc-switch/cc-switch.db
   ${EDITOR:-vi} ~/.cc-switch/claude-hub.json
   ```

   `model_slots` 把 Fable、Opus、Sonnet、Haiku 四个原生槽位绑定到已声明的
   `渠道,模型`；`launch_slot` 决定默认直启槽位，`effort_by_slot` 分别保存每个
   槽位的默认 effort。`default_channel` 仍只负责裸模型请求的网关回退路由，
   不等同于启动默认值。

   首次打开 `claude1` 时，启动器会把现有 `~/.cc-switch/claude-hub.json`
   自动登记为名为 `Claude-Hub` 的工作区，并创建目录
   `~/.cc-switch/claude-hubs.json`。旧配置文件只被目录引用，不会被移动或改名。
   新增 Hub 的独立配置写入 `~/.cc-switch/hubs/<hub-id>.json`。

   目录文件的结构如下；其中所有路径都相对于目录文件所在位置：

   ```json
   {
     "version": 1,
     "default_hub": "claude-hub",
     "order": ["claude-hub"],
     "hubs": {
       "claude-hub": {
         "name": "Claude-Hub",
         "config": "claude-hub.json",
         "log": "logs/claude-hub.log",
         "usage": "logs/claude-hub-usage.jsonl"
       }
     }
   }
   ```

   channel 的 `provider` 建议写成 `id:<provider-id>`；Hub 的 Channels 添加向导
   会始终保存稳定 id，不保存凭证。向导优先复用 CC Switch 的协议元数据；无法
   判断时会要求选择 Anthropic、OpenAI Chat 或 OpenAI Responses，避免静默使用
   错误协议。每个旧版 Hub 配置首次打开时都会原子迁移到 v2，并在其原目录留下
   一份 `<配置文件名>.bak-migrate-*` 私有备份。

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

`doctor` 默认只检查本机配置、数据库、渠道映射和文件权限，不会连接 provider，也
不会显示上游地址或 token。Hub 会要求配置、CC Switch 数据库以及当前存在的
`-wal`、`-shm` 文件权限不超过 `0600`；检查失败时按输出修正后再启动。

进入 Claude Code 后运行 `/model`，模型以 `渠道别名,模型名` 的形式出现。Slots
页可修改当前 Hub 各槽位的默认 effort，并用 `b` 将模型池里的模型绑定到某个槽位；
Channels 页可添加或删除未被回退路由和槽位引用的渠道。槽位默认 effort 通过本次
会话的临时 settings 注入，不会用环境变量锁死，进入会话后仍可用原生 `/effort`
调整。Hub 会在请求发生时从 CC Switch DB 只读获取对应 provider 的凭证，不修改
DB、provider 或 current 状态。

## 可选：查看 token 用量与缓存命中率

请求经过 Hub 时，网关会把每条响应的 token 用量（输入 / 输出 / 缓存读 /
缓存写）追加到 `~/.cc-switch/logs/claude-hub-usage.jsonl`（权限 `0600`，只
存 token 计数与时间，不含任何凭证）。平时由 Hub 实时写入；也可以把 Claude
Code 本地会话记录里已有的用量一次性导入同一文件（见下）。

```bash
claude1 usage            # 最近 24 小时（默认）
claude1 usage --day      # 最近 24 小时，按小时分桶
claude1 usage --week     # 最近 7 天，按天分桶
claude1 usage --month    # 最近 30 天，按天分桶
```

输出分两部分：

- **汇总表**：请求数、输入 / 输出 / 缓存读 / 缓存写 token，以及整体缓存
  命中率（`缓存读 / (输入 + 缓存读)`）；
- **Braille 双曲线**：缓存命中率与 token 量（按窗口内峰值归一化）随时间的
  变化；彩色终端使用两种颜色区分，禁用颜色时共用 Braille 点阵并保留文字图例。

两点说明：

- 只有返回了 cache 字段的上游（官方 / 原生 Anthropic 转发）缓存命中率才是
  真实值；多数 OpenAI 兼容中转不返回 cache 字段，对应渠道的缓存读会计为
  `0`，命中率因此偏低属正常；
- 用量文件由 Hub 写入，首次使用或窗口内无记录时，`claude1 usage` 会提示先
  用 `claude1 hub` 跑几个请求。

### 导入已有会话的用量

除了 Hub 实时埋点，也可以把 Claude Code 本地会话记录（
`~/.claude/projects/*/*.jsonl`）里已经发生的用量一次性导入同一个统计文件，
这样能覆盖不经过 Hub 的直连渠道。导入按消息 id 去重，时间戳取消息本身的
时间。一个可用的导入脚本片段：

```bash
python3 - <<'EOF'
import json, glob, os, time, datetime
start = time.mktime(datetime.date.today().timetuple())  # 今天 00:00
seen = {}
for path in glob.glob(os.path.expanduser("~/.claude/projects/*/*.jsonl")):
    for line in open(path, encoding="utf-8", errors="ignore"):
        try: r = json.loads(line)
        except Exception: continue
        m = r.get("message")
        if not (isinstance(m, dict) and isinstance(m.get("usage"), dict)): continue
        ts = datetime.datetime.fromisoformat(
            (r.get("timestamp") or "").replace("Z", "+00:00")).timestamp()
        if ts < start: continue
        u = m["usage"]
        seen[m.get("id")] = (ts, u, m.get("model", ""))
out = os.path.expanduser("~/.cc-switch/logs/claude-hub-usage.jsonl")
with open(out, "a", encoding="utf-8") as f:   # 追加；想重来先清空该文件
    for ts, u, model in sorted(seen.values()):
        f.write(json.dumps({"ts": int(ts), "channel": "import",
            "model": model, "format": "session",
            "in": u.get("input_tokens", 0), "out": u.get("output_tokens", 0),
            "cr": u.get("cache_read_input_tokens", 0),
            "cw": u.get("cache_creation_input_tokens", 0)}) + "\n")
os.chmod(out, 0o600)
print("导入", len(seen), "条")
EOF
```

## 安装位置与备份

默认位置：

```text
~/.claude/
├── scripts/
│   ├── claude-provider-once.py
│   ├── claude-hub.py
│   ├── claude1_protocol.py
│   └── statusline-model.py
├── claude1/
│   └── zsh-functions.sh
└── backups/
    └── <时间>-<进程号>/
```

隔离测试或自定义安装位置时可以显式注入目录：

```bash
HOME=/tmp/claude1-home \
CLAUDE1_INSTALL_ROOT=/tmp/claude1-install \
./install.sh
```

安装器始终从 `$HOME/.cc-switch/cc-switch.db` 检查 CC Switch 是否已经就绪。

## 项目结构

| 路径 | 作用 |
| --- | --- |
| `claude-provider-once.py` | 一次性 provider 选择、TUI 与 Claude Code 启动 |
| `claude-hub.py` | 可选的本地 Anthropic gateway |
| `claude1_protocol.py` | Anthropic / OpenAI Chat / OpenAI Responses 协议转换 |
| `statusline-model.py` | 自定义 statusline 可复用的实际上游模型解析 |
| `zsh-functions.sh` | 默认安全的 `claude1` shell 集成 |
| `zsh-sticky-integration.sh` | 需要人工接入的普通 `claude` 粘性路由 |
| `examples/claude-hub.example.json` | 无凭证 Hub 配置示例 |
| `install.sh` | 幂等安装与改写前备份 |
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
