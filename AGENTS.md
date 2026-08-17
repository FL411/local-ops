# 总控台 (Console)

本地服务监控与快速启动控制台。**零依赖**（macOS：Python 3 标准库后端，无第三方依赖；Windows：唯一运行时依赖 `psutil>=7.2`，首次启动自动安装）。macOS 推荐双击 `总控台.app` 后台运行（不显示 Terminal/Dock），`start.command` 保留为终端调试入口；**Windows 双击 `start.bat`**（pythonw 无窗口后台运行，已有实例时显示「打开/重启/取消」菜单，由 `launcher_check.py` 探测/执行）。

## 结构

- `server.py` — 后端（单文件，仅标准库，Python 3.12+）
- `sysops.py` — **跨平台系统操作层（Windows 移植新增）**：进程/端口扫描（macOS 用 ps/lsof，Windows 用 psutil）、进程组（Windows 用进程树回溯）、进程归属（uid / TokenUser SID）、文件锁（flock / msvcrt.locking）、对话框（osascript / tkinter+ctypes）、信号终止（SIGTERM / WM_CLOSE+TerminateProcess）。平台差异一律收口到本文件，server.py 不直接依赖平台 API
- `start.bat` / `launcher_check.py` — Windows 启动器（探测实例/打开/重启，输出纯 ASCII）；`requirements-runtime-win.txt` — Windows 运行时依赖说明
- `static/index.html` / `static/app.js`（入口）/ `static/js/{core,launchpad,services,overlays,ports,widgets}.js`（原生 ES Modules，无构建）/ `static/icons.js` — 前端（原生，禁框架/CDN/构建）；`core.js` 承载工具/API/浮层/状态/主题注册，`launchpad.js` 卡片+拖拽+诊断+启动台 KPI/分区过滤，`services.js` 表格+监控 KPI 火花线，`overlays.js` 模态+抽屉，`ports.js` 端口归一化纯函数，`widgets.js` 右侧信息栏（实时动态/告警、TOP5、小贴士、快捷操作）与导航轨状态；模块间用 `window.__poll` 共享轮询入口
- 布局 v2：左侧 `.rail` 图标导航轨（启动台/服务监控视图切换 + 日志中心/设置中心弹层入口）+ 顶栏 + 内容/右侧信息栏双栏网格（≤1280px 侧栏下沉到底部、≤900px 导航轨隐藏）；结构样式集中在 `static/base.css` 末尾「布局 v2」段（主题令牌驱动），主题包负责视觉皮肤
- `static/themes/` — **单一主题**：当前仅内置 `ops`（指挥台，`DEFAULT_UI_THEME` 常量指定并在清单中固定排首位）。`{id}.css` 整包样式 + `{id}.json` 清单（`id/name/author/desc/colors[]`）的注册机制保留：`GET /api/state` 返回 `themes` 与 `uiTheme`；`POST /api/ui/theme {theme}` 校验 id 后落盘。产品不提供主题选择界面（已随多主题一并移除），深浅色切换仍保留。
- `static/fonts/GeistMono-Variable.woff2` — vendored 数据/代码字体；中文与正文使用 macOS 系统字体栈；`static/icons/*.svg` — Lucide 图标源文件（vendored）；`tools/gen_icons.py` — 由 svg 重新生成 `icons.js`（勿手改 icons.js）
- `static/assets/` — 品牌素材：`console-app-icon.png` 为 App Icon 主图，`brand-mark.png` 为顶栏标识；`favicon-32.png` / `favicon.ico` / `apple-touch-icon.png` 与 `.app` 内 `AppIcon.icns` 由 `tools/gen_brand_assets.py` 生成
- `~/Library/Application Support/总控台/config.json` — 用户配置；`icons/` 为应用图标。目录/ 文件权限分别为 0700/0600
- `~/Library/Logs/总控台/{appId}.log` — 应用启动日志；`console.log` 为 `.app` 启动日志
- `data/` — 旧版项目内数据，仅在新目标不存在的首次启动中复制迁移；保留不删除
- `start.command` — macOS 双击启动脚本（chmod +x）
- `总控台.app` — macOS 无终端窗口启动器（`LSUIElement` 后台应用；内部直接启动 `server.py`，输出写入 `~/Library/Logs/总控台/console.log`）

