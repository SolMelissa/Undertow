// App-frame launcher for Undertow: starts the Python backend headless (no browser
// tab), then shows its web dashboard inside a native window via WebView2 - so pinning this
// .exe to Start/Taskbar behaves like a real app instead of "opens a browser tab". Closing the
// window triggers a full shutdown (Hydrus, daemon, systray, VeraCrypt dismount) via the
// backend's /shutdown-full route, rather than just hiding the dashboard.
using System.Diagnostics;
using System.Net.Http;
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
            return (null, output); // Already running (e.g. launched earlier) - just attach a window to it.

        string venvPython = Path.Combine(baseDir, ".venv", "Scripts", "python.exe");
        string localAppData = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        string fallbackVenvPython = Path.Combine(localAppData, "Undertow", "venv", "Scripts", "python.exe");

        string python = File.Exists(venvPython) ? venvPython
            : File.Exists(fallbackVenvPython) ? fallbackVenvPython
            : "python";

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
        process.OutputDataReceived += (_, e) => output.Emit(e.Data ?? "");
        process.ErrorDataReceived += (_, e) => output.Emit(e.Data ?? "");
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

    private readonly string _dashboardUrl;
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
        var deadline = DateTime.UtcNow.AddSeconds(90);

        while (DateTime.UtcNow < deadline)
        {
            if (_backend is not null && _backend.HasExited)
            {
                _statusLabel.Text = $"Undertow's backend exited unexpectedly (code {_backend.ExitCode}) " +
                    "before the dashboard came up - check the console/log output for the actual error.";
                _progressBar.Visible = false;
                return;
            }

            try
            {
                var resp = await client.GetAsync(_dashboardUrl);
                if (resp.IsSuccessStatusCode)
                    break;
            }
            catch
            {
                // Not up yet - keep polling.
            }
            await Task.Delay(500);
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
