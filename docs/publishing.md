# 发布 model-bridge 到 npm

发布与授权的操作细节（passkey 2FA、EOTP 处理、PTY 授权链接提取、网页授权轮询）
见 Claude Code skill：`npm-publish`（`~/.claude/skills/npm-publish/SKILL.md`）。

## 首次发布 / 日常发布

1. 手动改 `package.json` 的 `version`（**不用 `npm version`**，避免自动 git commit）。
2. 发布前自检：

   ```bash
   npm pack --dry-run          # 确认 tarball 包含 bin/、根目录 9 个 Python 模块、
                               # shell 脚本、examples/ 与 README/LICENSE
   python3 -m unittest discover -s tests -p "test_*.py"
   ```

3. `npm publish --access=public`（scoped 包必须显式 public；非 scoped 名
   `model-bridge` 因与已有包 `modelbridge` 过于相似被 npm 反抢注规则拒绝）。
   `prepublishOnly` 钩子先跑 secret guard 拦截凭证泄漏。
   发布需 passkey 网页授权（Use security key + Touch ID），授权链接从 PTY 日志提取。
4. 发布后 registry 读接口有约 2–5 分钟同步延迟，`npm view` 404 属正常，验证：

   ```bash
   npm install -g @yufeng-dev/model-bridge
   model-bridge --version
   model-bridge doctor
   ```

## 清理旧版本

仅当旧版本发布**未超过 72h** 且无依赖者时可 `npm unpublish <pkg>@<ver>`；
对仍在 `latest` 的旧版先发新版切走 latest 再 unpublish。超窗改用
`npm deprecate <pkg>@<ver> "reason"`。**不要用 `&&` 串联多个需授权的
npm 命令**——publish / unpublish 各自要一次独立授权。

## 发布源与 files 白名单

`package.json` 的 `files` 跟随仓库当前结构（根目录平铺：Python 模块、
shell 脚本、`bin/`、`examples/`；曾为 `core/` `shell/` 目录，树重组后已更新）。
README 图片须用 unpkg 绝对地址（`https://unpkg.com/<pkg>/assets/...`）。

## PyPI / pipx（可选第二渠道）

需要先把启动器转成标准 Python 包（`pyproject.toml` + console_scripts 入口）。
`model-bridge` 在 PyPI 目前未被占用，转包改造完成时同名发布即可。
桌面 App 不走 npm/PyPI，由 Tauri 产出 `.dmg` / `.msi`，经 GitHub Releases 分发。
