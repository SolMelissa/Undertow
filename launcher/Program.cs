// App-frame launcher for Undertow: starts the Python backend headless (no browser
// tab), then shows its web dashboard inside a native window via WebView2 - so pinning this
// .exe to Start/Taskbar behaves like a real app instead of "opens a browser tab". Closing the
// window triggers a full shutdown (Hydrus, daemon, systray, VeraCrypt dismount) via the
// backend's /shutdown-full route, rather than just hiding the dashboard.
using System.Diagnostics;
using System.Net.Http;
using System.Text.RegularExpressions;
using Microsoft.Web.WebView2.WinForms;

namespace UndertowLauncher;

/// Forwards the backend's stdout/stderr lines to whoever is listening (the startup UI) -
/// decoupled from MainForm so the process can be started and its output reader attached
/// before the form even exists, without losing any lines emitted in between.
internal sealed class BackendOutput
{
    public event Action<string>? LineReceived;

    public void Emit(string line)
    {
        if (!string.IsNullOrWhiteSpace(line))
            LineReceived?.Invoke(line);
    }
}

internal static class Program
{
    // Default/fallback port - what the backend uses absent any trouble. It can talk itself out
    // of this (see undertow/webui.py's run_webui) if the port is stuck in an unrecoverable dead
    // state at the OS level (observed live: a listening socket whose owning process no longer
    // exists, yet Windows kept it bound and swallowing every new connection forever - not
    // something any process-level cleanup here or in Python can always fix). MainForm parses
    // the real port the backend reports and polls/navigates to that instead, so this constant
    // only matters for the "already running" fast-path check below, before any such fallback
    // could be known about.
    private const int Port = 8765;
    private static readonly string DashboardUrl = $"http://127.0.0.1:{Port}/";

    [STAThread]
    private static void Main()
    {
        ApplicationConfiguration.Initialize();

        string baseDir = AppContext.BaseDirectory;
        var (backend, output) = StartBackendIfNeeded(baseDir);

        using var form = new MainForm(DashboardUrl, backend, output);
        Application.Run(form);
    }

    private static (Process? backend, BackendOutput output) StartBackendIfNeeded(string baseDir)
    {
        var output = new BackendOutput();

        if (IsDashboardAlreadyUp())
        {
            // Already running (e.g. launched earlier) - just attach a window to it. But the
            // backend process being alive doesn't mean Hydrus/the VeraCrypt volume still are;
            // either can get closed/dismounted independently (manually, sleep/wake, a crash)
            // while the headless backend keeps serving the dashboard. Since menu.main()'s
            // start_required_services() only ever runs once per backend process launch, that
            // self-healing would otherwise never fire again for the rest of this backend's
            // life. Re-run it now via the same route the dashboard's own Diagnostics ->
            // "Restart services" button uses - fire-and-forget, so a slow/unreachable backend
            // doesn't delay showing the window.
            _ = TryEnsureServicesRunningAsync();
            return (null, output);
        }

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
            StandardOutputEncoding = System.Text.Encoding.UTF8,
            StandardErrorEncoding = System.Text.Encoding.UTF8,
        };
        psi.EnvironmentVariables["UNDERTOW_NO_BROWSER"] = "1";
        // The backend buffers stdout when it isn't attached to a real console (see
        // hide_console_window's docstring for the analogous concern) - force it unbuffered so
        // the startup print()s here arrive as they happen instead of only once Python's
        // internal buffer fills or the process exits.
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";

        var process = new Process { StartInfo = psi, EnableRaisingEvents = true };
        var stdoutWriter = new StreamWriter(File.Open(stdoutLog, FileMode.Create, FileAccess.Write, FileShare.Read)) { AutoFlush = true };
        var stderrWriter = new StreamWriter(File.Open(stderrLog, FileMode.Create, FileAccess.Write, FileShare.Read)) { AutoFlush = true };
        process.OutputDataReceived += (_, e) => { if (e.Data is not null) stdoutWriter.WriteLine(e.Data); output.Emit(e.Data ?? ""); };
        process.ErrorDataReceived += (_, e) => { if (e.Data is not null) stderrWriter.WriteLine(e.Data); output.Emit(e.Data ?? ""); };
        process.Exited += (_, _) => { stdoutWriter.Dispose(); stderrWriter.Dispose(); };
        process.Start();
        // Attached immediately after Start() (not deferred to MainForm construction) so no
        // startup output is lost to the OS pipe buffer before a reader is listening.
        process.BeginOutputReadLine();
        process.BeginErrorReadLine();

        return (process, output);
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

    private static async Task TryEnsureServicesRunningAsync()
    {
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(60) };
            await client.PostAsync(DashboardUrl.TrimEnd('/') + "/diagnostics/restart-services", content: null);
        }
        catch
        {
            // Best-effort - if the backend is unreachable or slow, the dashboard window still
            // opens and its own Diagnostics modal remains available to retry manually.
        }
    }
}

