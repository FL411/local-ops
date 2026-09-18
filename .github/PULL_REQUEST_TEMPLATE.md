## 变更目的

<!-- 说明要解决的问题、用户场景和为什么需要这次改动。 -->

## 主要改动

<!-- 用简短列表说明实现范围；不要夹带无关变更。 -->

## 验证

<!-- 列出实际执行的命令和手工流程，例如 `make check`。 -->

- [ ] `python tools\check_project.py`
- [ ] 涉及发行范围、许可或打包时已运行 `make release-check`
- [ ] UI 变化已检查相关主题、明暗模式和窄屏
- [ ] 进程/配置变化已覆盖失败、升级和回退路径
- [ ] 已更新 `CHANGELOG.md` 的 `Unreleased`，或已在说明中写明不适用

## 安全、隐私与素材

- [ ] 没有提交 `data/`、`%APPDATA%\总控台`、`%LOCALAPPDATA%\总控台`、凭据、token、个人绝对路径或真实用户命令
- [ ] 截图、录屏、日志和示例已经脱敏
- [ ] 没有削弱回环绑定、写接口授权、TokenUser SID 或受控进程身份校验
- [ ] 没有引入 macOS/Linux 运行分支或交付物
- [ ] 新增/替换素材已更新 `ASSET_PROVENANCE.md`；必要时同步 `THIRD_PARTY_NOTICES.md` 和许可原文
- [ ] 发行范围内没有 `BLOCKED` 或 `TO_REPLACE` 素材

## 兼容性与回退

<!-- 说明配置 schema、数据迁移、Windows/Python 要求及安全回退方式；不适用请写“无”。 -->

## 截图或证据

<!-- 仅附脱敏内容；不要上传 config.json 或完整日志。 -->
