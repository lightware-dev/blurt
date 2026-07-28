using System.IO;
using System.IO.Compression;
using System.Net;
using System.Net.Http;
using System.Reflection;
using System.Text.Json;

namespace Blurt;

/// In-app updater: checks GitHub Releases for a newer build, downloads the Windows
/// zip, and swaps the running exe in place. There is no Mac twin — the macOS client
/// updates through Homebrew.
///
/// Self-replacing a running exe uses the standard Windows trick: you can't overwrite
/// a locked, running exe, but you *can* rename it. So `ApplyUpdate` renames the live
/// `Blurt.exe` to `Blurt.old.exe`, drops the freshly-downloaded build into the
/// original path, and the relaunched instance deletes the leftover `.old.exe` on its
/// next startup (`CleanupOldExe`). The app ships as a portable single-file exe the
/// user runs from wherever they unzipped it, so no elevation is normally needed; if a
/// write is denied (e.g. the exe lives under Program Files) the caller surfaces the
/// error and points the user at the Releases page.
internal static class Updater
{
    private const string ApiLatest = "https://api.github.com/repos/lightware-dev/blurt/releases/latest";
    private const string ReleasesPage = "https://github.com/lightware-dev/blurt/releases/latest";
    // The stable, version-less asset the release workflow always attaches; we fall
    // back to any "*-Windows.zip" if it's ever renamed.
    private const string AssetName = "Blurt-Windows.zip";
    private const string OldExeSuffix = ".old.exe";

    public static readonly string ReleasesUrl = ReleasesPage;

    /// The running build's version, normalised to four components so comparisons
    /// against a parsed tag never trip over an unspecified (-1) component.
    public static Version CurrentVersion { get; } =
        Normalize(Assembly.GetEntryAssembly()?.GetName().Version ?? new Version(0, 0));

    /// A newer release than the one running, or null if we're current / offline /
    /// there's no Windows asset to install.
    public sealed record Release(string Tag, Version Version, string DownloadUrl);

    /// Query GitHub for the latest release and return it only if it's strictly newer
    /// than the running build. Throws on a network/HTTP failure so a *manual* check
    /// can report it; the startup check swallows that by passing silent.
    public static async Task<Release?> CheckAsync()
    {
        using var http = MakeClient(TimeSpan.FromSeconds(15));
        using var resp = await http.GetAsync(ApiLatest).ConfigureAwait(false);
        if (resp.StatusCode == HttpStatusCode.NotFound) return null; // repo has no releases yet
        resp.EnsureSuccessStatusCode();

        var json = await resp.Content.ReadAsStringAsync().ConfigureAwait(false);
        using var doc = JsonDocument.Parse(json);
        var root = doc.RootElement;

        var tag = root.TryGetProperty("tag_name", out var t) ? t.GetString() ?? "" : "";
        if (!TryParseVersion(tag, out var latest)) return null;
        if (latest <= CurrentVersion) return null;

        // Prefer the stable "Blurt-Windows.zip"; otherwise take any "*-Windows.zip".
        string? url = null;
        if (root.TryGetProperty("assets", out var assets) && assets.ValueKind == JsonValueKind.Array)
        {
            foreach (var a in assets.EnumerateArray())
            {
                var name = a.TryGetProperty("name", out var n) ? n.GetString() ?? "" : "";
                var dl = a.TryGetProperty("browser_download_url", out var d) ? d.GetString() : null;
                if (dl is null) continue;
                if (string.Equals(name, AssetName, StringComparison.OrdinalIgnoreCase)) { url = dl; break; }
                if (name.EndsWith("-Windows.zip", StringComparison.OrdinalIgnoreCase)) url ??= dl;
            }
        }
        return url is null ? null : new Release(tag, latest, url);
    }

