# 总控台代码审查报告

审查日期：2026-08-17

审查范围：`server.py`、`sysops.py`、Windows 启动链路、发行与项目检查工具，以及相关测试。

审查结论：当前不建议发布 Windows 发行包。发行构建、发行内容和 Windows 脚本健康检查存在 P1 问题；此外，自动启动与已认领服务的身份维护存在可导致重复启动或失去管理状态的 P2 问题。

## 风险摘要

| 优先级 | 数量 | 结论 |
| --- | ---: | --- |
| P1 | 4 | 会阻断发行、导致 Windows 主路径不可用，或在多用户部署前提下破坏本地控制边界。 |
| P2 | 3 | 会在自启动、端口竞争或已认领服务重建时产生错误行为。 |

P1 应在下一次发行前修复。P2 不应阻塞仅 macOS 的内部使用，但应在承诺 Windows 支持前完成。

## 修复跟踪

后续修复已完成，原始发现及其复现依据保留在下文。

| 发现 | 状态 | 修复摘要 |
| --- | --- | --- |
| 1、2、7 | 已修复 | 发行清单补齐 Windows 运行时与启动器；路径泄漏检查允许文档占位路径；项目检查按平台运行。 |
| 3 | 已修复 | Windows 用 TokenUser SID 校验进程归属；所有写接口要求当前用户私有 `control.token` 对应的能力令牌。 |
| 4 | 已修复 | Windows 简单命令解析支持 Python、批处理和 PowerShell 脚本路径，并对动态 CMD 语法安全降级。 |
| 5 | 已修复 | 手动启动、自启动与重启后的再次启动共用应用锁和启动前事务。 |
| 6 | 已修复 | 已认领服务仅对记录 PID 校验创建时间，替代监听 PID 按端口、SID 与 cwd 唯一重关联并持久化。 |

修复后已在 Windows 上通过后端、硬化、前端、发行、项目检查和端口归一化测试；未在 macOS 上实际运行 `.app` 启动器。

## 详细发现

### 1. [P1] 发行检查把文档和注释中的示例路径误判为本机路径泄漏

位置：`tools/build_release.py:275`、`CHANGELOG.md:62`、`static/js/core.js:59`

`find_path_leak()` 对所有待发行文件的原始字节进行路径模式扫描，无法区分实际写入的本机绝对路径与文档、注释中的路径示例。当前仓库中的变更记录和前端注释会命中该规则，因此即使源码没有泄漏当前开发者目录，发行检查仍会失败。

复现：在 Windows 上执行 `python tools\build_release.py --check-only`，进程以退出码 `1` 结束。

影响：发行流水线无法通过，且开发人员若为恢复发行而放宽规则，可能反而削弱真正的路径泄漏检查。

建议：将检查对象限定为需要防泄漏的生成物或运行时配置；对于 Markdown、源代码注释等允许包含示例路径的文件，采用语义白名单或更窄的上下文匹配。为“真实当前用户目录必须失败”和“文档示例必须通过”各增加回归测试。

### 2. [P1] Windows 发行包缺少运行时和启动器文件

位置：`tools/build_release.py:41`、`server.py:33`

发行文件清单 `INCLUDE` 包含 `server.py`，但未包含其无条件导入的 `sysops.py`。它也没有包含 Windows 入口与依赖说明：`start.bat`、`launcher_check.py`、`tray.py`、`requirements-runtime-win.txt`，以及 Windows 分发使用的 `LocalOpsConsole.exe`、`console.ico`。

影响：解压出的 Windows 包至少会在导入 `server.py` 时因缺少 `sysops.py` 失败；即使手工补齐该文件，也没有受支持的 Windows 启动、依赖安装和托盘运行链路。

建议：按平台定义完整、可执行的发行清单，并在构建后将压缩包解压到临时目录，分别执行 Windows 启动冒烟检查和 macOS 启动器完整性检查。`sysops.py` 应作为服务器的强制运行时文件，而非依赖人工维护的可选项。

### 3. [P1，取决于部署前提] Windows 多用户环境缺少有效的本地控制权限边界

位置：`server.py:3023`、`sysops.py:194-213`

写接口允许没有 `Origin`、`Sec-Fetch-Site` 和 Cookie 的本地命令行请求继续执行，以兼容 headerless CLI。与此同时，Windows 的 `process_uid()` 将任意存活进程视为 UID `0`，因此“仅允许当前用户进程”的校验在 Windows 上无法区分账户。

在同一台 Windows 主机存在多个登录账户、远程桌面会话或不可信本地程序时，其他本地账户可以连接 loopback 控制接口，并以总控台所属账户的权限请求启动命令、修改配置或影响进程。项目文档已将 Windows 版本限定为个人单用户电脑；若这是可强制执行的部署边界，可将其视为明确接受的风险。若要支持共享电脑、企业终端或多会话场景，这是一项安全漏洞。

建议：为所有变更请求建立真正的本地客户端身份校验，例如要求受保护的随机控制令牌，并以 Windows SID/会话而非恒定 UID 验证进程归属。CLI 兼容模式不应默认绕过认证；若必须保留，应使用只有当前用户可读取的令牌文件或受 ACL 保护的 IPC 通道。

### 4. [P1] Windows 的“选择脚本”生成命令与健康检查解析器不兼容

位置：`server.py:1784`、`server.py:1835`、`server.py:1880`

`command_for_script()` 在 Windows 上会生成 `python.exe -- <path>`、批处理文件路径等 CMD 风格命令；但 `_simple_command_tokens()` 固定采用 POSIX `shlex` 解析。反斜杠会被当作转义字符删除，且 `_script_target()` 的 Python 可执行文件匹配不接受 `python.exe`。

