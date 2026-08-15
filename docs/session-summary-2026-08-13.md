# 2026-08-13 会话总结（Windows 移植完备化）

> 范围：local-ops（总控台）Windows 移植版的稳定性修复、功能对齐、体验增强与贡献准备。
> 提交：22 个（`27d29dc` → `dbd84cc`），工作区干净。

## 一、修复 8 个真实 Bug

| 严重度 | Bug | 影响 |
| --- | --- | --- |
| 严重 | 单实例锁两处缺陷（锁位置依赖 pid 位数 → 双实例并发写配置；未获锁进程崩溃） | 配置损坏 + 崩溃 |
| 中 | `detect_project` 硬编码 `python3` | Windows 项目识别不可用 |
| 中 | restart 后新实例日志丢失（缺 `--log-to-file`） | 重启后无法诊断 |
| 中 | start.bat 分支 1 `%PY%` 为空（ShellExecute 弹"选择程序"） | 启动弹窗 + 菜单失效 |
| 中 | 设置中心硬编码 mac 路径 | Windows 显示错误路径 |
| 低 | 快捷键 mac 符号 / fallback 命令 mac 语义 / 路径分割不识别 `\` | 体验错乱 |
| 低 | 托盘三处（message-only 宿主、`WM_NULL` 未定义、右键协议） | 右键菜单两轮失效 |

## 二、功能对齐

- API 层 100% 对齐（13 顶层路由 + 22 handler，0 缺失）；README 8 条功能逐项达成。
- 差异仅为平台语义取舍（UID/CPU 口径/优雅停止/Shell），见 README 支持矩阵。

## 三、测试体系

- 168 项全绿（26 项 macOS 专属跳过），33 项失败清零。
- 固化回归：单实例锁（并发互斥/优雅拒绝/释放重取）、托盘（WM_NULL/回调/窗口宿主）。

## 四、Windows 专属增强

- **系统托盘**（纯 ctypes 零依赖）：状态 tooltip、左键打开、右键菜单（打开/重启/停止/退出）。
- **exe 启动器**（C# + 系统编译器 + 多尺寸品牌图标 16/32/48/256）：双击无窗口启动。
- start.bat 跨机器加固（探测通用化 + Python≥3.12 校验）；mac 残留全面清零。

## 五、文档与贡献

- README「Windows 平台支持矩阵」、`docs/windows-parity-check.md`、`docs/upstream-pr-map.md`。
- 上游 Issue #1 提案已发布，等维护者回应后按 PR 映射推进。

## 六、环境治理

- Python 4 套盘点 → 3.11 卸载（残留全清）；确认 3.12 为业务主力（CAD/AI/trading 硬编码依赖）。

## 当前状态与维护约定

- 功能对齐、测试全绿、托盘 + exe 体验闭环、实例运行正常。
- **约定**：修改 `tools/launcher.cs` / `tools/gen_console_ico.py` 后必须重新编译并提交 `LocalOpsConsole.exe`。
- 剩余项：上游 PR 动作（待 Issue 回应）；三个已文档化的产品级取舍（UID/优雅停止/favicon attached）。