## 运行

macOS：`python3 server.py` → 绑定 `127.0.0.1`，端口从 **9600** 起尝试，被占则 +1（最多 10 个）。启动后自动打开浏览器。`/favicon.ico` 返回统一品牌图标。双击 `总控台.app` 会先识别同目录的现有总控台，可直接打开或安全重启。
Windows：`start.bat`（自动探测 Python ≥3.12 与 psutil，`pythonw server.py --log-to-file` 无窗口后台运行）；或手动 `python server.py`。数据目录 `%APPDATA%\总控台\config.json`（icons/ 为应用图标；`control.token` 为仅当前用户可读的本地控制能力令牌）、日志 `%LOCALAPPDATA%\总控台\console.log`（macOS 分别为 `~/Library/Application Support/总控台` 与 `~/Library/Logs/总控台`）。

## 平台差异（Windows 移植的有意取舍）

- **进程归属与本地控制**：Windows 用 TokenUser SID 代替 Unix uid；身份未知的进程不视为当前用户。所有写接口还要求 `X-Console-Token`，令牌存于私有 `control.token`，启动器仅通过浏览器 URL fragment 传入（不进入 HTTP 请求或 Referer）。
- **CPU 口径**：Windows 按「占全部逻辑核百分比」归一化，`/api/state` 带 `coreCount`；macOS 保持单核口径。
- **优雅停止**：Windows 无 SIGTERM——先 WM_CLOSE 软通道（带窗口进程可清理），宽限后对无窗口服务硬杀。
- **Shell 包装**：macOS 用 bash 双层包装；Windows 用 `cmd /c "echo <marker> & <command>"`，`service` 需前台命令，命令内**不要用单引号**（cmd 不识别）。
- **快捷键提示**：前端 `MOD_KEY` 常量（macOS ⌘ / 其他 Ctrl）驱动提示文案；触发逻辑兼容 `metaKey || ctrlKey`。
- 测试：macOS 专属语义用例（lsof 解析/bash 包装/chmod/symlink/发布检查）在 Windows 用 `IS_POSIX` 条件跳过；跨平台断言引用 `server.PYTHON_CMD`。

## API 契约（全部 JSON；icon 上传为原始字节）

- 所有 `POST` / `PUT` / `DELETE` 请求均需携带 `X-Console-Token`；令牌由启动器和前端自动处理，手工 API 客户端应从当前用户的数据目录读取，禁止把令牌写入日志或命令历史。

