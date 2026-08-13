# 上游贡献映射（Upstream PR Map）

> 目的：把本地 Windows 移植的全部改动分为「可回馈上游的通用修复」与「Windows 专属增强」，
> 为将来向 laogou717/local-ops 提交 PR 提供依据。更新日期：2026-08-13。

## 一、可回馈上游的通用修复（平台无关，上游直接受益）

这些修复不依赖 Windows 支持，纯属缺陷修复或改进，可独立成 PR：

| Commit | 内容 | 上游价值 |
| --- | --- | --- |
| `54ea686` | **Windows 单实例锁缺陷**（锁位置依赖 pid 长度导致锁错位、未获锁进程崩溃） | msvcrt 分支的正确性——若上游未来接受 Windows 支持必须修复；macOS flock 分支不受影响 |
| `27d29dc` 内 | **detect_project 命令平台化**（`PYTHON_CMD` 常量，Windows 用 python / macOS 用 python3） | 上游当前硬编码 python3；引入常量后为将来 Windows 支持铺路，macOS 行为不变 |
| `9101cff` | **测试平台条件化**（`IS_POSIX` skipUnless + `PYTHON_CMD` 断言 + 权限位断言条件化） | 上游在 macOS 上运行全部不受影响（跳过项在 macOS 不触发）；让测试具备跨平台能力 |
| `9101cff` 内 | 测试基建修复（`mock.patch.dict(os.environ)` 环境块上限） | 通用测试健壮性 |
| `b5d78c7` | **shortHome 支持 Windows 路径**（`C:\Users\<name>` 缩写 `~`） | macOS 行为不变，纯增强 |

## 二、Windows 专属（仅在合入 Windows 支持后有价值）

| Commit | 内容 | 备注 |
| --- | --- | --- |
| `27d29dc` | **sysops.py 跨平台抽象层**（psutil 实现、进程树回溯、msvcrt 锁、tkinter/ctypes 对话框、WM_CLOSE 终止） | **PR1 的核心**：先以「纯重构、macOS 行为零变化」形态提交，Windows 实现可放 PR2 |
| `d5ba7f6` | 优雅停止软通道、restart `--log-to-file`、启动器菜单 | Windows 专属 |
| `8ff5593` / `786c8dd` | start.bat 修复与跨机器加固 | Windows 专属 |
| `28a0224` | 快捷键 `MOD_KEY` 平台自适应、`IS_MAC` 常量 | macOS 显示不变；Windows 生效 |
| `493cb71` | 设置中心 `dataDir/logsDir` 动态化、前端 fallback 平台化、路径分割 | macOS 显示不变（真实路径）；Windows 生效 |
| `9101cff` | WindowsInstanceLockTests 等 Windows 测试 | Windows 专属 |

## 三、建议的 PR 顺序

1. **PR1（纯重构）**：`sysops.py` 抽象层——把 server.py 的平台调用收口，macOS 行为零变化。这是最易合、建立信任的一步。
2. **PR2（修复+增强）**：单实例锁修复 + shortHome + 测试条件化（可回馈项，无 Windows 依赖）。
3. **PR3（Windows 支持）**：sysops 的 psutil 实现 + detect_project 平台化 + 文档 + Windows 测试（须等维护者确认 Windows 方向）。
4. **PR4（体验）**：start.bat/launcher_check.py、快捷键平台自适应、设置中心路径动态化。

## 四、红线提醒

- **绝不提交**：HANDOVER.md、AGENTS.md 中含本机路径的内容、`.workbuddy/`、`data/`、`__pycache__`、个人 token/路径。
- 从 **upstream main** 重新建分支（不要直接推本地 main）。
- 每个 PR 保持单一主题、含 CHANGELOG 条目、说明验证方式。
- 与维护者对齐后再提交大 PR（参考 Issue #1 讨论）。
