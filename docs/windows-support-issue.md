# [提案] Windows 平台移植支持：sysops 跨平台抽象层 + psutil 唯一依赖

> **Proposal: Windows port — a `sysops.py` cross-platform layer with psutil as the only new runtime dependency. macOS behavior unchanged, zero-dependency promise preserved on macOS.**

## 摘要 / Summary

总控台目前仅支持 macOS。本提案建议通过引入 `sysops.py` 跨平台系统操作层，使项目在 Windows 10/11 上完整可用：

- **macOS 分支**：保持现有 `ps` / `lsof` / `osascript` 实现，运行时**零第三方依赖**的承诺不变；
- **Windows 分支**：基于 `psutil`（唯一新增运行时依赖，`start.bat` 首次启动自动安装，要求 `>= 7.2` 以兼容 Python 3.14）。

目前已有一份可运行的移植实现，并完成全链路验证（task 批处理退出码、项目识别、外部进程认领、favicon/图标、前端交互、优雅停止）。改动遵循**「macOS 行为零变化」**原则。

## 动机与用户场景 / Motivation

- Windows 开发者（个人单用户电脑）同样有"本地服务与批处理任务集中管理"的需求，而 Windows 没有 `ps`/`lsof`/进程组等 Unix 语义，原生工具链缺失；
- 总控台"回环绑定 + 本地页面"的形态非常适合 Windows 个人开发环境（替代手动维护多个终端窗口）；
- 移植不改变产品定位：仍是单机、单用户、本地回环的运维工具，不做远程/多用户。

## 已完成的工作与验证 / Work done

**架构**：新增 `sysops.py` 跨平台操作层，把平台差异收敛到一个文件：

| macOS（原实现，零依赖） | Windows（psutil） |
| --- | --- |
| `ps` / `lsof` 进程与端口扫描 | `psutil` 快照 + `net_connections` |
| 进程组 PGID（`killpg`） | 进程树回溯（root pid 锚点，沿 ppid 向上） |
| `fcntl.flock` 单实例锁 | `msvcrt.locking` |
| `osascript` 原生对话框 | `tkinter` 文件选择 + `ctypes MessageBox` |
| `SIGTERM` / `SIGKILL` | WM_CLOSE 软通道 + `TerminateProcess` 硬杀 |

**验证结果**（Windows 10/11 实测）：
- task 批处理退出码映射：0=成功 / 130=取消 / 非零=失败 / 中止=stopped；
- `detect_project` 项目识别：Node / FastAPI / Flask / Django / Streamlit / 静态站点，命令按平台生成；
- `attached` 外部进程认领：创建原子认领、cwd 同步、拒绝路径全部符合契约；
- favicon 抓取与图标上传/删除；
- 前端交互冒烟（视图切换、命令面板、新端口发现、批量停止、添加表单）；
- Windows 优雅停止（先软后硬）、restart 后日志完整可查。

**顺带修复**：`detect_project` 此前硬编码 `python3`（macOS 命令名），现按平台生成；Windows 重启后新实例日志丢失问题已修复（`--log-to-file`）。

**测试**：`tests/test_hardening.py` 46 项在 Windows 通过；完整套件 163 项中 130 通过，剩余 33 项失败均为 macOS 平台行为断言（`/bin/bash`、`python3`、POSIX symlink 等），合并前可统一用 `sysops.IS_POSIX` 条件化。

## 兼容性与安全影响（请重点审阅）/ Compatibility & security trade-offs

遵循贡献指南"不得削弱安全校验"的要求，以下妥协必须由维护者确认：

1. **UID 语义失效**：Windows 没有 Unix uid，`SELF_UID` 恒为 0——「只能操作当前用户进程」的校验在 Windows 上不生效。**建议仅限个人单用户电脑使用**（README 已声明），不在共享主机使用。
2. **进程组 → 进程树**：以 root pid 为锚点沿 ppid 回溯，语义等价，但边角场景（孤儿进程、PID 复用）判定能力弱于 PGID。
3. **信号模型**：Windows 无 SIGTERM；带窗口进程走 WM_CLOSE 软通道，无窗口服务只能硬杀——优雅停止能力弱于 macOS。
4. **CPU 口径归一化**：Windows 按「占全部逻辑核心百分比」（任务管理器口径），`/api/state` 新增 `coreCount` 字段；macOS 保持单核口径不变。
5. **Shell 包装**：macOS 用 bash 双层包装，Windows 用 `cmd /c "echo <marker> & <command>"`——`service` 需前台命令，shell 语法与 macOS 有差异。

## 分阶段提交建议 / Proposed merge strategy

为避免一次性大改动，建议分三个 PR：

1. **PR 1 — 纯重构**：引入 `sysops.py`，把 server.py 的平台调用收口到抽象层，**macOS 行为零变化**（低风险，先行合并建立信任）；
2. **PR 2 — Windows 支持**：psutil 分支实现 + 平台文档 + 测试条件化（`sysops.IS_POSIX`）；
3. **PR 3 — 启动器体验**：`start.bat`（已有实例时提供「打开/重启/取消」菜单）+ `launcher_check.py`。

## 维护承诺 / Maintenance

愿意在合并后持续跟进 Windows 相关 Issue 与后续维护（Windows 分支的 bug 修复、版本适配）。

## 需要维护者决策的问题 / Questions

1. **psutil 依赖**：作为 Windows 平台唯一运行时依赖是否可接受？（macOS 保持零依赖不变）
2. **UID 妥协**：Windows 上「只能操作当前用户进程」校验失效，按"个人单用户电脑"前提处理是否可以？
3. **合并方式**：接受分阶段提交，还是需要整体评估后一次合入？

---

*本提案不含任何本机路径、配置、token 或未脱敏信息。*
