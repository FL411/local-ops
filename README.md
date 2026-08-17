# 总控台（Windows 移植版）

> **本仓库是 [laogou717/local-ops](https://github.com/laogou717/local-ops)（总控台）的 Windows 移植衍生版**，面向 Windows 10/11 用户：完整保留原版全部功能，并额外提供系统托盘、无窗口启动器。
> **macOS 用户请直接使用上游仓库**，本仓库以 Windows 为使用目标。

## 亮点

- **系统托盘**：后台常驻品牌图标，左键打开控制台，右键菜单「打开 / 重启 / 停止 / 退出」，tooltip 实时显示运行状态——摆脱命令行窗口。
- **无窗口 exe 启动器**：双击 `LocalOpsConsole.exe` 即可后台启动，体验等同 macOS 的 `.app`。
- **功能完全对齐**：API 层 14 条路由 + 23 个 handler 与上游 100% 覆盖，无功能缺失。
- **205 项测试全绿**：单实例锁、托盘、平台泄漏扫描、控制令牌与 ACL 保护均已回归固化。
- **顺带修复上游缺陷**：单实例锁失效、`detect_project` 硬编码 `python3`、Windows 重启后日志丢失等。

**Preview / Alpha · 源码预览**

总控台是一个本地服务与批处理任务快速启动、运行监测工具。它把常用项目命令、长期服务和一次性批处理任务集中到本地网页中，并用 Python 3 提供只绑定回环地址的后端（macOS 仅用标准库；Windows 额外依赖 psutil）；前端是无构建、无 CDN 的原生 HTML/CSS/JavaScript。

> 当前版本仍处于 Preview / Alpha 阶段，以源码预览形式提供。接口、配置格式和安装方式仍可能调整。Windows 平台为移植版本，核心功能（服务启停、进程溯源、日志、诊断、新端口发现、控制令牌）已验证可用。

总控台只服务当前电脑和当前用户，不是远程运维、多人协作或公网管理面板。它能够以当前用户权限执行保存的 shell 命令；不要将监听地址、反向代理、SSH 隧道或端口映射暴露到不受信任的网络。

## 维护说明

总控台由作者个人维护：功能的新增、修改与完善以作者日常使用中的实际需求为准，迭代节奏不定；PR 不承诺审阅或合入。

如果你希望增加功能、修复问题或适配其他平台，欢迎 **Fork 本仓库自行修改**，并在 Discussions 中提交衍生版本说明。经过试用评估后，优秀的衍生版本会收录到下方 [社区衍生版本](#社区衍生版本) 列表推荐给大家；衍生版本由各自作者维护，未经原作者审阅或测试，使用前请自行评估。

## 功能

- 每 2 秒查看当前用户的本地监听服务、CPU、内存和运行时长。
- 保存常用服务或批处理任务，集中启动、停止、重启、查日志和诊断。
- 在当前页面会话中发现新出现的、尚未管理的监听端口，可直接加入启动台或忽略隐藏。
- 运行前检查工作目录、脚本和运行时；明确失效时直接给出修复入口，不必先失败一次。
- 从项目文件夹识别常用启动命令，但不安装依赖、不执行项目代码。
- 通过运行 token、进程组和当前 UID 联合识别受控进程，不会因端口相同就杀死外部进程。
- Ops 指挥台单一主题：深空蓝黑/雾灰双色，左侧导航轨、KPI 概览卡、实时动态侧栏，浅色、深色和跟随系统。
- 全局命令面板可直接添加服务或批处理任务；启动台卡片支持鼠标拖拽和键盘排序。

## 界面预览

以下截图使用脱敏演示数据，不包含真实用户名、目录、命令或服务信息。

| 启动台 | 服务监控 |
| --- | --- |
| ![Ops 指挥台 · 启动台](docs/screenshots/ops-launchpad.jpg) | ![Ops 指挥台 · 服务监控](docs/screenshots/ops-services.jpg) |

## 系统要求

- **Windows 10/11**：运行时依赖 `psutil`（≥ 7.2，Python 3.13/3.14 兼容所需；首次运行 `start.bat` 自动安装，或 `pip install "psutil>=7.2"`）。
- Python 3.12 或更高版本。
- Chrome、Edge 或其他支持 ES Modules 的现代浏览器。

`VERSION` 是项目版本的唯一权威来源，发行包名和发行说明应与它保持一致。

## 安装

总控台以完整项目目录运行，Windows 使用 `start.bat`（或双击 `LocalOpsConsole.exe`）启动。

### Windows

1. **安装 Python 3.12+**：从 <https://www.python.org/downloads/> 下载安装，安装时务必勾选 **“Add python.exe to PATH”**。
2. **首次运行**：双击 `start.bat`。首次启动会自动安装 `psutil`（Windows 运行时依赖，仅一次），随后打开浏览器进入 `http://127.0.0.1:9600/`。

   > 也可手动运行：`python -m pip install "psutil>=7.2"` 后执行 `python server.py`。
3. 建议将脚本保存在稳定、会单独备份的自动化目录中（参见下文「批处理任务」说明）。

### macOS

> **本仓库不提供 macOS 版本与安装说明**。macOS 用户请直接使用上游仓库
> <https://github.com/laogou717/local-ops>，安装与使用方式见上游 README。

## 运行

### Windows

| 方式 | 操作 | 适用场景 |
| --- | --- | --- |
| **exe 启动器** | 双击 `LocalOpsConsole.exe`（项目根已附带；重新编译见 `tools\build_launcher.bat`） | **推荐**。无窗口、无命令行、双击即用（同 macOS `.app`） |
| 双击脚本 | 双击 `start.bat` | 备用。已运行时直接打开浏览器 |
| 命令行 | `python server.py` | 调试、脚本化（前台运行，Ctrl+C 停止） |

Windows 后台运行说明：

- 启动使用 `pythonw.exe` 无窗口运行，服务在后台常驻，**系统托盘显示品牌图标**（左键打开控制台，右键菜单可打开/重启/停止/退出）；日志写入 `%LOCALAPPDATA%\总控台\console.log`。
- **静默启动（不自动打开浏览器）**：命令行运行 `LocalOpsConsole.exe --no-browser`，或给快捷方式的目标后追加 ` --no-browser`——后台 + 托盘启动，浏览器由你主动打开（点托盘图标或手动访问 `http://127.0.0.1:9600/`）。
- **停止总控台**：打开页面后点击顶栏「停止」（网页按钮，不影响已启动的应用）；或托盘右键「停止总控台」。
- 首次运行会自动安装 `psutil`（≥ 7.2，Windows 唯一运行时依赖）。

### macOS

> macOS 用户请使用上游仓库 <https://github.com/laogou717/local-ops>，本仓库不提供 macOS 运行说明。

两个平台通用的命令行参数：

```bash
python server.py --no-browser        # 只启动服务，不自动打开浏览器
python server.py --preferred-port 9603  # 在 9600-9609 内指定优先端口
```

启动后程序只绑定 `127.0.0.1`，从 9600 起尝试端口，被占用则递增（最多 10 个），并自动打开浏览器。命令行参数、环境变量（`CONSOLE_DATA_DIR` / `CONSOLE_LOG_DIR`）见下文“数据、隐私与备份”。

**实际地址在哪里看**：顶栏「重启 :9600」按钮上直接显示当前端口；或看日志文件（Windows：`%LOCALAPPDATA%\总控台\console.log`）。浏览器手动访问 `http://127.0.0.1:端口号/` 即可。

**停止与重启**：顶栏「重启 / 停止」控制的是总控台自身（网页服务）。停止总控台**不会**停止启动台里已经运行的应用——它们是独立进程组，会继续运行；下次打开总控台时会自动重新识别。重启总控台会加载磁盘上的最新代码，同样不影响运行中的应用。

## 使用

打开页面后，左侧是导航轨，右侧是信息栏；所有数据每 2 秒自动刷新。

### 启动台（管理你的服务与任务）

- **添加服务/任务**：点「+ 添加服务」卡片或页头快捷按钮。选择工作区文件夹后会自动识别项目类型（Node/pnpm、Hexo/Hugo、Django/FastAPI、Go、Rust、静态站点等）并给出候选命令；也可以「选择脚本」或完全手动填写。`service` 是长期服务（带端口语义），`task` 是有明确结束时间的批处理（强制无端口）。
- **卡片**：大按钮启动/停止（任务是运行/中止）；右侧一排小按钮（复制链接/日志/诊断/重启/编辑/删除）常显，不用悬浮。运行中显示端口与时长；配置失效（目录/脚本丢失）会直接标出原因并禁用启动，点开「启动诊断」有修复建议。
- **筛选**：每个分区右上角可按 全部/运行中/已停止/异常（任务为 全部/运行中/成功/失败/已取消）过滤，点按即时切换。
- **排序**：鼠标拖拽，或聚焦卡片后按空格进入键盘排序（方向键移动，空格确认）。
- **批量停止**：右侧「快捷操作」里可一键停止全部运行中的应用（有确认框，逐个安全停止，绝不按端口杀进程）。
- **开机自启（autostart）**：编辑服务卡片时打开「开机自启」开关，总控台启动后会按配置顺序自动拉起标记的服务（仅长期服务，批处理任务无自启意义）。配合「总控台自身设为开机启动」（Windows 注册表 Run 键）即可实现开机后全部托管服务自动就绪。
- **端口被占用时**：点启动会弹出确认框，展示占用进程的名称、PID 与命令，确认后终止该占用进程（仅限当前用户进程，后端 UID 校验兜底）并自动启动服务——一键解决端口冲突。

### 服务监控（看这台设备在跑什么）

- **概览卡**：在线服务/后台应用/总 CPU/总内存（带最近一分钟负载曲线）/端口警告/最后更新。
- **服务表格**：每个服务的 PID、端口、目录、负载、时长、状态，以及**启动者徽标**——溯源显示这个进程是哪个 AI 助手（Codex/Claude/Kimi 等）、编辑器（VS Code/Cursor 等）、终端或总控台启动的。点端口直接打开服务；行尾按钮可加入启动台、置顶、隐藏、展开完整命令或安全结束进程。
- **发现新端口**：页面打开期间新出现的监听端口会单独提醒，可一键「加入启动台」（自动识别项目并原子认领进程）、「忽略并隐藏」或「暂时关闭」。
- **后台与已隐藏**：系统/GUI 应用进程默认折叠在「应用后台」；被隐藏的服务可随时恢复。
- **关注的进程**：输入关键字（如 `ffmpeg`）回车，匹配进程实时列出。

### 日志中心（Ctrl+J）

导航轨「日志中心」或快捷键 Ctrl+J（Ctrl+L 是浏览器保留键）：所有应用按运行中优先排列，点开任意一行看实时日志；底部固定总控台自身日志入口。

### 设置中心

导航轨齿轮：任务完成通知开关（系统通知，切走页面也能收到）、外观三态（自动/浅色/深色）、版本/端口/工作目录/数据目录信息。

### 命令面板（Ctrl+K）

全局搜索并执行：添加服务/任务、启动/停止/重启任意应用、打开页面、查看日志、切换视图、开关任务通知、查看总控台日志等，全键盘操作。

### 使用要点

- 红色按钮会结束进程或删除应用，需要二次确认。
- 批处理任务自然退出 `0` 表示成功，其他非零退出码表示失败；脚本内部用户主动取消请退出 `130`（显示为「已取消」）；总控台按钮主动中止单独显示为「已中止」。
- 选择批处理脚本时，总控台只保存脚本的绝对路径和生成的执行命令，不会复制或托管脚本内容。脚本移动、改名或删除后，任务会失效；建议将个人脚本放在长期稳定、会单独备份的自动化目录中。
- 停止总控台不会自动停止已启动的独立服务；配置里的应用、图标、关注关键字和隐藏/置顶标记都会保留。

### 批处理退出码约定

任务自然退出 `0` = 成功，其他非零 = 失败；脚本内部用户主动取消请退出 `130`（显示为「已取消」而非失败）；总控台按钮中止显示为「已中止」。Python 用 `raise SystemExit(130)`，Shell 用 `exit 130`，Node.js 设 `process.exitCode = 130`。此约定只用于 `task`，长期服务仍按普通退出处理。

### 新端口发现的基线规则

「服务监控」只提醒**页面打开后新出现**、尚未纳入启动台的本地服务。首次载入、页面从后台恢复、断线重连或总控台重启后的第一份状态只用于建立静默基线，不会把已有端口全部弹一遍。「忽略并隐藏」写入配置并可恢复；「暂时关闭」只影响当前页面会话。

## 数据、隐私与备份

运行数据与程序目录分离。默认位置按平台区分：

| 平台 | 路径 | 内容 | 备份建议 |
| --- | --- | --- | --- |
| macOS | `~/Library/Application Support/总控台/config.json` | 应用命令、本地路径、端口、标记和运行识别信息 | 必须 |
| macOS | `~/Library/Application Support/总控台/config.json.bak` | 上一份已知良好的配置 | 必须 |
| macOS | `~/Library/Application Support/总控台/icons/` | 用户上传的图标和站点图标 | 按需 |
| macOS | `~/Library/Logs/总控台/` | 应用与总控台运行日志 | 通常不需 |
| Windows | `%APPDATA%\总控台\config.json`（及 `.bak`） | 同 macOS 的 config 两项 | 必须 |
| Windows | `%APPDATA%\总控台\icons\` | 用户上传的图标和站点图标 | 按需 |
| Windows | `%LOCALAPPDATA%\总控台\` | 应用与总控台运行日志 | 通常不需 |

目录权限会收紧为 `0700`，配置、图标和日志文件为 `0600`；Windows 上权限位检查自动跳过，改由 **TokenUser SID 收紧文件 DACL**（私有目录/文件仅当前用户可访问，发布检查会实际验证）。这些文件仍可能含个人路径、完整 shell 命令和日志内容；不应进入 Git，也不应随发行包或故障报告对外传播。

### 旧版数据首次迁移

如果新目标目录尚不存在，首次启动会将项目内旧 `data/config.json{,.bak}` 和 `data/icons/` 安全复制到 Application Support，将 `data/logs/` 复制到 Library Logs。迁移使用临时目录后原子落位，并且：

- 旧 `data/` 始终保留，不会自动删除。
- 目标已存在时绝不覆盖或合并，避免把更新的用户数据换回旧版。
- 符号链接和非普通文件不会被复制。
- 显式设置 `CONSOLE_DATA_DIR` 或 `CONSOLE_LOG_DIR` 时，对应目录不执行旧数据自动迁移。

需要自定义路径时：

```bash
CONSOLE_DATA_DIR="/private/path/console-data" \
CONSOLE_LOG_DIR="/private/path/console-logs" \
python server.py
```

Windows（cmd）：

```bat
set CONSOLE_DATA_DIR=D:\path\console-data
set CONSOLE_LOG_DIR=D:\path\console-logs
python server.py
```

自定义值必须是非空的绝对路径，并指向总控台专用的非符号链接子目录；不要直接填 `/`、用户主目录或项目根目录。

### 备份

1. 不再执行新的启动、停止或编辑操作。
2. 停止总控台。
3. 将数据目录（macOS：`~/Library/Application Support/总控台/`；Windows：`%APPDATA%\总控台\`）复制到受保护的备份目录。
4. 记录当前 `VERSION`，以便恢复时匹配配置格式。

### 恢复

1. 确保总控台已停止，并另存当前数据目录（macOS：`~/Library/Application Support/总控台/`；Windows：`%APPDATA%\总控台\`）。
2. 将备份中的 `config.json` 和 `icons/` 复制回对应位置。
3. 重新启动，逐项确认命令、工作目录和端口。

如果主配置损坏，程序会验证 `config.json.bak` 并恢复主文件。如果两份都不可用，服务进入只读保护状态，不会用空配置覆盖它们。`config.json.bak` 保留的是每次修改之前的上一份良好配置，而不是主文件的同内容副本。

## 升级

1. 阅读 `CHANGELOG.md`，确认是否有配置或平台变更。
2. 停止总控台并完整备份 `~/Library/Application Support/总控台/`。
3. 用新版本替换程序文件；用户数据保持在 Library 目录中。
4. 运行 `make check`。
5. 启动后检查应用数量、主题、关注关键字和一个可控服务的完整启停。

配置包含 `schemaVersion`，启动时逐版执行显式、幂等迁移。新程序不会静默降级它不认识的更高 schema；回退程序时仍应同时恢复与该版本匹配的数据备份。

## 卸载

1. 如果不希望已启动的服务继续运行，先在启动台逐个停止它们。
2. 停止总控台。
3. 按需导出数据目录（macOS：`~/Library/Application Support/总控台/`；Windows：`%APPDATA%\总控台\`）备份。
4. 将整个项目目录移到废纸篓。
5. 确认不再需要数据后，手动删除数据目录（macOS：`~/Library/Application Support/总控台/` 与 `~/Library/Logs/总控台/`；Windows：`%APPDATA%\总控台\` 与 `%LOCALAPPDATA%\总控台\`）。

程序不会安装系统启动项，卸载时也不会自动删除用户数据。

## 安全边界

总控台不是多用户服务器或远程管理面板。它能以当前登录用户（Windows / macOS）的权限执行你保存的 shell 命令，因此：

- 只添加你已检查且信任的命令和工作目录。
- 不要将服务绑定到 `0.0.0.0`，不要通过反向代理、SSH 隧道或端口映射对外暴露。
- 不要在共享或不受信任的用户账户中运行。
- 不要把 Application Support 中的 `config.json`、Library Logs 日志或故障截图未经脱敏就上传。
- 本地回环绑定只是第一层边界。所有写接口还要求当前用户私有 `control.token` 对应的 `X-Console-Token`；启动器仅通过浏览器 URL fragment 传入令牌，前端会立即从地址栏清除。发布验收时必须执行 `RELEASE_CHECKLIST.md` 中的安全项。
- 直接输入本地 URL 可以只读查看；页面会显示只读提示，变更请求不会在无令牌时发出。需要通过启动器或托盘的“打开控制台”进入可写页面。

**Windows 平台差异**：

- Windows 没有 Unix PGID，受控进程识别使用「随机运行 token + 以根 PID 为锚点的进程树回溯」代替 PGID；进程归属则使用 TokenUser SID，无法读取 SID 的进程不会被当作当前用户。写接口还要求私有能力令牌，因此其他本地账户不能仅凭回环端口控制总控台。
- 受控应用的启动命令通过 `cmd.exe /c` 执行：`service` 请使用前台命令（如 `python -m http.server`、`node server.js`）；需要 Shell 语法时同样可用（`&`、`&&` 等）。
- 系统通知使用浏览器 Notification API（与 macOS 一致），无需额外依赖。

## Windows 平台支持矩阵

Windows 为同一代码库的移植版本（平台差异收口在 `sysops.py` 跨平台层）。下表是功能支持与有意取舍的完整清单：

| 能力 | Windows | 说明 |
| --- | --- | --- |
| 服务/任务启停、重启、日志、诊断 | ✅ 一致 | 与 macOS 同一实现 |
| 进程溯源、端口发现、attached 认领 | ✅ 一致 | 进程树回溯替代 PGID |
| favicon 抓取、图标上传 | ✅ 一致 | 见下方限制 |
| 命令面板、新端口发现、设置中心 | ✅ 一致 | 快捷键显示为 `Ctrl`（macOS 为 `⌘`） |
| 优雅停止 | ⚠️ 差异 | 无 SIGTERM：带窗口进程走 WM_CLOSE 软通道，无窗口服务只能硬杀（macOS 可被 SIGTERM 捕获落盘） |
| 用户隔离 | ✅ | Windows 以 TokenUser SID 校验进程归属；所有写接口要求当前用户私有 `control.token` 对应的能力令牌 |
| CPU 口径 | ⚠️ 差异 | 按「占全部逻辑核百分比」（任务管理器口径），`/api/state` 带 `coreCount`；macOS 为单核口径 |
| Shell 语义 | ⚠️ 差异 | `cmd.exe /c` 包装：`service` 用前台命令，**命令内不要用单引号**（cmd 不识别） |
| 启动器 | ✅ 等价 | `start.bat`（双击后台运行、首次自动装 psutil、已有实例时显示打开/重启/取消菜单）；运行中显示**系统托盘图标**（左键打开、右键菜单重启/停止，tooltip 显示端口与状态） |
| 开机自启（autostart） | ✅ 新增 | 服务卡片可标记「开机自启」，总控台启动后按顺序自动拉起（延迟数秒、间隔启动、失败记日志不阻塞） |
| 端口占用「释放并启动」 | ✅ 新增 | 端口被占用时点启动，确认框展示占用进程信息，确认后终止（仅当前用户）并自动启动 |

**已知限制**（与 macOS 一致或移植固有的）：

- favicon 抓取只对 token 受管进程生效，`attached` 认领的卡片会返回"应用未运行或无可用端口"（上游既有行为，两平台相同）。
- 优雅停止对无窗口服务无效（只能硬杀），涉及落盘的服务建议定期保存。
- 运行要求：Windows 10/11 + Python 3.12+；唯一运行时依赖 `psutil>=7.2`（`start.bat` 首次启动自动安装，要求联网）。

## 故障排查

### 双击后没有界面

- 确认 `python --version` 可用且为 3.12 或更高。
- 查看 `%LOCALAPPDATA%\总控台\console.log`。
- 用 `python server.py` 从终端启动，直接查看错误。
- 不要单独移动 `LocalOpsConsole.exe`；它必须与 `server.py` 同目录。

### 9600 打不开

程序可能已选择 9601–9609。查看终端输出或 `%LOCALAPPDATA%\总控台\console.log` 中的实际地址。服务可访问时，`GET /api/health` 会返回程序版本、配置 schema 和降级原因，且不会执行 `ps/lsof` 扫描。

### 应用启动失败

- 先打开该应用的日志和“启动诊断”。
- 确认工作目录仍然存在、命令可在普通 shell（`cmd.exe` / PowerShell）中运行。
- 检查启动瞬间配置端口是否正被其他进程占用；不同项目允许保存相同的常见开发端口（也可用「释放并启动」一键解决）。
- 通过快捷方式或托盘启动的应用可能不继承完整的用户 PATH；总控台会补入常用开发工具路径，但非标准安装仍可能需要显式绝对路径。

### 配置丢失或损坏

停止总控台，保留当前 `config.json`，然后按上文“恢复”流程使用已知良好的 `config.json.bak` 或离线备份。

## 开发

运行时无第三方 Python 依赖。重新生成品牌图标派生文件或图标库时需要开发依赖：

```bash
python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
```

主要目录：

```text
server.py                 Python 标准库后端
static/                   原生前端、主题、品牌、图标和字体
tests/                    后端、前端契约、发布与交付检查
tools/gen_brand_assets.py 从品牌主图生成 favicon 与图标（.ico/.png）
tools/gen_icons.py         由 vendored SVG 生成 icons.js
tools/check_project.py     统一的只读项目检查
data/                      旧版运行数据（仅首次迁移源，不进 Git/发行包）
```

### 检查

提交前的权威命令是：

```bash
make check
```

它会检查 Python/JavaScript/Bash/plist/JSON 语法、版本一致性、主题和资源引用、生成的图标是否同步，并显式发现和运行测试。测试数量为 0 时会失败，不会出现“0 tests 也算通过”。

**Windows 开发环境**（无 make 时的等价命令）：

```bat
:: 提交前全量检查（等价 make check）
python tools/check_project.py
:: 仅语法检查（等价 make syntax）
python tools/check_project.py --skip-tests
:: 仅后端测试（等价 make test）
python -m unittest discover -s tests -p "test_*.py" -v
```

只运行后端测试（macOS）：

```bash
make test
# 等价的显式命令：
python3 -m unittest discover -s tests -p 'test_*.py' -v
```

正式发布前还应运行：

```bash
make release-check
```

它会额外检查 Git 状态和不应进入发行范围的文件；不会代替 `RELEASE_CHECKLIST.md` 中的人工验收。

在 Windows 上，`make release-check` 的等价命令还会在临时目录中真实验证私有目录/文件的 DACL 非空，以及当前用户的创建、读取、覆盖和删除权限：

```bat
python tools\build_release.py --check-only
python tools\check_project.py --release
```

### 重新生成资源

```bash
make generate-icons
make generate-brand
make check
```

`static/icons.js` 是生成文件，不应手工修改。`generate-brand` 以 `static/assets/console-app-icon.png` 为主源（macOS 开发环境使用系统 `iconutil`；Windows 上 `tools/gen_brand_assets.py` 直接生成 `.ico`）。重新生成品牌图标后，只提交预期的差异，并同步更新 `ASSET_PROVENANCE.md` 的 SHA-256。

## 发布

请按 `RELEASE_CHECKLIST.md` 逐项验收。一个可对外交付的版本至少需要：

- 与根目录 MIT 许可证一致的版权信息，以及全部第三方素材和项目图像的来源、许可与授权凭证。
- 干净、可追溯的 Git commit 和带签名版本 Tag。
- 通过 `make release-check` 和人工 UI/安全/升级/回滚验收。
- 不含任何项目内旧 `data/`、用户数据、日志、绝对路径、token 或缓存的发行包。
- Windows 发行包（zip）解压后可直接运行：`LocalOpsConsole.exe` 无窗口启动、`start.bat` 备用；按 `RELEASE_CHECKLIST.md` 完成 Windows 全新安装与回退验证（含控制令牌只读/可写切换、ACL 冒烟检查）。

## 社区衍生版本

以下衍生版本由社区贡献者各自维护，未经原作者审阅或测试，收录仅作推荐。提交新衍生版本或更新说明，请前往 Discussions。

| 衍生版本 | 说明 | 出处 |
| --- | --- | --- |
| Windows 10/11 适配（双平台运行） | 共享代码 + 平台分支收敛，不新增运行时依赖，含 Windows 专属测试与 CI | PR [#2](https://github.com/laogou717/local-ops/pull/2)（dontpanic1） |
| Windows 11 安全优先移植（Draft） | Job Objects、签名回执、CREATE_SUSPENDED 等更严格的进程所有权模型，含打包体系 | PR [#3](https://github.com/laogou717/local-ops/pull/3)（songconmaisaix31-design） |
| Windows 后端 `server_win.py` | 独立 Windows 后端（纯标准库），复用本仓库前端 | PR [#4](https://github.com/laogou717/local-ops/pull/4)（Hexvork） |
| sysops.py 跨平台抽象层方案（Windows 移植版） | psutil 唯一新增依赖，macOS 分支零改动；含系统托盘、无窗口启动器、开机自启、端口释放、控制令牌等 Windows 增强 | [FL411/local-ops](https://github.com/FL411/local-ops) |

## 参与贡献与安全

- 提交代码前请阅读 [`CONTRIBUTING.md`](CONTRIBUTING.md) 与上方「维护说明」，并运行 `make check`。
- 行为规范见 [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)。
- 安全问题不要作为普通公开 Issue 披露；报告方式和脱敏要求见 [`SECURITY.md`](SECURITY.md)。
- 新增或替换字体、图标、插画、纹理等素材时，必须同步更新 [`ASSET_PROVENANCE.md`](ASSET_PROVENANCE.md) 和 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md)。

## 许可与第三方素材

项目自有代码和文档采用 [`MIT License`](LICENSE)。Lucide、Geist Mono 以及项目生成图像等素材可能适用各自的许可或发布限制，不因根目录 MIT 许可证而自动改变，详见 [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md) 与 [`ASSET_PROVENANCE.md`](ASSET_PROVENANCE.md)。
