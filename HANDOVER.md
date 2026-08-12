# 交接文档 — local-ops(总控台)Windows 移植版

> 最后更新:2026-08-13 03:00 | 接手前请先通读本文件 + `README.md` + `CHANGELOG.md`

## 1. 项目是什么

GitHub `laogou717/local-ops`(中文名"总控台")的 **macOS → Windows 移植版**。功能:本地服务/批处理任务监控与快速启动台(网页 UI,后端 Python,零构建前端)。原版只支持 macOS(依赖 ps/lsof/osascript);本移植让它在 Windows 10/11 上完整可用。

- **位置**:`D:\A_program\local-ops`(有 `.git`,shallow clone 而来)
- **版本**:1.0.0(VERSION 文件)
- **macOS 原始代码备份**:`C:\Users\Zhou\WorkBuddy\2026-08-12-18-41-19\repo_analysis\local-ops`(只读参考)

## 2. 架构与核心文件

| 文件 | 行数 | 职责 |
|---|---|---|
| `server.py` | ~4200 | 后端主程序(HTTP + 状态构建 + 进程管理 + 启动器逻辑) |
| `sysops.py` | ~800 | **跨平台系统操作层**(本次移植核心):macOS 走 ps/lsof/osascript(POSIX 分支,零依赖);Windows 走 psutil |
| `start.bat` | 63 | Windows 启动器:探测 Python → 自动装 psutil → pythonw 无窗口后台运行 |
| `static/` | — | 原生前端(HTML/CSS/JS,无构建),无需改动 |
| `requirements-runtime-win.txt` | — | Windows 唯一运行时依赖:psutil |

**Windows 平台映射**(sysops.py 内部):
- ps/lsof → `psutil`(进程快照/监听端口/工作目录)
- 进程组 PGID → **进程树回溯**(root pid 为锚,沿 ppid 向上 BFS)
- `fcntl.flock` → `msvcrt.locking`(单实例锁)
- `osascript` 对话框 → `tkinter`(文件选择)/`ctypes MessageBox`(提示)
- `SIGTERM` 优雅停止 → `psutil.terminate()`(Windows 上即 TerminateProcess,硬杀)
- `os.getuid` → 恒 0(Windows 无等价 uid,**安全边界弱化,仅限单用户个人电脑**,README 已声明)

## 3. 已完成并验证的工作(全部实测通过)

1. **核心链路**:创建服务 → 启动(token+进程树识别)→ running/listening/端口关联 → 停止 → 端口释放、进程树清空
2. **后台运行**:`start.bat` 双击 → pythonw 无窗口、bat 立即退出、浏览器自动打开;`--log-to-file` 参数(日志 `%LOCALAPPDATA%\总控台\console.log`)
3. **单实例去重**:服务运行中再次双击 start.bat → 只打开浏览器,不重复启动(日志记录"已在运行")
4. **系统进程归组**:svchost/System/explorer 等 27 个 + System32 路径 → 归入「应用后台」折叠(mine 组 72→51)
5. **进程溯源别名**:Code.exe→VS Code、Cursor、WindowsTerminal/cmd/powershell→终端 等;修复含空格路径解析
6. **CPU 采样补全 + Windows 口径归一**:每进程 CPU 由恒 0 改为跨快照差分,并归一为「占全部逻辑核心百分比」(任务管理器口径,吃满 1 核 = 100/28 ≈ 3.6%);macOS 保持单核口径;`/api/state` 输出 `coreCount`
7. **服务重启恢复**:重启总控台后,之前启动的应用(trading-cluster, 18888 端口)被正确重新识别 running

## 4. 关键坑(务必先看,避免重复踩)

### 4.1 Windows 批处理(start.bat)
- **行尾必须 CRLF**(Write 工具写出的是 LF,需 `python .replace('\r\n','\n').replace('\n','\r\n')` 转换)
- **不能用 `if ( ... )` 括号块**:块内圆括号字符(如 `(Windows...)`)会被 cmd 当块定界符 → 「此时不应有 ...」。**用 goto 标签结构**
- **必须纯 ASCII**(中文 Windows cmd 按 GBK 解析 UTF-8 会乱码/解析错乱)
- **`for /f` 内嵌带引号命令会解析失败**(如 `for /f in ('py -3 -c "..."')`)→ 改用「输出到临时文件 + `set /p` 读回」
- pythonw 解析:先 `py -3 -c "import sys;print(sys.executable)"` 拿 python 路径,再替换 `python.exe→pythonw.exe`

### 4.2 Python 3.14(运行解释器是 3.14 pythonw,开发是 3.13 venv)
- `os.fdopen(..., line_buffering=True)` 在 3.14 **已移除该参数** → 用 `open(fd, buffering=1, closefd=False)`
- `signal` 模块**无 SIGKILL** → `getattr(signal, 'SIGKILL', 9)`
- 无 `os.getuid`/`fcntl`/`os.killpg`/`os.getpgid`(都走 sysops)
- **pyc 缓存坑(最重要)**:多解释器混用时,`__pycache__` 里旧 pyc 的 mtime 与源码同秒会被误判有效 → **改完代码必须 `rm -rf __pycache__` 再启动验证**,否则"新代码不生效"

