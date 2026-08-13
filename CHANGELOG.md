# 变更记录

本项目的重要变更记录在此。格式参考 Keep a Changelog，版本号使用语义化版本。`VERSION` 是当前版本的唯一权威来源。

每个面向用户的重要功能、修复、安全或兼容性变化先写入
`Unreleased`；发布时再移动到带版本号和日期的章节。纯缓存清理、
一次性构建产物和不影响行为的内部整理由 Git 历史记录，不在此逐项罗列。

## [Unreleased]

### Added

- **Windows 平台移植**：新增跨平台系统操作层 `sysops.py`，macOS 保持零第三方依赖（ps/lsof/osascript），Windows 基于 `psutil` 实现进程快照、监听端口、工作目录与进程树管理；新增 `start.bat` 启动器与 `requirements-runtime-win.txt` 运行时依赖说明。`start.bat` 采用 `pythonw.exe` **无窗口后台运行**，日志写入 `%LOCALAPPDATA%\总控台\console.log`；新增 `--log-to-file` 命令行参数。
- **Windows 平台优化（对齐 macOS）**：① 服务监控按系统进程名单（svchost/System/explorer 等）与 System32 目录将系统进程归入「应用后台」折叠，消除界面噪音；② 进程溯源新增 Windows 工具别名映射（Code.exe→VS Code、Cursor、WindowsTerminal/cmd/powershell→终端 等），并修复含空格路径（如 `Microsoft VS Code`）的溯源解析——psutil argv 拼接时对含空格参数补引号；③ **补全性能监控 CPU 采样**：Windows 版每进程 CPU 由恒 0 改为「跨快照 cpu_times 差分」，并按 Windows 用户习惯归一为「占全部逻辑核心的百分比」（任务管理器口径，吃满 1 核 = 100/核心数 %，多线程也不会超过 100），`/api/state` 新增 `coreCount` 供前端迷你条还原相对满核宽度；macOS 保持单核口径不变；排除 System Idle Process（pid 0）。
- 顶栏新增 GitHub 仓库图标按钮，点击在新标签页打开项目源码仓库。
- 增加用户/开发文档、备份恢复和升级卸载指南。
- 布局升级为指挥台结构：左侧图标导航轨、启动台与服务监控双视图 KPI 概览卡（含 CPU/内存火花线）、右侧实时动态/实时告警与端口/资源 TOP 5 信息栏、小贴士、页头快捷操作，以及服务/任务分区筛选芯片；服务表格增加 PID、状态列与 CPU 迷你负载条。结构样式集中于 `base.css`。
- 导航轨与侧栏补齐聚合能力：日志中心弹层（⌘J 呼出，应用与总控台日志目录页，⌘L 为浏览器保留键故用 ⌘J）、设置中心弹层（任务完成通知开关、浅色/深色/自动外观、版本与目录信息）、快捷操作部件的批量停止服务（确认后逐个安全停止，绝不按端口结束进程）。
- 服务表格新增**进程溯源**：沿 PPID 链识别并显示每个服务的启动者（AI 编程助手/编辑器/终端/总控台），副标题行展示来源图标与名称。
- 新增「Ops 指挥台」为唯一 UI 主题（深空蓝黑/雾灰双色、柔和圆角细边、蓝色强调），主题清单中固定排首位；保留 `#themeCss` 整包加载与 `uiTheme` 配置机制，但不再提供多主题与主题选择界面。
- 增加统一项目检查入口、显式测试发现和发布核对表。
- 增加项目权利声明与第三方素材清单。
- 增加根目录 `VERSION` 统一版本源，`/api/state` 暴露版本/schema/降级信息，并增加不执行进程扫描的 `/api/health`。
- 增加 `schemaVersion=1` 和显式、幂等的逐版配置迁移器。
- 增加 `SECURITY.md`、`CONTRIBUTING.md`、社区行为规范以及 GitHub Issue/PR 模板。
- 增加 `ASSET_PROVENANCE.md`，并用路径、SHA-256 与发布状态检查覆盖字体、品牌图片、插画和程序化纹理。
- 增加统一品牌标识、网页 favicon、Apple Touch Icon、macOS App Icon 与可重建的品牌导出脚本。
- 命令面板增加“添加服务”和“添加批处理任务”入口；应用卡片增加可取消的键盘排序。
- 服务监控增加会话级新端口发现栏，可将新监听服务加入启动台、忽略隐藏或暂时关闭。
- 应用状态增加只读配置健康检查，在运行前识别丢失的工作目录、脚本和运行时，并提供修复入口。

### Changed

