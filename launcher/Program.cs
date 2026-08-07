// App-frame launcher for the Hydrus Pipeline: starts the Python backend headless (no browser
// tab), then shows its web dashboard inside a native window via WebView2 - so pinning this
// .exe to Start/Taskbar behaves like a real app instead of "opens a browser tab". Closing the
// window triggers a full shutdown (Hydrus, daemon, systray, VeraCrypt dismount) via the
// backend's /shutdown-full route, rather than just hiding the dashboard.
using System.Diagnostics;
using System.Net.Http;
using Microsoft.Web.WebView2.WinForms;

namespace HydrusPipelineLauncher;

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
        string fallbackVenvPython = Path.Combine(localAppData, "HydrusPipeline", "venv", "Scripts", "python.exe");

        string python = File.Exists(venvPython) ? venvPython
            : File.Exists(fallbackVenvPython) ? fallbackVenvPython
            : "python";

        var psi = new ProcessStartInfo
        {
            FileName = python,
            Arguments = "-m hydrus_pipeline",
            WorkingDirectory = baseDir,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        psi.EnvironmentVariables["HYDRUS_PIPELINE_NO_BROWSER"] = "1";

        return Process.Start(psi);
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
            Text = "Starting Hydrus Pipeline...",
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
        var deadline = DateTime.UtcNow.AddSeconds(60);

        while (DateTime.UtcNow < deadline)
        {
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
        _statusLabel.Text = "Shutting down Hydrus Pipeline...";
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
