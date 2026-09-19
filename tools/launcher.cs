// LocalOps console launcher
// Double-click LocalOpsConsole.exe to start the console in background:
// it probes Python (py launcher -> PATH python), starts and verifies the
// background server, then opens only a ready console.
// Build: see build_launcher.bat (uses system .NET Framework csc.exe).
using System;
using System.Diagnostics;
using System.IO;
using System.Text;
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
        bool noBrowser = Array.IndexOf(args, "--no-browser") >= 0;
        string preferredPort = "";
        foreach (string a in args)
        {
            if (a.StartsWith("--preferred-port="))
                preferredPort = a.Substring("--preferred-port=".Length);
        }
        string pyexe = ProbePython();
        if (string.IsNullOrEmpty(pyexe) || !File.Exists(pyexe))
        {
            MessageBox.Show("Python 3.12+ not found. Install it and add to PATH.",
                            "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        string status = RunLauncherCheck(pyexe, root, "status", 10000);
        if (status.StartsWith("RUNNING "))
        {
            if (noBrowser) return 0;
            string port = status.Substring("RUNNING ".Length).Trim();
            string opened = RunLauncherCheck(pyexe, root, "open " + port, 10000);
            if (opened.StartsWith("OPENED ")) return 0;
            MessageBox.Show("The console is running, but its control token is unavailable. " +
                            "Use the tray menu to restart it, or stop it before launching again.",
                            "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Warning);
            return 1;
        }
        // The helper starts candidates without opening a browser, verifies disk-backed
        // card readiness, and retries once if the first process is stale.
        if (!EnsureRuntime(pyexe, root)) return 1;
        string launchArgs = "launch";
        if (preferredPort.Length > 0) launchArgs += " " + preferredPort;
        string launched = RunLauncherCheck(pyexe, root, launchArgs, 45000);
        if (!launched.StartsWith("RUNNING "))
        {
            MessageBox.Show("The console failed its startup readiness check. " +
                            "Your saved cards were not overwritten. See console.log.",
                            "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
        if (noBrowser) return 0;
        string launchedPort = launched.Substring("RUNNING ".Length).Trim();
        string launchOpened = RunLauncherCheck(
            pyexe, root, "open " + launchedPort, 10000);
        return launchOpened.StartsWith("OPENED ") ? 0 : 1;
    }

    private static string RunLauncherCheck(string pyexe, string root,
                                           string arguments, int timeoutMs)
    {
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = pyexe;
            psi.Arguments = "launcher_check.py " + arguments;
            psi.WorkingDirectory = root;
            psi.UseShellExecute = false;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.CreateNoWindow = true;
            using (Process p = Process.Start(psi))
            {
                if (p == null) return "";
                if (!p.WaitForExit(timeoutMs))
                {
                    try { p.Kill(); } catch { }
                    return "";
                }
                string output = p.StandardOutput.ReadToEnd().Trim();
                p.StandardError.ReadToEnd();
                return p.ExitCode == 0 ? output : "";
            }
        }
        catch (Exception)
        {
            return "";
        }
    }

    // python.exe may see user-site psutil that pythonw ignores.
    // Install into THIS interpreter before launching pythonw.
    private static bool EnsureRuntime(string pyexe, string root)
    {
        try
        {
            ProcessStartInfo psi = new ProcessStartInfo();
            psi.FileName = pyexe;
            psi.Arguments = "launcher_check.py ensure-runtime";
            psi.WorkingDirectory = root;
            psi.UseShellExecute = false;
            psi.RedirectStandardOutput = true;
            psi.RedirectStandardError = true;
            psi.CreateNoWindow = true;
            using (Process p = Process.Start(psi))
            {
                if (p == null)
                {
                    MessageBox.Show("Failed to start Python to prepare runtime.",
                                    "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return false;
                }
                StringBuilder sbOut = new StringBuilder();
                StringBuilder sbErr = new StringBuilder();
                p.OutputDataReceived += delegate(object s, DataReceivedEventArgs e)
                {
                    if (e.Data != null) sbOut.AppendLine(e.Data);
                };
                p.ErrorDataReceived += delegate(object s, DataReceivedEventArgs e)
                {
                    if (e.Data != null) sbErr.AppendLine(e.Data);
                };
                p.BeginOutputReadLine();
                p.BeginErrorReadLine();
                if (!p.WaitForExit(120000))
                {
                    try { p.Kill(); } catch { }
                    MessageBox.Show("Timed out installing runtime dependency (psutil).",
                                    "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return false;
                }
                p.WaitForExit();
                if (p.ExitCode != 0)
                {
                    string detail = (sbOut.ToString() + "\n" + sbErr.ToString()).Trim();
                    if (detail.Length == 0)
                        detail = "psutil is required but could not be installed.";
                    if (detail.Length > 1200)
                        detail = detail.Substring(0, 1200);
                    MessageBox.Show(detail,
                                    "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
                    return false;
                }
                return true;
            }
        }
        catch (Exception ex)
        {
            MessageBox.Show("Failed to prepare Python runtime: " + ex.Message,
                            "LocalOps Console", MessageBoxButtons.OK, MessageBoxIcon.Error);
            return false;
        }
    }

    private static string ProbePython()
    {
        string[] commands = { "py -3", "python" };
        foreach (string cmd in commands)
        {
            try
            {
                int sp = cmd.IndexOf(' ');
                string file = (sp < 0) ? cmd : cmd.Substring(0, sp);
                string prefix = (sp < 0) ? "" : (cmd.Substring(sp + 1) + " ");
                // Require Python 3.12+ so a WindowsApps stub or old install is skipped.
                string args = prefix + "-c \"import sys;print(sys.executable) if sys.version_info >= (3,12) else None\"";
                ProcessStartInfo psi = new ProcessStartInfo(file, args);
                psi.UseShellExecute = false;
                psi.RedirectStandardOutput = true;
                psi.RedirectStandardError = true;
                psi.CreateNoWindow = true;
                using (Process p = Process.Start(psi))
                {
                    string outText = p.StandardOutput.ReadToEnd().Trim();
                    p.StandardError.ReadToEnd();
                    if (p.WaitForExit(5000) && outText.Length > 0
                        && outText != "None" && File.Exists(outText))
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
