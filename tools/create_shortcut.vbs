Option Explicit
' 创建桌面快捷方式:双击运行一次,在桌面生成「总控台」快捷方式
' (指向 pythonw 无窗口后台运行,图标为 console.ico;已运行则打开浏览器)
Dim fso, ws, root, pyexe, pythonw, desktop, sc, tmp
Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("WScript.Shell")
root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
tmp = ws.ExpandEnvironmentStrings("%TEMP%") & "\localops_pyexe.txt"
pyexe = ""
' 探测 python(隐藏窗口,无闪窗):py launcher 优先,回退 PATH 中的 python
ws.Run "cmd /c py -3 -c ""import sys;print(sys.executable)"" > """ & tmp & """ 2>nul", 0, True
If fso.FileExists(tmp) Then
  pyexe = Trim(fso.OpenTextFile(tmp, 1).ReadAll())
  fso.DeleteFile tmp
End If
If pyexe = "" Then
  ws.Run "cmd /c python -c ""import sys;print(sys.executable)"" > """ & tmp & """ 2>nul", 0, True
  If fso.FileExists(tmp) Then
    pyexe = Trim(fso.OpenTextFile(tmp, 1).ReadAll())
    fso.DeleteFile tmp
  End If
End If
If pyexe = "" Then
  WScript.Echo "ERROR: 未找到 Python,请先安装 Python 3.12+"
  WScript.Quit 1
End If
pythonw = Replace(pyexe, "python.exe", "pythonw.exe")
desktop = ws.SpecialFolders("Desktop")
Set sc = ws.CreateShortcut(desktop & "\总控台.lnk")
sc.TargetPath = pythonw
sc.Arguments = "server.py --log-to-file"
sc.WorkingDirectory = root
sc.IconLocation = root & "\console.ico"
sc.Description = "LocalOps 总控台(本地服务管理)"
sc.Save
WScript.Echo "OK: 已创建桌面快捷方式「总控台」"