### `GET /api/state` — 前端唯一轮询接口
```json
{
  "services": [{
    "key": "python3.12:8791", "instanceKey": "54252:8791",
    "pid": 54252, "name": "python3.12", "port": 8791,
    "cwd": "/Users/example/xx项目", "project": "xx项目", "cmd": "python3 app.py",
    "cpu": 0.3, "mem": 1.2, "uptimeSec": 7980,
    "group": "mine", "pinned": false, "hidden": false, "promoted": false,
    "appId": null, "appName": null,
    "origin": {"label": "Codex", "icon": "bot"}
  }],
  "watched": [{"pid": 1, "name": "ffmpeg", "cmd": "...", "cpu": 0.0, "mem": 0.5, "uptimeSec": 60, "keyword": "ffmpeg"}],
  "apps": [{
    "id": "a1b2c3d4", "name": "我的博客", "command": "python3 -m http.server 8080",
    "cwd": "/path", "port": 8080, "emoji": "🚀", "glyph": "rocket", "icon": "/icons/a1b2c3d4.png",
    "kind": "service", "attached": false,
    "running": true, "pid": 1234, "uptimeSec": 120,
    "listening": true, "portOccupied": false, "portOccupiedPid": null,
    "portConflict": false, "portConflictApps": [],
    "lastExit": {"status": "succeeded", "code": 0, "at": 1700000000, "startedAt": 1699999998750, "durationSec": 1.25},
    "health": {"status": "ok", "blocking": false, "issues": []},
    "favicon": "/icons/fav-a1b2c3d4.png",
    "ports": [8080],
    "listening": true, "portOccupied": false, "portOccupiedPid": null,
    "portOwner": null, "portConflict": false, "portConflictApps": []
  }],
  "watchedKeywords": ["ffmpeg"],
  "consolePort": 9600, "consolePid": 123, "consoleCwd": "/path/to/总控台",
  "version": "1.0.0", "schemaVersion": 1,
  "degraded": false, "degradedReasons": []
}
```
- `GET /api/health` — 不运行 `ps/lsof` 的轻量健康检查，返回 `status/version/schemaVersion/degraded/issues/config`
- `group`: `"mine"` | `"background"`；`icon`/`emoji`/`port`/`cwd`/`project`/`appId`/`appName`/`lastExit` 可为 `null`
- `lastExit`：最近一次退出结果。任务状态为 `succeeded`（exit 0）/`canceled`（脚本主动 exit 130）/`failed`（其他自然退出）/`stopped`（总控台中止，code=null）；旧数据可能只有 `code/at`，API 输出时会兼容推导但不改写磁盘。批处理启动时保留上一次完成历史，自然退出或中止后覆盖
- `health`：每次状态读取时只读检查配置，返回 `status: ok|error|unknown`、`blocking` 与 `issues[{kind,severity,title,detail,fix,action}]`。明确缺失的 cwd、脚本或运行时会阻止启动；复杂 Shell 命令无法静态判断时为 unknown，不阻止运行
- `kind`：`"service"`（长期服务，有端口语义）| `"task"`（批处理任务，强制 port=null，主按钮为「运行」）；旧数据缺省视为 `service`。启动台按 kind 分两个区渲染
- `running`：仅表示存在通过本次启动 token、进程组与当前用户三重校验的受控进程；不再以“配置端口有任意监听者”作为运行依据
- `attached`：用户从服务监控明确认领的外部服务身份。此类服务的监听子进程换 PID 后，可按配置端口 + 当前 UID + 真实 cwd 唯一重新关联；普通卡片仍不得仅凭端口自动认领
- 服务行的 `key` 保持 `name:port` 以兼容隐藏/置顶配置；`instanceKey` 使用 `pid:port` 区分同名同端口后来出现的新进程实例，前端发现与 DOM 对账均使用它
- `listening`：受控进程是否正在监听配置端口；`portOccupied`：该端口当前被不属于本卡片的进程占用；多张卡片允许保存同一个常见开发端口，`portConflict/portConflictApps` 仅为旧前端兼容字段并固定返回 `false/[]`；`legacyManaged`：是否通过旧版 PID+端口+UID+cwd 兼容身份识别
- `project`：cwd 最后一段目录名（用于区分同名进程）；`appId`/`appName`：该端口命中启动台应用时的关联信息
- 排除控制台自身进程；只返回当前用户的进程
- **进程溯源**：`origin` 沿 PPID 链（≤12 层）识别启动者——跳过壳/包管理器/运行时包装层与 launchd，优先匹配已知 AI 编程助手（codex/claude/kimi/gemini/aider/opencode 等）、`.app` 包（VS Code/Cursor/iTerm/Warp 等）、tmux/screen 与总控台 run-token 标记（「总控台」）；未识别的中间层先记为候选、有更优答案即覆盖，全部落空才以最近未识别进程命名；`label` 为展示名、`icon` 取 bot/code/terminal/package/rocket/server，仅用于展示，不影响启停判定

### 服务操作
- `POST /api/kill` `{pid, force?}` → `{ok}` / `{ok:false, error}`（force 用 SIGKILL；校验属当前用户）
- `POST /api/services/flag` `{key, flag: "hidden"|"pinned"|"promoted", value: bool}` → `{ok}`（promoted=false 即「移回后台」，前端对 `svc.promoted` 的行显示该按钮）
- `POST /api/watch` `{keyword, action: "add"|"remove"}` → `{ok, keywords}`

