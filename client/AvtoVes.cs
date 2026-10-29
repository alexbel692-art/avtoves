// АвтоВес — программа оператора: отдельное окно с интерфейсом АвтоВес (движок WebView2, как в Edge).
// Собирается build\build.ps1 (csc из .NET Framework). Язык — C# 5, чтобы собирался штатным csc.
//
// Адрес сервера:
//   1) ключ --url http://server:8780/
//   2) реестр HKLM\Software\AvtoVes, значение Url (установка «только программа оператора»)
//   3) порт из C:\ProgramData\AvtoVes\config.yaml (сервер на этом же компьютере)
using System;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Win32;

[assembly: AssemblyTitle("АвтоВес")]
[assembly: AssemblyProduct("АвтоВес")]
[assembly: AssemblyDescription("АвтоВес — рабочее место оператора весовой")]

namespace AvtoVes
{
    static class Native
    {
        [DllImport("user32.dll")] public static extern bool PostMessage(IntPtr hWnd, int msg, IntPtr w, IntPtr l);
        [DllImport("user32.dll", CharSet = CharSet.Unicode)] public static extern int RegisterWindowMessage(string name);
        [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr hWnd);
        public static readonly int WM_SHOWME = RegisterWindowMessage("AvtoVes.ShowMe");
        public static readonly IntPtr HWND_BROADCAST = (IntPtr)0xffff;
    }

    static class Settings
    {
        public const int DefaultPort = 8780;

        public static string DataDir
        {
            get
            {
                string d = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "AvtoVes");
                Directory.CreateDirectory(d);
                return d;
            }
        }