    /// Download the release zip to a temp folder, extract it, and return the path to
    /// the new Blurt.exe. Nothing on disk near the running app is touched yet.
    public static async Task<string> DownloadAsync(Release release)
    {
        using var http = MakeClient(TimeSpan.FromMinutes(5));

        var work = Path.Combine(Path.GetTempPath(), "Blurt-update-" + Guid.NewGuid().ToString("N"));
        Directory.CreateDirectory(work);
        var zipPath = Path.Combine(work, AssetName);

        await using (var src = await http.GetStreamAsync(release.DownloadUrl).ConfigureAwait(false))
        await using (var dst = File.Create(zipPath))
            await src.CopyToAsync(dst).ConfigureAwait(false);

        var extractDir = Path.Combine(work, "extracted");
        ZipFile.ExtractToDirectory(zipPath, extractDir);

        // Only ever install a file actually named Blurt.exe. There is deliberately no
        // "any *.exe in the archive" fallback: if the package layout ever changes,
        // failing loudly is correct — quietly installing whatever executable happens to
        // be in the zip is not.
        return Directory.EnumerateFiles(extractDir, "Blurt.exe", SearchOption.AllDirectories).FirstOrDefault()
            ?? throw new InvalidOperationException("The update package didn't contain Blurt.exe.");
    }

    /// Rename the running exe out of the way and copy the new build into its place.
    /// Returns the (unchanged) path the app runs from, ready to relaunch. Rolls the
    /// rename back if the copy fails, so the app is never left without an exe.
    public static string ApplyUpdate(string newExe)
    {
        var exePath = Environment.ProcessPath
            ?? throw new InvalidOperationException("Couldn't determine the running executable's path.");
        var backup = BackupPath(exePath);

        if (File.Exists(backup)) TryDelete(backup);
        File.Move(exePath, backup);          // renaming a running exe is allowed on Windows
        try
        {
            File.Copy(newExe, exePath, overwrite: false);
        }
        catch
        {
            if (!File.Exists(exePath)) File.Move(backup, exePath); // roll back
            throw;
        }
        return exePath;
    }

    /// Launch the freshly-installed exe (used right before shutting the old one down).
    public static void Launch(string exePath) =>
        System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo(exePath) { UseShellExecute = true });

    /// Delete the `Blurt.old.exe` left behind by a previous update. Safe to call on
    /// every startup; retries briefly since the just-exited old process can hold the
    /// file for a moment. Meant to run off the UI thread.
    public static void CleanupOldExe()
    {
        try
        {
            var exePath = Environment.ProcessPath;
            if (exePath is null) return;
            var backup = BackupPath(exePath);
            if (File.Exists(backup)) TryDelete(backup);
        }
        catch { /* best-effort cleanup */ }
    }

    // ── helpers ──────────────────────────────────────────────

    private static HttpClient MakeClient(TimeSpan timeout)
    {
        var http = new HttpClient { Timeout = timeout };
        // GitHub rejects requests without a User-Agent; the API version header keeps
        // the JSON shape stable.
        http.DefaultRequestHeaders.UserAgent.ParseAdd("Blurt-Updater");
        http.DefaultRequestHeaders.Accept.ParseAdd("application/vnd.github+json");
        return http;
    }

    private static string BackupPath(string exePath) =>
        Path.Combine(Path.GetDirectoryName(exePath)!,
                     Path.GetFileNameWithoutExtension(exePath) + OldExeSuffix);

    private static void TryDelete(string path)
    {
        for (int i = 0; i < 15; i++)
        {
            try { File.Delete(path); return; }
            catch { System.Threading.Thread.Sleep(150); }
        }
    }

    /// Parse a release tag like "v1.2" / "1.2.0" / "v2" into a 4-component Version,
    /// ignoring any pre-release suffix ("v1.2-beta" → 1.2).
    private static bool TryParseVersion(string tag, out Version version)
    {
        version = new Version(0, 0, 0, 0);
        var s = tag.Trim().TrimStart('v', 'V').Trim();
        int i = 0;
        while (i < s.Length && (char.IsDigit(s[i]) || s[i] == '.')) i++;
        s = s[..i].Trim('.');
        if (s.Length == 0) return false;
        if (!s.Contains('.')) s += ".0";     // "2" → "2.0" so Version.TryParse accepts it
        if (!Version.TryParse(s, out var parsed)) return false;
        version = Normalize(parsed);
        return true;
    }

    private static Version Normalize(Version v) =>
        new(v.Major, v.Minor, Math.Max(v.Build, 0), Math.Max(v.Revision, 0));
}