### 启动台应用
- `POST /api/apps` `{name, command, cwd?, port?, emoji?, glyph?, kind?, attachPid?}` → app 对象（`kind` 缺省 `service`；`task` 强制 port=null；服务监控来源可带 `attachPid`，后端先校验 PID/端口/UID/cwd，再将卡片与运行身份一次写入，失败不创建半成品卡片）
- `POST /api/pick` `{what: "dir"|"script"}` → `{ok, path}` / `{ok, canceled:true}`（osascript 弹 macOS 原生目录/文件选择框；取消不是错误）
- `POST /api/project/detect` `{cwd}` → `{ok, cwd, name, files, candidates:[{command,label,source,port,kind,detail}]}`（只读分析项目根目录，不执行项目代码；识别 package.json scripts 与包管理器锁文件、Hexo/Hugo/Jekyll、Django/FastAPI/Flask/Streamlit、Docker Compose、Go、Rust、常用启动脚本及纯静态站点。Hexo 无 scripts 时仍返回 `hexo s` 服务与 `hexo cl` 任务）
- `POST /api/apps/reorder` `{ids: [...]}` → `{ok}`（按 ids 重排 apps 数组；Python sort 稳定，未涉及的 id 相对顺序不变，服务/任务两区可独立拖拽排序互不干扰）
- `PUT /api/apps/{id}`（部分更新同字段，可带 `stopBeforeUpdate:true`）→ app 对象；运行中修改 command/cwd/port/kind 时，缺少该标记返回 `{ok:false, requiresStop:true}`，带标记则安全停止后原子保存
- `DELETE /api/apps/{id}` → `{ok}`（先停止再删，连同图标/日志）
- `POST /api/apps/{id}/start` → `{ok, pid}` / `{ok:false, error, health?}`（已运行则报错；启动前复查配置健康，明确失效返回 422；批处理启动后立即返回，由退出监视线程记录结果，快速成功任务不会被误判成启动失败）
- `POST /api/apps/{id}/stop` → `{ok}` / `{ok:false, error}`
- `POST /api/apps/{id}/restart` → `{ok, pid}` / `{ok:false, error}`（仅重启 token 校验通过的受管进程；等待旧进程退出后再启动，不自动 SIGKILL）
- `POST /api/apps/{id}/diagnose` → `{ok, issues:[{kind,title,detail,fix,action?}], summary}`（本地规则诊断，不调外部 AI：合并运行前健康检查，并覆盖依赖未装/模块缺失、npm 脚本名错误、运行时端口占用、权限不足、pip 包缺失与退出码兜底判读；前端在配置失效或运行失败时显示诊断入口）
- `POST /api/apps/{id}/attach` `{pid}` → `{ok, pid, cwdUpdated?, cwd?}` / `{ok:false, error}`（把已在监听配置端口的当前用户进程**认领**为本卡片受管进程：走 legacy 身份通道 lastPid+端口+UID+真实 cwd 四重校验，cwd 不一致时原子同步为进程实际目录；拒绝 task、无端口、已运行、非当前用户、他卡已认领与未监听该端口的进程。前端在端口诊断弹窗提供「认领为本卡片」）
- `POST /api/apps/{id}/icon`（body 为 png/jpg/webp 原始字节）→ `{ok, icon}`
- `POST /api/apps/{id}/favicon` → `{ok, favicon}` / `{ok:false, error}`（按有效端口抓站点图标：解析首页 `<link rel*icon*>`，兜底 `/favicon.ico`，支持 png/jpg/webp/ico/svg，存入 Application Support 的 `icons/fav-{id}.{ext}` 并写入 `app.favicon`；图标优先级：上传 icon > glyph > favicon > 名称首字，前端在无 icon/glyph 且运行中时自动触发一次）
- `DELETE /api/apps/{id}/icon` → `{ok}`
- `GET /api/apps/{id}/logs?tail=300` → `{text}`

### 总控台自身
- `POST /api/console/restart` → `{ok, pid, helperPid, port}`（先返回响应，再由独立 helper 等待旧进程退出并优先复用原端口；启动台应用不随总控台停止）
- `POST /api/console/stop` → `{ok, pid, port}`（响应发出后关闭总控台 HTTP 服务；启动台中已经运行的独立进程组保持运行）
- `POST /api/ui/theme` `{theme}` → `{ok, theme}` / `{ok:false, error}`（校验主题 id 存在后写入 `config.json` 的 `uiTheme`；主题清单由 `/api/state` 的 `themes` 字段返回）

### 静态
`GET /` → `static/index.html`；`/app.js`、`/js/*`、`/themes/*`、`/assets/*`、`/fonts/*` 等映射 `static/`；`/icons/xxx` → Application Support 的 `icons/xxx`。防路径穿越。

