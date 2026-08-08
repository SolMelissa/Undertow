// App-frame launcher for Undertow: starts the Python backend headless (no browser
// tab), then shows its web dashboard inside a native window via WebView2 - so pinning this
// .exe to Start/Taskbar behaves like a real app instead of "opens a browser tab". Closing the
// window triggers a full shutdown (Hydrus, daemon, systray, VeraCrypt dismount) via the
// backend's /shutdown-full route, rather than just hiding the dashboard.
using System.Diagnostics;
using System.Net.Http;
using Microsoft.Web.WebView2.WinForms;

namespace UndertowLauncher;

internal static class Program
{
    private const int Port = 8765;
    private static readonly string DashboardUrl = $"http://127.0.0.1:{Port}/";

    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();

        string baseDir = AppContext.BaseDirectory;
        Process? backend = StartBackendIfNeeded(baseDir);

        using var form = new MainForm(DashboardUrl, backend);
        Application.Run(form);
    }

    private static Process? StartBackendIfNeeded(string baseDir)
    {
        if (IsDashboardAlreadyUp())
            return null; // Already running (e.g. launched earlier) - just attach a window to it.

        string venvPython = Path.Combine(baseDir, ".venv", "Scripts", "python.exe");
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string fallbackVenvPython = Path.Combine(localAppData, "Undertow", "venv", "Scripts", "python.exe");

        string python = File.Exists(venvPython) ? venvPython
            : File.Exists(fallbackVenvPython) ? fallbackVenvPython
            : "python";

        // Backend output was previously discarded entirely (UseShellExecute=false with no
        // redirection just sends it nowhere) - if "python -m undertow" failed immediately
        // (missing dependency, import error, ...) there was no way to tell why the dashboard
        // never came up. Redirecting to a log file next to the exe makes that diagnosable.
        string logDir = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Undertow", "logs");
        Directory.CreateDirectory(logDir);
        string stdoutLog = Path.Combine(logDir, "launcher-backend-stdout.log");
        string stderrLog = Path.Combine(logDir, "launcher-backend-stderr.log");

        var psi = new ProcessStartInfo
        {
            FileName = python,
            Arguments = "-m undertow",
            WorkingDirectory = baseDir,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        psi.EnvironmentVariables["UNDERTOW_NO_BROWSER"] = "1";

        var process = Process.Start(psi);
        if (process is not null)
        {
            var stdoutWriter = new StreamWriter(File.Open(stdoutLog, FileMode.Create, FileAccess.Write, FileShare.Read)) { AutoFlush = true };
            var stderrWriter = new StreamWriter(File.Open(stderrLog, FileMode.Create, FileAccess.Write, FileShare.Read)) { AutoFlush = true };
            process.OutputDataReceived += (_, e) => { if (e.Data is not null) stdoutWriter.WriteLine(e.Data); };
            process.ErrorDataReceived += (_, e) => { if (e.Data is not null) stderrWriter.WriteLine(e.Data); };
            process.BeginOutputReadLine();
            process.BeginErrorReadLine();
            process.Exited += (_, _) => { stdoutWriter.Dispose(); stderrWriter.Dispose(); };
            process.EnableRaisingEvents = true;
        }
        return process;
    }

    private static bool IsDashboardAlreadyUp()
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(1) };
            var resp = client.GetAsync(DashboardUrl).GetAwaiter().GetResult();
            return resp.IsSuccessStatusCode;
        }
        catch
        {
            return false;
        }
    }
}

internal sealed class MainForm : Form
{
    private readonly string _dashboardUrl;
    private readonly Process? _backend;
    private readonly WebView2 _webView;
    private readonly Label _statusLabel;
    private bool _shutdownRequested;

    public MainForm(string dashboardUrl, Process? backend)
    {
        _dashboardUrl = dashboardUrl;
        _backend = backend;

        Text = "Undertow";
        Width = 1280;
        Height = 860;
        StartPosition = FormStartPosition.CenterScreen;

        _statusLabel = new Label
        {
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font(Font.FontFamily, 12),
            Text = "Starting Undertow...",
        };
        Controls.Add(_statusLabel);

        _webView = new WebView2 { Dock = DockStyle.Fill, Visible = false };
        Controls.Add(_webView);

        FormClosing += OnFormClosing;
        Load += async (_, _) => await WaitForDashboardAndLoadAsync();
    }

    private async Task WaitForDashboardAndLoadAsync()
    {
        using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(2) };
        // Backend startup (VeraCrypt mount, Hydrus, hydownloader daemon, subscription sync)
        // can legitimately run past 60s on a slow/first-time boot - the old fixed 60s deadline
        // just navigated to the dashboard URL regardless of whether it ever came up, which is
        // what produced the browser's own ERR_CONNECTION_REFUSED page instead of a real error.
        var deadline = DateTime.UtcNow.AddSeconds(180);
        bool up = false;

        while (DateTime.UtcNow < deadline)
        {
            if (_backend is { HasExited: true })
            {
                _statusLabel.Text = "Undertow's backend exited unexpectedly (code " + _backend.ExitCode + ") " +
                    "before the dashboard came up.\n\nCheck %LocalAppData%\\Undertow\\logs\\launcher-backend-stderr.log " +
                    "for details.";
                return;
            }

            try
            {
                var resp = await client.GetAsync(_dashboardUrl);
                if (resp.IsSuccessStatusCode)
                {
                    up = true;
                    break;
                }
            }
            catch
            {
                // Not up yet - keep polling.
            }
            await Task.Delay(500);
        }

        if (!up)
        {
            _statusLabel.Text = "Undertow's dashboard didn't come up within 180s.\n\n" +
                "Check %LocalAppData%\\Undertow\\logs\\launcher-backend-stderr.log for details, " +
                "or that Hydrus/hydownloader aren't stuck waiting on a VeraCrypt password prompt.";
            return;
        }

        try
        {
            await _webView.EnsureCoreWebView2Async();
            _webView.CoreWebView2.Navigate(_dashboardUrl);
            _webView.Visible = true;
            _statusLabel.Visible = false;
        }
        catch (Exception ex)
        {
            _statusLabel.Text = "Couldn't start the WebView2 runtime:\n" + ex.Message +
                "\n\nIt ships with modern Windows/Edge - install the \"Evergreen\" WebView2 " +
                "runtime from Microsoft if this keeps happening.";
        }
    }

    private void OnFormClosing(object? sender, FormClosingEventArgs e)
    {
        if (_shutdownRequested)
            return; // Second close (e.g. after the async work below finishes) - let it through.

        e.Cancel = true;
        _shutdownRequested = true;
        _ = ShutdownAndCloseAsync();
    }

    private async Task ShutdownAndCloseAsync()
    {
        _statusLabel.Text = "Shutting down Undertow...";
        _statusLabel.Visible = true;
        _webView.Visible = false;

        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            await client.PostAsync(_dashboardUrl.TrimEnd('/') + "/shutdown-full", content: null);
        }
        catch
        {
            // Backend may already be down, or slow to answer - either way, don't block closing.
        }

        Close();
    }
}