### 4.3 psutil 细节
- argv 是列表,join 后含空格路径(如 `Microsoft VS Code\Code.exe`)不带引号 → `split()[0]` 截断成 "Microsoft" → 已有 `_win_join_cmdline()` 补引号
- 每进程 CPU 需**跨快照采样**(单次返回 0):`_CPU_SAMPLES` 缓存 + TTL 30s
- `pid 0`(System Idle Process)要排除
- 进程名用 `exe` 字段(完整路径),不是 `name`

### 4.4 测试环境
- **cmd.exe 被 Bash/PowerShell 工具拦截**:用 Python `subprocess.run(['cmd.exe', '/c', ...])` 测 bat
- `subprocess.run(capture_output=True)` 跑 cmd→bat→pythonw 时,pythonw 继承管道句柄会阻塞 communicate → **pythonw 的输出必须指向 DEVNULL**
- 原生 curl 不认 git-bash 的 `/tmp` 路径 → cookie jar 用相对路径
- WorkBuddy 的 safe-delete(回收站)在本机部分删除失败 → 用 Python `os.remove/os.rmdir` 逐文件删
- 烧 CPU 测试:**用独立子进程**(Python 线程死循环受 GIL 限制,双线程也只 ~100%;且 spin 线程抢 GIL 会延迟端口绑定)

## 5. 当前状态(2026-08-13 03:30)

- **P0+P1 待办全部清空**:task 退出码(7/7)、detect_project(6/6)、attached 认领(16/16)、favicon/图标(14/14)、前端冒烟(21/21)全部实测通过。
- **验证中发现并修复**:detect_project 生成的 Python 候选命令原硬编码 `python3`(macOS 命令),Windows 上会启动失败;已按平台改用 `python`(Windows)/`python3`(macOS),诊断文案同步平台化。`python3` 命令名问题同样影响 `command_for_script` 的 POSIX 分支(该分支 Windows 已用 `sys.executable`,正确)。
- **psutil 版本锁定**:`requirements-runtime-win.txt`/`start.bat`/README 统一为 `psutil>=7.2`(Python 3.14 兼容)。
- **已知限制(既有行为,非移植引入)**:favicon 抓取只对 token 受管进程生效,`attached` 卡片不支持(favicon 接口内部用 `managed_pids` 判定 live 进程)。
- 服务正在运行(pythonw,9600 端口,/api/health ok);数据目录 `%APPDATA%\总控台`。
- 注意:用户可能通过网页「停止」停过服务 → 页面刷新会出现"连接断开,自动重连"(预期行为,重跑 start.bat 即可)。

## 6. 待办清单(按优先级)

### P0(核心功能完整性,各约 5 分钟)
- [x] 验证 `task` 批处理类型(退出码约定:0=成功/130=取消,cmd /c 下未验证)
- [x] 验证 `detect_project` 项目识别(package.json 检测 + Windows 命令生成)
  - 验证方式:隔离数据目录 + 独立实例(9601)+ HTTP API 全链路;`tests.test_hardening` 46 项在 3.13 venv 全绿。
  - **修复**:Python 候选命令按平台 `python`(Win)/`python3`(macOS)。

### P1(一次冒烟覆盖)
- [x] `attached` 认领外部进程(16/16 通过:创建原子认领/已有卡片认领/cwd 同步/task 422/未监听 409/重复认领 409/运行中 409)
  - 注意:Windows venv 的 python.exe 是 shim,Popen 返回 pid ≠ 监听 pid;测试须用 psutil.net_connections 端口反查真实 pid
- [x] favicon 抓取 / 图标上传(14/14 通过:受管服务 favicon 抓取落盘/上传/静态访问/删除)
  - 已知限制(既有行为,非移植引入):favicon 只对 **token 受管进程**生效,`attached` 卡片会返回"应用未运行或无可用端口"
- [x] 前端交互冒烟(21/21 通过:渲染/视图切换/命令面板/新端口发现+加入/批量停止/添加表单;Playwright Core + 系统 Chrome,隔离实例)

### P2(体验/工程化)
- [x] git 提交(Windows 移植里程碑,分逻辑提交,2026-08-13)
- [ ] 启动器「重启/打开控制台」选项(对齐 macOS launcher 对话框,可选)
- [ ] 优雅停止(Windows 先尝试软终止再强杀,对需落盘的进程有意义)
- [ ] 原 2687 行 macOS 测试套件 Windows 适配(长期维护才需要)
- [ ] `__pycache__` 清理后注意:运行服务会重新生成(正常,别慌)

## 7. 环境备忘

- 开发/验证:managed venv `C:\Users\Zhou\.workbuddy\binaries\python\envs\default`(Python 3.13.12,已装 psutil)
- 实际运行:`py -3` 在本机解析到 **Python 3.14**(`C:\Users\Zhou\AppData\Local\Python\pythoncore-3.14-64\pythonw.exe`),psutil 7.2.1
- 本机 28 逻辑核(CPU 归一口径依据)
- 测试隔离:设 `CONSOLE_DATA_DIR`/`CONSOLE_LOG_DIR` 环境变量即可不污染真实数据

## 8. 用户偏好(重要)

- **沟通用中文**
- **适配 Windows 用户习惯,而非照搬 macOS**(CPU 口径归一化即据此修改)
- 严谨工程化:改完必须验证(语法 + 实机),不留未验证的"完成"
- 项目文件勿留临时物(__pycache__/调试 bat 一律清理)