## 后端实现要点

- **端口扫描**：`lsof -iTCP -sTCP:LISTEN -P -n`，按 `(pid, port)` 去重（IPv4/6 重复行）。lsof 的 COMMAND 列会截断，名称以 ps 的 comm 为准。
- **状态快照**：`/api/state` 使用 2.2 秒 TTL 与 stale-while-revalidate；过期时立即返回上一份完整快照，后台最多一个刷新线程。缓存锁内不得读取配置或执行进程/端口扫描，`Config.update()` 必须在释放配置锁后再使缓存失效，避免锁顺序反转。
- **进程详情**：批量 `ps -o pid=,user=,comm=,args=,%cpu=,%mem=,etime= -p <逗号分隔pid>`；只保留 `user == 当前用户`。
- **cwd**：`lsof -a -p <逗号分隔pid> -d cwd -Fn`，解析 `n` 行。
- **etime 解析**：`[[dd-]hh:]mm:ss` → 秒。
- **分组逻辑**（按优先级）：用户 `promoted` → `mine`；进程名含开发关键词（python node ollama docker 等，见 `DEV_KEYWORDS`，只匹配 name 不匹配 args，避免 VS Code `--ms-enable-electron-run-as-node` 这类误伤）→ `mine`（覆盖下方规则，Ollama/Docker 这类在 .app 内的守护进程仍算服务）；可执行路径含 `.app/Contents/`（GUI 应用及其 helper）→ `background`；comm 以系统路径开头（`/usr/libexec/`、`/usr/sbin/`、`/sbin/`、`/System/`、`/usr/lib/`）→ `background`；comm 或 cwd 含 `/Library/Containers/`（沙盒应用）→ `background`；其余默认 `mine`。`hidden` 仅是标记，照常返回。
- **关注进程**：`ps -axo pid=,uid=,comm=,args=,etime=,%cpu=,%mem=`，args 小写包含关键字即命中，只保留当前用户并排除自身及 ps/lsof。
- **应用状态**：每次启动生成随机 `runToken`，常驻外层 shell 在 argv 中持有标记并等待内层命令及其后台作业。新版进程只有同时命中 `lastPgid` / 当前 UID / token 的进程组才算 running；升级前缺少 token 的旧进程，只有配置 `lastPid`、监听端口、当前 UID 与真实 cwd 全部一致时才兼容认领。用户明确从服务监控认领的 `attached` 卡片允许监听子进程换 PID，但必须在配置端口上按当前 UID + 真实 cwd 唯一命中；任一条件不符仍按外部端口占用处理。`ports` 来自受控进程组成员实际监听的端口。
- **应用启停**：多张卡片可保存相同端口（例如多个默认使用 3000 的项目）；启动前只拒绝失效配置和当时真实被占用的端口。重启先做健康预检，失败时不会先停掉仍工作的旧服务。停止时先校验 token，然后只对该受控进程组发 `SIGTERM`，**绝不按端口杀其他监听者**。服务手动 stop 不记录退出历史；任务自然结束记录四态结果，总控台中止记录 `stopped`。批处理不做“长期服务存活探测”，避免把快速成功误判成失败
- **任务取消协议**：一次性任务内部的“用户主动取消”以退出码 **130** 通知总控台；0 表示成功，其余表示失败。不要通过日志文字猜测状态
- **配置健康**：`inspect_app_health` 只解析确定无歧义的简单命令并执行 stat/权限/PATH 检查，不执行命令、不展开变量/通配符。相对脚本按配置 cwd（空值时用户主目录）解析；复杂或动态命令返回 unknown
- **运行中编辑**：编辑面板打开时立即显示“停止服务”。点击只调用 stop，面板保持打开且当前草稿不变；停止成功后用户继续编辑并普通保存。名称/图标仍可在运行中直接保存。`stopBeforeUpdate:true` 保留为 API 客户端的原子停止更新能力，但不是默认前端流程。
- **无终端 PATH**：Finder/`LSUIElement` 启动不会读取 shell 配置；子应用启动环境需显式补入 `~/.local/bin`、Volta/Bun/pnpm、NVM/fnm、Homebrew 与系统 bin 目录，保证 `node`/`npm`/`pnpm` 等可用。启动 API 短暂探测立即退出，并把日志末行作为明确错误返回。
- **日志**：单文件超过 10MB 时 copy-truncate，保留 3 份轮转备份；日志 API 从文件尾部分块读取，不将整个日志读入内存。
- **keep-alive 陷阱**：POST start/stop 前端会带 `{}` body，handler 必须 `discard_body()` 读掉——否则残留字节污染同一 keep-alive 连接的下一个请求（method 解析成 `{}GET` → 501，前端显示断连横幅）。新增不读 body 的 POST 路由时同样处理。
- **运行目录**：默认配置/图标位于 `~/Library/Application Support/总控台`，日志位于 `~/Library/Logs/总控台`；`CONSOLE_DATA_DIR` / `CONSOLE_LOG_DIR` 可显式覆盖，覆盖时对应目录不自动迁移旧 `data/`。
- **配置**：读写加线程锁；写入用临时文件 + `os.replace` 防损坏；`schemaVersion` 逐版显式迁移；`.bak` 保留上一份良好版本。主配置与备份均不可读时进入只读保护，不覆盖原文件。
- **项目识别**：仅读取项目根目录下不超过 2MB 的已知配置/入口文件，不安装依赖、不执行配置、不扫描整个目录；显式 CLI 端口优先于框架默认端口。
- **kill 安全**：只允许结束当前用户的进程。