- 默认将配置/图标移至 `~/Library/Application Support/总控台`，日志移至 `~/Library/Logs/总控台`。新目标不存在时仅首次复制旧 `data/`，不删除原文件。
- `config.json.bak` 现保留修改前的上一份良好配置，而不是与主配置相同的副本。
- 运行目录权限收紧为 `0700`，配置、图标和日志文件为 `0600`。
- 项目自有代码和文档改用 MIT License；README 明确 Preview / Alpha、源码预览和非远程运维边界。
- 公开发行检查会拒绝仍标记为 `BLOCKED` 或 `TO_REPLACE` 的素材，并要求对 `REVIEW_REQUIRED` 项形成人工结论。
- 表单把外观设置收进可选区域，服务/任务分别优先聚焦“选择项目”和“选择脚本”。
- 修正 900px 附近顶栏导航异常放大，并补齐移动端、高对比度、键盘焦点和表格语义。
- 移除来源和再分发链路不完整的精简中文字体，改用 macOS 系统字体栈。
- 批处理结果改为成功、取消、失败、中止四态；脚本内部用户取消统一使用退出码 130。
- 任务运行中的动作统一使用“中止”，服务继续使用“停止”，诊断和编辑提示随类型变化。
- 多个启动配置现在可以共享同一端口；项目归属由受管进程身份和工作目录判断，只有实际启动时的监听占用才会阻止运行。

### Fixed

