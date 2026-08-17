# Windows 功能对齐验证（Parity Check）

> 目的：确认 Windows 移植版覆盖 macOS 原版的全部功能，不新增、不缺失。
> 验证日期：2026-08-13。结论：**功能完全对齐，无缺失**。

## 一、API 层（100% 对齐）

- 顶层路由：原版 13 条（/api/state、/health、/apps、/apps/reorder、/kill、/pick、
  /project/detect、/services/flag、/watch、/console/{log,restart,stop}、/ui/theme）
  → 当前 13 条，**0 缺失**。
- handler 方法：原版 22 个 handle_* → 当前 22 个，**0 缺失**。
- 动态路由（/api/apps/{id}/start|stop|restart|diagnose|attach|icon|favicon|logs、PUT/DELETE）全齐。

## 二、功能清单（README 8 条逐项核对）

| # | 功能 | 对齐 | 验证方式 |
| --- | --- | --- | --- |
| 1 | 每 2 秒监控服务/CPU/内存/时长 | ✅ | /api/state 轮询 + 前端渲染（前端冒烟） |
| 2 | 服务/任务集中管理（启停/重启/日志/诊断） | ✅ | task 退出码、attach、favicon、图标、批量停止全程验证 |
| 3 | 新端口发现 + 加入启动台 | ✅ | 前端冒烟（8015 新端口出现 + 加入） |
| 4 | 运行前检查 + 修复入口 | ✅ | diagnose 测试、健康检查 |
| 5 | 项目识别 | ✅ | detect_project 6/6 |
| 6 | 受控进程识别（token+进程组+UID） | ✅ 等价 | Windows 用进程树回溯替代 PGID，并以 TokenUser SID 校验当前用户 |
| 7 | 单一 Ops 主题 + 深浅色跟随 | ✅ | 本次验证 light→dark 切换（html[data-theme]） |
| 8 | 命令面板 + 拖拽/键盘排序 | ✅ | 命令面板冒烟；reorder API 本次验证；拖拽为 pointerdown 自定义实现 |

## 三、前端交互（累计验证）

视图切换、命令面板（打开/过滤/执行）、新端口发现、批量停止、添加服务表单、
日志中心、设置中心（版本/真实数据目录）、主题切换、reorder 排序、快捷键 Ctrl 提示、
无 JS 错误 —— 全部通过。

## 四、平台语义差异（非缺失，已在 README 支持矩阵文档化）

CPU 口径（全核百分比）、优雅停止（WM_CLOSE+硬杀）、Shell 包装（cmd /c，禁单引号）、
快捷键显示（Ctrl vs ⌘）、数据目录（%APPDATA%）。Windows 进程归属使用 TokenUser SID，
本地写接口还由私有能力令牌保护，不再以“UID 校验失效”作为单用户妥协。

## 五、结论

Windows 移植版**功能上与 macOS 原版完全对齐**：API 全量覆盖、README 功能清单
逐项达成、前端交互全部验证。差异仅为平台语义的有意取舍，均已文档化。
后续上游合流或回归时，可重跑本清单确认对齐状态。
