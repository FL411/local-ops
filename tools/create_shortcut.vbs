Option Explicit
' 创建桌面快捷方式:双击运行一次,在桌面生成「总控台」快捷方式
' (指向 pythonw 无窗口后台运行,图标为 console.ico;已运行则打开浏览器)
Dim fso, ws, root, py, pythonw, desktop, sc
Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
Set exec = ws.Exec("py -3 -c ""import sys;print(sys.executable)""")
py = Trim(exec.StdOut.ReadAll())
If py = "" Then
  Set exec = ws.Exec("python -c ""import sys;print(sys.executable)""")
  py = Trim(exec.StdOut.ReadAll())
End If
If py = "" Then
  WScript.Echo "ERROR: 未找到 Python,请先安装 Python 3.12+"
  WScript.Quit 1
End If
pythonw = Replace(py, "python.exe", "pythonw.exe")
desktop = ws.SpecialFolders("Desktop")
Set sc = ws.CreateShortcut(desktop & "\总控台.lnk")
sc.TargetPath = pythonw
sc.Arguments = "server.py --log-to-file"
sc.WorkingDirectory = root
sc.IconLocation = root & "\console.ico"
sc.Description = "LocalOps 总控台(本地服务管理)"
sc.Save
WScript.Echo "OK " & desktop & "\总控台.lnk"
