# claude1 0.1.0 发布验证

验证日期：2026-07-21
验证分支：`codex/product-v1`

## 结论

`claude1` 的首次引导、日常渠道直达、可选 Hub 会话内切换、安装与隔离边界均已
通过本机和 GitHub CI 验证。默认安装不会接管普通 `claude`，不会修改或重启
ReClaude，也不会改变 CC Switch 当前渠道。

## 分阶段交付

1. 固定产品边界和同类产品取舍；
2. 将 launcher 与可选 `claude-hub` 隔离联动；
3. 完成可跳过的渐进式欢迎动画、稳定编号、最近使用和脚本化命令；
4. 加固 Hub 的本地鉴权、只读 DB 快照、HTTP 边界、SSE 与压缩流处理；
5. 加固日志、MRU、后端状态和临时 settings 的原子私密写入；
6. 备份、安装、精准替换旧 Hub，并完成真实运行验收。

## 自动化验证

- Python：84 个测试通过；
- zsh 集成：8 个测试通过；
- 安装器：4 个测试通过；
- Python 编译、sh/zsh 语法和 `git diff --check` 通过；
- GitHub Actions 在 macOS 与 Ubuntu、Python 3.11 与 3.12 的四组矩阵全部通过：
  [tests run 29796654627](https://github.com/Aley3567/claude1/actions/runs/29796654627)。

测试覆盖临时 HOME、fixture DB、fake Claude、fake upstream、严格健康契约、鉴权、
模型发现、请求透传、流式终态、CR/LF/CRLF、UTF-8 BOM、gzip/x-gzip/deflate、
截断与损坏压缩流、文件权限、符号链接和 FIFO 拒绝，以及 Logo 渐进开场、
呼吸周期不进入 `A_DIM`、开场后保留静态 Logo、退出清屏和终端 EOF 快速退出。
SSE 终态追踪器另以 5 万组
随机字段、换行和分块边界与参考实现比对，无差异。

## 隔离与真实运行验证

- 在临时目录和随机回环端口完成了 Hub 启动、路由、凭证注入和单次上游调用；
- 使用真实配置结构完成离线 `doctor` 与启动验证，全程未连接 provider；
- 安装后的三份文件与仓库逐字一致，模式为 `0755 / 0755 / 0644`；
- zsh 中只有一条 managed source，未接入可选 sticky integration；
- 真实 256 色 PTY 中，纯 Logo 绘制为 `53.6µs/帧`；动画只存在于最长
  `240ms` 的开场。Logo 在选择页静态保留，选择器立即使用阻塞读键，不再运行
  定时刷新；
- 最新仓库版本在隔离 PTY 中完成一次开场、静置和退出：静置 `800ms` 输出
  `0 bytes`，整个进程累计 CPU `57.2ms`，按 `q` 后退出码为 `0` 且只留下
  `Bye，欢迎下次使用 claude1。`；
- live Hub 只有一个 `127.0.0.1` 监听者，健康契约、鉴权和模型发现通过；
- 通过安装后的 launcher、live Hub 和 fake Claude 完成端到端启动，临时 settings
  权限为 `0600` 且进程结束后删除；
- 使用近期已成功的真实渠道完成一次 `max_tokens=1` 请求：状态 200、响应结构
  正确、实际路由与所选路由一致；
- Hub 配置、CC Switch DB、日志、MRU 和后端状态均为 `0600`，DB 只读检查后未
  产生 WAL/SHM 副作用；
- 普通 `claude` 函数、共享 settings、sticky 状态、既有 ReClaude 进程、长运行
  launcher 和 CC Switch 监听均保持安装前状态。

真实验收不记录或提交 provider 名称、上游地址、token、请求正文或响应正文。

## 已知限制

- 一个依赖显式代理的渠道在当前系统 Python TLS 链路中仍会遇到证书错误或超时。
  Hub 没有为此关闭 TLS 校验；该问题作为渠道或代理环境问题单独处理，不影响
  已通过的其他真实渠道。
- 尚未执行长时间、高并发的真实外部压缩流压力测试；协议和边界条件已由本地
  loopback 与随机测试覆盖。
- 默认安装只提供安全的 `claude1` 入口。是否让普通 `claude` 读取 sticky 状态是
  单独的显式选择，不属于本次默认发布范围。

## 回滚边界

安装前的脚本、shell 配置、Hub 配置、数据库一致性快照和运行状态文件已保存在
`~/.claude/backups/` 下的自动备份与 `manual-stage4-*` 私密目录中。回滚时只应
停止经端口、命令路径和父子关系确认的 Hub 进程，再恢复对应文件；不得按模糊
进程名终止 Claude、ReClaude 或 CC Switch。
