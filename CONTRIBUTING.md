# 参与贡献

感谢你帮助改进总控台。项目仍处于 Preview / Alpha 阶段，优先接受范围清晰、可验证且不扩大安全边界的改动。

> **维护立场**：本仓库由作者个人维护，PR 不承诺审阅或合入。希望增加功能、适配其他平台的朋友，请优先 Fork 自行修改，并在 Discussions 提交衍生版本说明（详见 README 的「维护说明」与「社区衍生版本」）。以下规范供提交讨论与 Fork 开发参考。

## 开始之前

1. 先搜索已有 Issue 和 Pull Request，避免重复工作。
2. 较大的功能、配置 schema 变化、进程管理策略或 UI 主题调整，请先开 Issue 说明动机、用户场景和兼容性影响。
3. 安全漏洞不要公开讨论，按 [`SECURITY.md`](SECURITY.md) 私下报告。
4. 不要提交本机 `data/`、Application Support、Library Logs、个人路径、完整命令、token、用户图标或未脱敏截图。

## 开发环境

- macOS 12 或更高版本；
- Python 3.12；
- Node.js，仅用于 JavaScript 语法检查；
- 运行时无第三方 Python 依赖。

只有重新生成纹理时才需要开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
```

## 修改原则

- 后端保持 Python 标准库实现；前端保持原生 ES Modules、无 CDN、无构建。
- 不得削弱回环绑定、当前 UID、run token、进程组、Host/Origin 或控制令牌等安全校验。
- 不得按端口直接结束未知进程。
- 配置变更必须有明确 `schemaVersion`、幂等迁移和升级测试。
- DOM 列表应按 key 原地更新，避免轮询造成整表闪烁。
- 危险操作必须有明确确认。
- 修改 `static/icons/*.svg` 后运行 `make generate-icons`，不要手改 `static/icons.js`。

## 素材与许可

新增或替换字体、Logo、App Icon、favicon、插画、照片、纹理、声音等素材时，Pull Request 必须同时：

1. 更新 [`ASSET_PROVENANCE.md`](ASSET_PROVENANCE.md)；
2. 记录来源、作者/生成方式、版本、修改过程、许可、SHA-256 和凭证位置；
3. 需要时更新 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 并随包加入许可原文；
4. 确认素材状态不是 `BLOCKED` 或 `TO_REPLACE`。

只有“网上可下载”“AI 生成”或“免费使用”的说明不足以证明可随开源项目再分发。

## 检查

提交前运行：

```bash
make check
```

涉及发行范围、许可证、静态资源或打包逻辑时，再运行：

```bash
make release-check
```

Pull Request 应说明：

- 改了什么、为什么；
- 用户可见影响和风险；
- 执行过的检查及结果；
- 必要的手工验收步骤；
- 是否影响配置、数据、进程生命周期、素材许可或发布范围。

## 变更记录

- 用户可感知的功能、修复、安全或兼容性变化必须写入
  [`CHANGELOG.md`](CHANGELOG.md) 的 `Unreleased`。
- 使用 `Added`、`Changed`、`Fixed`、`Removed` 或 `Security`
  描述用户结果，不记录实现步骤。
- 纯缓存清理、过期本地构建产物和不影响行为的内部重构不必写入；
  Pull Request 中应说明为什么不适用。
- 发布时将 `Unreleased` 中的内容移动到对应版本和发布日期，并重新保留空的
  `Unreleased` 章节。

## Commit 与 Pull Request

- 使用简洁、可追溯的 commit；不要使用占位邮箱或伪造作者身份。
- 一个 Pull Request 尽量只解决一个主题。
- 不重写他人的历史，不夹带无关格式化或生成文件。
- 如果 UI 有变化，提供不含个人路径和真实服务信息的脱敏截图。
- 贡献即表示你有权提交该内容，并同意项目按根目录 `LICENSE` 及对应素材许可分发。

所有参与者都应遵守 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
