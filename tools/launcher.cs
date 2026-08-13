// LocalOps console launcher
// Double-click LocalOpsConsole.exe to start the console in background:
// it probes pythonw (py launcher -> PATH python), starts
// "pythonw server.py --log-to-file" with no window, and exits.
// If the console is already running, server.py itself opens the browser.
// Build: see build_launcher.bat (uses system .NET Framework csc.exe).
using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

public static class Launcher
{
    public static int Main(string[] args)
    {
        string root = AppDomain.CurrentDomain.BaseDirectory;
        if (!File.Exists(Path.Combine(root, "server.py")))
        {
            MessageBox.Show("LocalOpsConsole.exe must run from the project root.",
                            "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
        // 参数透传:支持 --no-browser(静默启动,不自动打开浏览器)等 server 参数。
        string extra = "";
        foreach (string a in args)
        {
            if (a == "--no-browser" || a.StartsWith("--preferred-port="))
            {
                extra += " " + a;
            }
        }
        string pyexe = ProbePython();
        if (string.IsNullOrEmpty(pyexe) || !File.Exists(pyexe))
        {
            MessageBox.Show("Python 3.12+ not found. Install it and add to PATH.",
                            "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        string pythonw = pyexe.Replace("python.exe", "pythonw.exe");
        if (!File.Exists(pythonw)) pythonw = pyexe;
        Process p = new Process();
        p.StartInfo.FileName = pythonw;
        p.StartInfo.Arguments = "server.py --log-to-file" + extra;
        p.StartInfo.WorkingDirectory = root;
        p.StartInfo.UseShellExecute = false;
        p.Start();
        return 0;
    }

    private static string ProbePython()
    {
        string[] commands = { "py -3", "python" };
        foreach (string cmd in commands)
        {
            int sp = cmd.IndexOf(' ');
            string file = cmd.Substring(0, sp);
            string args = cmd.Substring(sp + 1) + " -c \"import sys;print(sys.executable)\"";
            try
            {
                ProcessStartInfo psi = new ProcessStartInfo(file, args);
                psi.UseShellExecute = false;
                psi.RedirectStandardOutput = true;
                psi.CreateNoWindow = true;
                using (Process p = Process.Start(psi))
                {
                    string outText = p.StandardOutput.ReadToEnd().Trim();
                    if (p.WaitForExit(5000) && outText.Length > 0 && File.Exists(outText))
                        return outText;
                }
            }
            catch (Exception)
            {
                // try next candidate
            }
        }
        return null;
    }
}