internal sealed class MainForm : Form
{
    // Known startup milestones from undertow/menu.py and undertow/services.py's print()
    // calls, in the order they're actually emitted, mapped to how far through startup each
    // one represents. Matched by substring (the real lines have extra detail, e.g. process
    // counts) rather than exact text, so this stays tolerant of minor wording changes.
    private static readonly (string Match, int Percent)[] Milestones =
    {
        ("Checking Hydrus pipeline services", 5),
        ("isn't mounted", 10),
        ("is mounted", 15),
        ("starting Hydrus", 20),
        ("Hydrus already running", 20),
        ("cleared", 30),
        ("starting hydownloader daemon", 35),
        ("hydownloader daemon already running", 35),
        ("starting hydownloader systray", 45),
        ("hydownloader systray already running", 45),
        ("Making sure every subscription is grouped", 55),
        ("Prioritizing least-recently-succeeded", 70),
        ("Ready - starting the web dashboard", 85),
        ("web dashboard running at", 95),
    };

    // Not readonly: OnBackendLine can repoint this at whatever port the backend actually ends
    // up serving on (see PortRe below) if it had to fall back off the default port.
    private string _dashboardUrl;
    private static readonly Regex PortRe = new(@"running at http://127\.0\.0\.1:(\d+)", RegexOptions.Compiled);
    private readonly Process? _backend;
    private readonly BackendOutput? _output;
    private readonly WebView2 _webView;
    private readonly Label _statusLabel;
    private readonly ProgressBar _progressBar;
    private bool _shutdownRequested;

    public MainForm(string dashboardUrl, Process? backend, BackendOutput? output)
    {
        _dashboardUrl = dashboardUrl;
        _backend = backend;
        _output = output;

        Text = "Undertow";
        Width = 1280;
        Height = 860;
        StartPosition = FormStartPosition.CenterScreen;

        _progressBar = new ProgressBar
        {
            Dock = DockStyle.Bottom,
            Height = 6,
            Style = ProgressBarStyle.Marquee,
            MarqueeAnimationSpeed = 30,
            Minimum = 0,
            Maximum = 100,
        };
        Controls.Add(_progressBar);

        _statusLabel = new Label
        {
            Dock = DockStyle.Fill,
            TextAlign = ContentAlignment.MiddleCenter,
            Font = new Font(Font.FontFamily, 12),
            Text = backend is null ? "Attaching to the already-running backend..." : "Starting Undertow...",
        };
        Controls.Add(_statusLabel);

        _webView = new WebView2 { Dock = DockStyle.Fill, Visible = false };
        Controls.Add(_webView);

        if (_output is not null)
            _output.LineReceived += OnBackendLine;

        FormClosing += OnFormClosing;
        Load += async (_, _) => await WaitForDashboardAndLoadAsync();
    }

    private void OnBackendLine(string line)
    {
        if (IsDisposed)
            return;

        void Apply()
        {
            if (IsDisposed)
                return;

            string trimmed = line.Trim();
            if (trimmed.Length == 0)
                return;

            var portMatch = PortRe.Match(trimmed);
            if (portMatch.Success)
                _dashboardUrl = $"http://127.0.0.1:{portMatch.Groups[1].Value}/";

            foreach (var (match, percent) in Milestones)
            {
                if (trimmed.Contains(match, StringComparison.OrdinalIgnoreCase))
                {
                    SetProgress(percent);
                    break;
                }
            }

            _statusLabel.Text = trimmed;
        }

        if (InvokeRequired)
            BeginInvoke(Apply);
        else
            Apply();
    }

    private void SetProgress(int percent)
    {
        if (_progressBar.Style != ProgressBarStyle.Continuous)
            _progressBar.Style = ProgressBarStyle.Continuous;
        // Never step backward - lines can arrive slightly out of the declared order (e.g.
        // stderr interleaving with stdout), and a progress bar visibly rewinding reads as
        // broken even when startup is actually fine.
        if (percent > _progressBar.Value)
            _progressBar.Value = percent;
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
                _progressBar.Visible = false;
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

        SetProgress(100);
        _statusLabel.Text = "Loading dashboard...";

        try
        {
            await _webView.EnsureCoreWebView2Async();
            _webView.CoreWebView2.Navigate(_dashboardUrl);
            _webView.Visible = true;
            _statusLabel.Visible = false;
            _progressBar.Visible = false;
        }
        catch (Exception ex)
        {
            _statusLabel.Text = "Couldn't start the WebView2 runtime:\n" + ex.Message +
                "\n\nIt ships with modern Windows/Edge - install the \"Evergreen\" WebView2 " +
                "runtime from Microsoft if this keeps happening.";
            _progressBar.Visible = false;
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
        if (_output is not null)
            _output.LineReceived -= OnBackendLine;

        _statusLabel.Text = "Shutting down Undertow...";
        _statusLabel.Visible = true;
        _webView.Visible = false;
        _progressBar.Visible = true;
        _progressBar.Style = ProgressBarStyle.Marquee;

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
