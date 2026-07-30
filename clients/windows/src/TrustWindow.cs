using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Blurt;

/// The certificate trust dialog — the Windows stand-in for the Mac client's
/// NSAlert in CertTrust.swift, built on the same bones as PromptWindow. Shows
/// the host and the SHA-256 fingerprint the user is being asked to vouch for,
/// and returns whether they did.
internal sealed class TrustWindow : Window
{
    private TrustWindow(string title, string body, Brush accent, IEnumerable<(string, string)> facts,
                        string acceptText, bool acceptIsDefault)
    {
        Title = "Blurt";
        Width = 520;
        SizeToContent = SizeToContent.Height;
        WindowStyle = WindowStyle.ToolWindow;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        ResizeMode = ResizeMode.NoResize;
        Background = Brand.Ink950Brush;

        var root = new StackPanel { Margin = new Thickness(20) };
        root.Children.Add(new TextBlock
        {
            Text = title,
            FontFamily = Brand.Display,
            FontSize = 16,
            FontWeight = FontWeights.Bold,
            Foreground = accent,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 0, 0, 10),
        });
        root.Children.Add(new TextBlock
        {
            Text = body,
            FontFamily = Brand.Display,
            FontSize = 13,
            Foreground = Brand.Solid(Brand.Bone, 0.8),
            TextWrapping = TextWrapping.Wrap,
            LineHeight = 13 * 1.45,
        });

        foreach (var (label, value) in facts)
        {
            root.Children.Add(new TextBlock
            {
                Text = label,
                FontFamily = Brand.Display,
                FontSize = 12,
                Foreground = Brand.BoneDimBrush,
                Margin = new Thickness(0, 12, 0, 4),
            });
            root.Children.Add(new TextBlock
            {
                Text = value,
                FontFamily = Brand.Mono,
                FontSize = 12,
                Foreground = Brand.BoneBrush,
                TextWrapping = TextWrapping.Wrap,
                Background = Brand.Ink900Brush,
                Padding = new Thickness(8, 6, 8, 6),
            });
        }

        var buttons = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new Thickness(0, 18, 0, 0),
        };

        // Cancel first (left) so the safe answer sits where the eye lands last on
        // the way to the accent button.
        var cancel = MakeButton("Cancel", filled: false);
        cancel.IsCancel = true;
        cancel.IsDefault = !acceptIsDefault;
        cancel.Click += (_, _) => { DialogResult = false; Close(); };
        buttons.Children.Add(cancel);

        if (acceptText.Length > 0)
        {
            var accept = MakeButton(acceptText, filled: true, fill: accent);
            accept.IsDefault = acceptIsDefault;
            accept.Click += (_, _) => { DialogResult = true; Close(); };
            buttons.Children.Add(accept);
        }

        root.Children.Add(buttons);
        Content = root;
    }

    private static Button MakeButton(string text, bool filled, Brush? fill = null) => new()
    {
        Content = text,
        MinWidth = 84,
        Margin = new Thickness(8, 0, 0, 0),
        Padding = new Thickness(14, 7, 14, 7),
        FontFamily = Brand.Display,
        FontWeight = FontWeights.Bold,
        Cursor = System.Windows.Input.Cursors.Hand,
        Foreground = filled ? Brand.Ink950Brush : Brand.BoneBrush,
        Background = filled ? fill ?? Brand.MarkerBrush : Brushes.Transparent,
        BorderBrush = Brand.Ink600Brush,
        BorderThickness = new Thickness(filled ? 0 : 1),
    };

    /// Shows the dialog for a decision and returns true if the user vouched for
    /// the certificate. UI thread only.
    public static bool Ask(TrustDecision decision)
    {
        var window = decision.Kind switch
        {
            TrustKind.FirstUse => new TrustWindow(
                "Trust this server's certificate?",
                $"Blurt is connecting to {decision.Key} for the first time. Its certificate " +
                "is self-signed, so there's no authority to check it against.\n\n" +
                "Trust it only if this is your own Blurt server.",
                Brand.MarkerBrush,
                new[] { ("SHA-256 fingerprint", Pretty(decision.Fingerprint)) },
                acceptText: "Trust", acceptIsDefault: true),

            TrustKind.Changed => new TrustWindow(
                $"The certificate for {decision.Host} has changed",
                "Only continue if you regenerated the certificate on your Blurt server " +
                "yourself. Otherwise something else could be impersonating it.",
                Brand.CoralBrush,
                new[]
                {
                    ("New fingerprint", Pretty(decision.Fingerprint)),
                    ("Previously trusted", Pretty(decision.Pinned ?? "")),
                },
                // No default button on the scary one: re-pinning is a deliberate click.
                acceptText: "Trust New Certificate", acceptIsDefault: false),

            _ => new TrustWindow(
                "Couldn't read the server's certificate",
                $"Blurt got no usable certificate from {decision.Key}, so the connection " +
                "can't be authenticated.",
                Brand.CoralBrush,
                Array.Empty<(string, string)>(),
                acceptText: "", acceptIsDefault: false),
        };

        // Prefer whatever window is actually up (Settings raises this on a field
        // blur, and it isn't MainWindow); an unowned dialog gets its own stray
        // taskbar entry.
        var owner = Application.Current?.Windows.OfType<Window>()
                        .FirstOrDefault(w => w.IsActive && w.IsVisible && w != window)
                    ?? Application.Current?.MainWindow;
        if (owner is { IsVisible: true } && owner != window) window.Owner = owner;
        return window.ShowDialog() == true;
    }

    /// `AB:CD:…` in two lines of 16 bytes, so a 32-byte digest stays readable and
    /// comparable against what the server prints.
    private static string Pretty(string hex)
    {
        var pairs = new List<string>();
        for (var i = 0; i < hex.Length; i += 2)
            pairs.Add(hex.Substring(i, Math.Min(2, hex.Length - i)));
        var lines = new List<string>();
        for (var i = 0; i < pairs.Count; i += 16)
            lines.Add(string.Join(":", pairs.GetRange(i, Math.Min(16, pairs.Count - i))));
        return string.Join("\n", lines);
    }
}