已验证的结果包括：选择现有的 `start.bat` 后，解析路径变为 `D:A_programlocal-opsstart.bat` 并被错误报告为缺失；配置一个不存在的 `.py` 脚本时，健康检查又可能报告为正常。

影响：Windows 用户无法可靠使用“选择脚本”入口，阻断型配置问题会被误报或漏报，启动台的禁用和诊断行为也随之不可信。

建议：为 Windows 实现受限但正确的 CMD 命令解析器，或将由 `command_for_script()` 生成的命令以结构化字段保存并直接检查，避免再反解析 shell 字符串。同时支持 `.exe` 形式的 Python 解释器，并覆盖 `.py`、`.bat`、`.cmd`、`.ps1` 以及包含空格的路径的正反例测试。

### 5. [P2] 自启动路径绕过常规启动的端口检查和并发串行化

位置：`server.py:3648`、`server.py:4126`

普通 `POST /api/apps/{id}/start` 通过 `serialized_app_operation` 串行化，并在启动前检查端口是否被占用。`_autostart_managed_apps()` 仅做健康检查后直接调用 `start_app()`，没有复用这两个保护。

影响：总控台启动期间，服务可能在端口已被外部进程占用时仍被拉起；自启动线程与用户手动启动同一应用也可能穿透各自的“尚未运行”检查，形成重复启动或竞争。

建议：提取一个唯一的受控启动入口，统一负责应用锁、运行状态复核、健康检查、端口占用检查和 `start_app()` 调用。自启动与 HTTP 请求都只能经由该入口执行；补充并发启动和启动时端口竞争的回归测试。

### 6. [P2] 已认领 Windows 服务在监听 PID 更替后会丢失身份，原子创建也漏写创建时间

位置：`server.py:1144-1150`、`server.py:2465`、`server.py:3503-3553`

已认领服务允许监听子进程更换 PID，但候选 PID 都要与原 `lastCreateTime` 比较。新监听进程的创建时间必然不同，因此会被全部排除，违背“按端口、当前用户和真实 cwd 唯一重新关联”的设计。另一个不一致是：手动认领会保存 `identity["ctime"]`，而创建卡片时携带 `attachPid` 的原子认领没有写入 `lastCreateTime`。

已验证：替换已认领服务的监听 PID 后，身份解析返回 `None`；通过“创建并认领”路径创建的应用，其 `lastCreateTime` 为 `None`。

影响：服务的正常重载、热重启或子进程替换后，卡片会显示为未运行，并把仍在监听的真实服务当作外部端口占用。直接创建认领还失去 PID 复用保护。

建议：仅在仍验证记录 PID 时使用其创建时间锚点；对于已认领服务的替代 PID，应按端口、Windows SID/当前用户、真实 cwd 的唯一性重新关联，并原子更新 PID 与创建时间。创建并认领与手动认领应共用同一身份持久化函数。

### 7. [P2] 项目检查工具在 Windows 上无条件依赖 macOS 工具

位置：`tools/check_project.py:394-403`

`check_shell_and_plist()` 无条件调用 `/bin/bash -n` 和 `plutil`，这两个依赖不属于 Windows 运行环境。`--skip-tests` 只跳过测试，没有跳过该 macOS 专属检查。

复现：在当前 Windows 环境运行 `python tools\check_project.py --skip-tests` 失败。

影响：项目文档中可用于提交前验证的检查命令无法在其声称支持的平台运行，Windows 回归无法形成可靠门禁。

建议：按平台拆分检查项。macOS 保留 shell 语法和 `Info.plist` 校验；Windows 改为验证 `start.bat`、启动器依赖与运行时清单，或在不适用时明确跳过并在输出中说明原因。

## 验证记录

以下测试在审查期间通过：

- `python -m unittest tests.test_server -q`
- `python -m unittest tests.test_hardening -q`
- `python -m unittest tests.test_frontend tests.test_release tests.test_project_checks tests.test_platform_leaks -q`
- `node --test tests\js\ports.test.mjs`（7/7 通过）

以下面向交付或跨平台的检查失败：

- `python tools\build_release.py --check-only`
- `python tools\check_project.py --skip-tests`

另外，`ruff check` 报告 `tools/check_platform_leaks.py:118` 的 `E741`（易混淆变量名）。这是非阻断的代码风格问题，未单列为功能缺陷。

审查时 `git status --short` 和 `git diff --check` 均无输出，工作树没有待提交改动。

## 建议修复顺序

1. 修复发行检查的误报并补全 Windows 发行清单；增加“解压后可启动”的平台级测试。
2. 修复 Windows 命令健康检查，使选择脚本、阻断提示和诊断结果一致可信。
3. 明确 Windows 的支持边界；若不严格限制为单用户设备，实现请求和进程操作的 Windows 身份校验。
4. 统一手动启动与自启动的启动前检查和锁，消除端口及并发竞态。
5. 修复已认领服务的 PID 重关联与创建时间持久化，并补覆盖热重启的测试。
6. 使项目检查工具按平台执行，保证 Windows 的提交前检查可用。

## 审查限制

本次在 Windows 环境完成，未实际运行 macOS 启动器或在真实多账户 Windows 会话中进行攻击演练。第 3 项的风险判断基于 loopback 接口认证逻辑和 Windows 进程归属实现；其严重性取决于“个人单用户电脑”是否为可执行的产品部署限制，而不仅是文档说明。