## 配置 schema
```json
{
  "schemaVersion": 1,
  "apps": [{"id": "8位hex", "name": "", "command": "", "cwd": null, "port": null, "emoji": null, "icon": null, "favicon": null, "kind": "service", "lastPid": null, "lastPgid": null, "runToken": null, "attached": false, "lastExit": null, "createdAt": 0}],
  "hidden": ["name:port"], "pinned": ["name:port"], "promoted": ["name:port"],
  "watchedKeywords": [],
  "uiTheme": "ops"
}
```

## 前端要求

- 中文 UI，单页两视图（侧边导航：启动台 / 服务监控），每 2s 轮询 `/api/state`
- 添加服务时选择工作区文件夹后自动调用项目识别并展示候选命令；用户点选候选后再填入命令/端口。原有“选择脚本”与手动填写入口必须保留
- 编辑运行中服务时，表单内立即显示“停止服务”；停止操作不得关闭编辑面板或清除已经填写的内容，停止后恢复普通“保存”
- 批处理运行中显示实时耗时和「中止」入口；结束后明确显示成功/取消/失败/中止、距今时间与耗时。失败时突出日志入口；首次加载已有历史不重复提醒
- 停止状态下配置健康有阻断问题时，卡片显示第一项原因、禁用运行/启动并开放「配置与运行诊断」；运行中的停止/中止入口不得被健康问题禁用
- 服务监控只在当前页面会话连续轮询期间提醒新出现的、未管理的 mine 端口；首次加载、断线/后台/降级/重启恢复时静默建立基线。发现栏提供「加入启动台」「忽略并隐藏」「暂时关闭」
- 从服务监控或新端口发现点击「加入启动台」时，项目识别完成前不得保存；创建请求必须携带 `attachPid`，由后端原子完成卡片创建与来源 PID 认领，失败时不留下“已创建但未认领”的半成品卡片；成功后卡片直接显示运行中
- DOM 按 key 原地更新，禁整列表重绘闪烁；fetch 失败显示断连横幅
- 深浅色跟随系统 + 手动切换（localStorage `console-theme`）；**单一 UI 主题 Ops 指挥台**（ops.css：深空蓝黑/雾灰双色 + 柔和圆角细边 + 蓝色强调，配合布局 v2 的导航轨/KPI 图标卡/实时动态侧栏；`#themeCss` 整包加载机制保留）；字体 = macOS 系统字体栈 + Geist Mono（数据/代码）；顶栏品牌图标 = `static/assets/brand-mark.png`；UI 零 emoji
- 动效：卡片入场 stagger（`--d`）、hover 浮起、模态/抽屉缓动、按键下压回弹、`prefers-reduced-motion` 降级
- 危险操作（结束进程/删除应用）必须确认