        static string Reg(string name)
        {
            try
            {
                using (RegistryKey hklm = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, RegistryView.Registry64))
                using (RegistryKey k = hklm.OpenSubKey(@"Software\AvtoVes"))
                {
                    if (k == null) return null;
                    object v = k.GetValue(name);
                    return v == null ? null : v.ToString();
                }
            }
            catch { return null; }
        }

        public static string ServerUrl(string[] args)
        {
            for (int i = 0; i < args.Length - 1; i++)
                if (args[i] == "--url") return Normalize(args[i + 1]);
            string url = Reg("Url");
            if (!string.IsNullOrEmpty(url)) return Normalize(url);
            return "http://localhost:" + LocalPort() + "/";
        }

        static string Normalize(string u)
        {
            u = u.Trim();
            if (!u.StartsWith("http://", StringComparison.OrdinalIgnoreCase) &&
                !u.StartsWith("https://", StringComparison.OrdinalIgnoreCase)) u = "http://" + u;
            if (!u.EndsWith("/")) u += "/";
            return u;
        }

        // порт из раздела server: файла настроек сервера
        static int LocalPort()
        {
            string cfg = Reg("Config");
            if (string.IsNullOrEmpty(cfg))
                cfg = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), @"AvtoVes\config.yaml");
            try
            {
                bool inServer = false;
                foreach (string ln in File.ReadAllLines(cfg))
                {
                    if (Regex.IsMatch(ln, @"^server:\s*(#.*)?$")) { inServer = true; continue; }
                    if (inServer && Regex.IsMatch(ln, @"^\S")) break;
                    Match m = Regex.Match(ln, @"^\s+port:\s*(\d+)");
                    if (inServer && m.Success) return int.Parse(m.Groups[1].Value);
                }
            }
            catch { }
            return DefaultPort;
        }
    }

    static class Program
    {
        [STAThread]
        static void Main(string[] args)
        {
            bool created;
            Mutex mutex = new Mutex(true, @"Local\AvtoVes.Client", out created);
            if (!created)
            {
                // программа уже открыта — показываем её окно
                Native.PostMessage(Native.HWND_BROADCAST, Native.WM_SHOWME, IntPtr.Zero, IntPtr.Zero);
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            string url = Settings.ServerUrl(args);
            string ver = null;
            try { ver = CoreWebView2Environment.GetAvailableBrowserVersionString(); } catch { }
            if (string.IsNullOrEmpty(ver))
            {
                Fallback(url);
                return;
            }
            Application.Run(new MainForm(url));
            GC.KeepAlive(mutex);
        }

        // WebView2 нет (старый Windows Server без интернета) — открываем окном Edge или браузером
        static void Fallback(string url)
        {
            string[] edges = {
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86), @"Microsoft\Edge\Application\msedge.exe"),
                Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles), @"Microsoft\Edge\Application\msedge.exe"),
            };
            foreach (string e in edges)
            {
                if (!File.Exists(e)) continue;
                Process.Start(e, "--app=" + url + " --user-data-dir=\"" + Path.Combine(Settings.DataDir, "Edge") + "\"");
                return;
            }
            try { Process.Start(new ProcessStartInfo(url) { UseShellExecute = true }); }
            catch (Exception ex)
            {
                MessageBox.Show("Не удалось открыть АвтоВес: " + ex.Message + "\n" + url, "АвтоВес",
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
            }
        }
    }

    class MainForm : Form
    {
        readonly string url;
        readonly WebView2 web = new WebView2();
        readonly System.Windows.Forms.Timer retry = new System.Windows.Forms.Timer();
        bool waiting;
        bool checking;
        string StateFile { get { return Path.Combine(Settings.DataDir, "window.txt"); } }

        public MainForm(string url)
        {
            this.url = url;
            Text = "АвтоВес";
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }
            BackColor = Color.FromArgb(18, 20, 23);
            MinimumSize = new Size(900, 600);
            StartPosition = FormStartPosition.CenterScreen;
            Size = new Size(1400, 900);
            WindowState = FormWindowState.Maximized;
            RestoreWindow();
            web.Dock = DockStyle.Fill;
            web.DefaultBackgroundColor = Color.FromArgb(18, 20, 23);
            Controls.Add(web);
            retry.Interval = 2000;
            retry.Tick += delegate { CheckServer(); };
            Load += OnLoadAsync;
            FormClosing += delegate { SaveWindow(); };
        }

        async void OnLoadAsync(object sender, EventArgs e)
        {
            try
            {
                CoreWebView2Environment env = await CoreWebView2Environment.CreateAsync(null, Path.Combine(Settings.DataDir, "WebView2"), null);
                await web.EnsureCoreWebView2Async(env);
            }
            catch (Exception ex)
            {
                MessageBox.Show("Не удалось запустить окно АвтоВес: " + ex.Message, "АвтоВес", MessageBoxButtons.OK, MessageBoxIcon.Error);
                Close();
                return;
            }
            CoreWebView2Settings s = web.CoreWebView2.Settings;
            s.IsStatusBarEnabled = false;
            s.AreDevToolsEnabled = false;
            s.IsZoomControlEnabled = true;
            web.CoreWebView2.NavigationCompleted += delegate (object o, CoreWebView2NavigationCompletedEventArgs a)
            {
                if (!a.IsSuccess && !waiting) ShowWaiting();
            };
            // ссылки на другие сайты — во внешнем браузере; талоны и файлы АвтоВес — здесь
            web.CoreWebView2.NewWindowRequested += delegate (object o, CoreWebView2NewWindowRequestedEventArgs a)
            {
                if (!a.Uri.StartsWith(url, StringComparison.OrdinalIgnoreCase))
                {
                    a.Handled = true;
                    try { Process.Start(new ProcessStartInfo(a.Uri) { UseShellExecute = true }); } catch { }
                }
            };
            ShowWaiting();
            CheckServer();
        }

        void ShowWaiting()
        {
            waiting = true;
            web.CoreWebView2.NavigateToString(WaitPage());
            retry.Start();
        }

        void CheckServer()
        {
            if (checking) return;
            checking = true;
            string probe = url + "api/state";
            Task.Factory.StartNew(delegate
            {
                bool ok = false;
                try
                {
                    HttpWebRequest r = (HttpWebRequest)WebRequest.Create(probe);
                    r.Timeout = 1500;
                    r.Proxy = null;
                    using (HttpWebResponse resp = (HttpWebResponse)r.GetResponse()) ok = resp.StatusCode == HttpStatusCode.OK;
                }
                catch { }
                return ok;
            }).ContinueWith(delegate (Task<bool> t)
            {
                checking = false;
                if (!t.Result || IsDisposed) return;
                retry.Stop();
                waiting = false;
                web.CoreWebView2.Navigate(url);
            }, TaskScheduler.FromCurrentSynchronizationContext());
        }

        string WaitPage()
        {
            string u = WebUtility.HtmlEncode(url);
            return "<!doctype html><html><head><meta charset='utf-8'><style>" +
                "body{margin:0;height:100vh;display:grid;place-items:center;background:#121417;color:#e6e8eb;font:15px 'Segoe UI',sans-serif}" +
                ".b{text-align:center;max-width:520px}.t{font-size:22px;font-weight:600;color:#f2a900;margin-bottom:10px}" +
                ".s{width:34px;height:34px;border:3px solid #2e343d;border-top-color:#f2a900;border-radius:50%;margin:22px auto;animation:r 1s linear infinite}" +
                "@keyframes r{to{transform:rotate(360deg)}}.m{color:#8a929e;font-size:13px;line-height:1.5}code{color:#e6e8eb}" +
                "</style></head><body><div class='b'><div class='t'>АвтоВес</div><div>Подключение к серверу…</div><div class='s'></div>" +
                "<div class='m'>Адрес: <code>" + u + "</code><br>Если подключение долго не проходит — проверьте, что служба «АвтоВес» " +
                "запущена на сервере (Пуск → АвтоВес → Перезапустить АвтоВес) и что компьютер в одной сети с сервером.</div></div></body></html>";
        }

        protected override void WndProc(ref Message m)
        {
            if (m.Msg == Native.WM_SHOWME)
            {
                if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Maximized;
                Show();
                Activate();
                Native.SetForegroundWindow(Handle);
            }
            base.WndProc(ref m);
        }

        protected override bool ProcessCmdKey(ref Message msg, Keys keyData)
        {
            if (keyData == Keys.F11)
            {
                bool full = FormBorderStyle != FormBorderStyle.None;
                FormBorderStyle = full ? FormBorderStyle.None : FormBorderStyle.Sizable;
                WindowState = FormWindowState.Normal;
                WindowState = FormWindowState.Maximized;
                return true;
            }
            return base.ProcessCmdKey(ref msg, keyData);
        }

        void RestoreWindow()
        {
            try
            {
                string[] p = File.ReadAllText(StateFile).Split(' ');
                Rectangle r = new Rectangle(int.Parse(p[0]), int.Parse(p[1]), int.Parse(p[2]), int.Parse(p[3]));
                bool visible = false;
                foreach (Screen sc in Screen.AllScreens) if (sc.WorkingArea.IntersectsWith(r)) visible = true;
                if (!visible || r.Width < 400 || r.Height < 300) return;
                StartPosition = FormStartPosition.Manual;
                Bounds = r;
                WindowState = p[4] == "1" ? FormWindowState.Maximized : FormWindowState.Normal;
            }
            catch { }
        }

        void SaveWindow()
        {
            try
            {
                Rectangle r = WindowState == FormWindowState.Normal ? Bounds : RestoreBounds;
                File.WriteAllText(StateFile, r.X + " " + r.Y + " " + r.Width + " " + r.Height + " " +
                    (WindowState == FormWindowState.Maximized ? "1" : "0"));
            }
            catch { }
        }
    }
}