- **系统托盘图标（Windows，纯 ctypes 零依赖）**：总控台运行时在系统托盘显示品牌图标，tooltip 显示「总控台 · 127.0.0.1:9600 · 运行中」；左键单击打开控制台，右键菜单提供「打开控制台 / 重启总控台 / 停止总控台 / 退出」。图标内嵌 PNG 解码（zlib 标准库）→ CreateIcon 生成 HICON，不依赖任何外部图标文件或第三方库；总控台停止时自动销毁（NIM_DELETE）。macOS 保持原有行为（无托盘，与上游一致）。
- **设置中心数据/日志目录显示真实路径**：此前硬编码 macOS 路径 `~/Library/Application Support/总控台`，Windows 用户会看到错误路径。现 `/api/state` 新增 `dataDir`/`logsDir`（服务器实际路径，平台无关），设置中心动态填充。
- **前端 fallback 命令平台化**（`overlays.js`）：旧后端兜底的 `fallbackScriptCommand` 此前硬编码 macOS 语义（python3/zsh/bash + 单引号）；现按 `IS_MAC` 分支——Windows 用 `python -- "path"`/powershell/bash + 双引号（cmd 不识别单引号），`.bat/.cmd` 直接执行。
- **Windows 路径分割修复**：`p.lastIndexOf('/')`/`p.split('/')` 对反斜杠路径失效（目录与文件名提取错误），改为同时识别 `\` 与 `/`。
- 新增 `core.js` `IS_MAC` 常量（`MOD_KEY` 基于它），供前端各平台分支复用。
- 修复 Windows 单实例锁两处缺陷：①锁位置依赖 pid 字符串长度（先写 pid 再在其后锁 1 字节），两个实例 pid 位数不同时会锁到不同字节导致单实例失效、双实例并发写同一配置；现改为先锁固定字节 0 再写 pid。②未获锁的进程在 `write/flush` 阶段抛 `PermissionError` 且 `except` 内 `close()` 再次抛出导致崩溃；现优雅返回 None（打印"已在运行"），`close` 二次保护。并发三进程实测 1 持锁 + 2 优雅拒绝、释放后重取正常。
- 修复 `shortHome` 只识别 macOS `/Users/` 路径的问题：Windows 的 `C:\Users\<name>` 现同样缩写为 `~`（目录显示不冗余）。
- **测试套件 Windows 全绿**：165 项通过 + 26 项跳过（macOS 专属语义），0 失败。新增单实例锁回归测试（并发互斥+优雅拒绝+释放重取）；macOS 专属用例（lsof 解析/bash 包装/chmod 执行位/symlink/发布产物检查）按 `IS_POSIX` 条件跳过；跨平台断言引用 `PYTHON_CMD`；修复 `mock.patch.dict(os.environ)` 在 Windows 的环境块 32767 上限问题。
- AGENTS.md 补齐 Windows 移植说明：sysops.py 跨平台层、start.bat/launcher_check.py、数据目录（`%APPDATA%`）、平台差异章节（UID 失效/CPU 口径/优雅停止/Shell 包装/快捷键提示/测试策略）。
- 修复前端快捷键提示硬编码 macOS 符号（⌘K/⌘J/⌘V）的问题：新增平台检测常量 `MOD_KEY`（macOS 显示 ⌘、Windows/Linux 显示 Ctrl），命令面板触发器、日志中心快捷项、图标粘贴提示、小贴士与命令面板 hint 全部改为平台自适应渲染。快捷键逻辑本身已兼容两平台（`metaKey || ctrlKey`），本次仅修正提示文案。
- 修复 `start.bat` 首个 Python 分支（固定路径 `python.exe` 且 psutil 可用）下 `%PY%` 从未赋值的问题：探测与菜单动作会以空前缀直接执行 `.py` 文件，Windows 按 `.py` 文件关联（ShellExecute）打开且输出重定向失效，实例探测误判为未运行（菜单永不出现），关联不完整时还会弹出"选择使用什么程序打开 .py"。现探测/打开/重启统一显式调用 `"%PYEXE%"`，启动改为直接调用 `"%PYW%"`（pythonw 为 GUI 子系统，cmd 不等待，无需 `start` 包装）。同步将该分支改为 goto 结构（不再使用括号块）。
- 修复 Windows 下 `detect_project` 生成 macOS 命令名的问题（同下）之外：**restart helper 启动的新实例缺少 `--log-to-file`**，pythonw 无控制台场景下输出写入无效句柄，重启后实例日志不可见且无法诊断异常；新实例现带 `--log-to-file`，重启链路日志完整可查。
- 修复 Windows 下项目识别（`detect_project`）生成 macOS 命令名的问题：Django/FastAPI/Flask/Streamlit 与静态站点兜底的 Python 启动候选现按平台使用 `python`（Windows）/ `python3`（macOS）；模块缺失诊断中的虚拟环境建议同步平台化（Windows 使用 `.venv\Scripts\pip`）。对应测试改为引用 `PYTHON_CMD` 常量跨平台断言。
- Windows 运行时依赖锁定 **psutil >= 7.2**（Python 3.14 兼容所需），`start.bat` 自动安装与 README 安装说明同步版本约束。
- 修复 Windows 下 `/api/state` 慢扫描与配置写入反向加锁导致总控台永久无响应的问题；状态轮询改为保留旧快照的单飞后台刷新，并缩小目标 PID 与来源祖先链扫描范围。受管进程停止改为按冻结成员列表从叶子到根终止，日志权限设置兼容无 `os.fchmod` 的 Windows Python，带空格的可执行路径不再被 `cmd.exe` 的二次转义破坏。

### Changed

- **Windows 优雅停止**：停止流程先走 WM_CLOSE 软通道（`taskkill` 无 `/F`，带窗口的服务可自行清理落盘），短暂宽限后对仍存活成员执行硬杀兜底；force 语义不变。`kill_process` 的非 force 分支同样先软后硬，目标已退出时视为成功（幂等）。
- **启动器交互**：新增 `launcher_check.py`（探测实例/打开控制台/重启控制台，输出纯 ASCII）；`start.bat` 检测到已有实例时显示「打开 / 重启 / 取消」菜单，对齐 macOS launcher 行为。
- **启动器跨机器加固**：`start.bat` 的 Python 探测顺序改为「py launcher（自动选最新）→ PATH 中的 python → 固定路径兜底」，不再优先硬编码本机路径；每个候选同时校验 Python ≥ 3.12 与 psutil，版本不足时给出明确提示（不再误报为 psutil 安装失败）。README 补充 Windows 开发环境的 `make` 等价命令。
- 修复从服务监控加入启动台时只创建卡片、未认领来源进程的问题；创建与进程认领现由后端原子完成，项目命令识别完成前不能提前保存。明确认领的服务在 Next/Vite 等框架重建监听子进程、PID 变化后，会按端口、当前用户与真实项目目录唯一重新关联。
- 修复 Candy 主题超大标题的英文粗体描边出现双重轮廓，并让英文副标题在窄屏明确换行。
- 修复批处理脚本内取消被误报为运行成功，以及任务成功退出被诊断成“服务过早退出”。
- 重启服务前先检查当前配置，避免脚本或目录失效时先停止仍在工作的旧进程。
- 修复外部进程碰巧监听停止卡片的配置端口时被误认成该卡片、且不会触发新端口发现的问题；端口诊断增加打开占用服务和修改原卡片两种非破坏性处理方式。
- 修复测试服务器、热重载等短命监听停止后仍长期残留在“发现新的监听端口”列表的问题。
- 修复从服务监控把正在运行的进程加入启动台后，新卡片反而把来源进程识别成端口占用者的问题；保存时现在会立即认领来源 PID。

### Removed

- 移除已被统一品牌图、Candy 新插画和系统字体栈替代的旧 Logo、旧插画及两份中文字体文件。
- 移除 Apollo/Candy/8-Bit 三套 UI 主题与主题选择面板、命令面板主题切换项及 Candy 专用 hero 卡；产品收敛为单一「Ops 指挥台」主题，旧的 `uiTheme` 偏好自动回退到 ops。
- 随主题移除不再使用的 Apollo 程序化纹理（deck/metal-brush 系列）、Candy 启动台插画与 `tools/gen_textures.py`；`ASSET_PROVENANCE.md` 与 `THIRD_PARTY_NOTICES.md` 同步核销。

### Security

- 将用户配置、日志、图标、token 和临时发行产物排除出版本控制默认范围。
- 主配置与备份均无法验证时进入只读保护，防止用空默认配置覆盖尚可恢复的用户数据。
- 增加私密漏洞报告、Issue/PR 脱敏和公开仓库安全披露门禁。

## [1.0.0] - 2026-07-23

### Added

- Python 3 标准库本地 HTTP 后端，只绑定 `127.0.0.1`。
- 启动台：服务与批处理任务的创建、编辑、排序、启动、停止、重启、日志与诊断。
- 服务监控：端口、进程、CPU、内存、运行时间、关注关键字与分组。
- 读取常见项目配置并提供候选启动命令的本地项目识别。
- 基于 run token、进程组和 UID 的受控进程识别。
- 原子配置写入、同步备份、有界日志读取与轮转。
- Apollo/Candy 双 UI 主题、深浅色模式、命令面板和原生 macOS 文件选择。
- `总控台.app` 后台启动器与 `start.command` 调试入口。
