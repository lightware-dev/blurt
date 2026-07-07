using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;

namespace Blurt;

/// A tiny branded modal text prompt — the Windows stand-in for the Mac client's
/// NSAlert-with-accessory-field (Set Server URL… / Set Auth Token…). Returns the
/// entered string, or null if cancelled.
internal sealed class PromptWindow : Window
{
    private readonly TextBox _field;

    private PromptWindow(string title, string value, string placeholder)
    {
        Title = title;
        Width = 460;
        SizeToContent = SizeToContent.Height;
        WindowStyle = WindowStyle.ToolWindow;
        WindowStartupLocation = WindowStartupLocation.CenterScreen;
        ResizeMode = ResizeMode.NoResize;
        Background = Brand.Ink950Brush;

        var heading = new TextBlock
        {
            Text = title,
            FontFamily = Brand.Display,
            FontSize = 16,
            FontWeight = FontWeights.Bold,
            Foreground = Brand.BoneBrush,
            Margin = new Thickness(0, 0, 0, 10),
        };

        _field = new TextBox
        {
            Text = value,
            FontFamily = Brand.Mono,
            FontSize = 13,
            Padding = new Thickness(8, 6, 8, 6),
            Background = Brand.Ink900Brush,
            Foreground = Brand.BoneBrush,
            CaretBrush = Brand.MarkerBrush,
            BorderBrush = Brand.Ink700Brush,
            BorderThickness = new Thickness(1),
        };
        if (placeholder.Length > 0) _field.ToolTip = placeholder;

        var save = MakeButton("Save", filled: true);
        save.IsDefault = true;
        save.Click += (_, _) => { DialogResult = true; Close(); };

        var cancel = MakeButton("Cancel", filled: false);
        cancel.IsCancel = true;
        cancel.Click += (_, _) => { DialogResult = false; Close(); };

        var buttons = new StackPanel
        {
            Orientation = Orientation.Horizontal,
            HorizontalAlignment = HorizontalAlignment.Right,
            Margin = new Thickness(0, 14, 0, 0),
        };
        buttons.Children.Add(cancel);
        buttons.Children.Add(save);

        var root = new StackPanel { Margin = new Thickness(20) };
        root.Children.Add(heading);
        root.Children.Add(_field);
        root.Children.Add(buttons);
        Content = root;

        Loaded += (_, _) => { _field.Focus(); _field.SelectAll(); };
    }

    private static Button MakeButton(string text, bool filled)
    {
        var b = new Button
        {
            Content = text,
            MinWidth = 84,
            Margin = new Thickness(8, 0, 0, 0),
            Padding = new Thickness(14, 7, 14, 7),
            FontFamily = Brand.Display,
            FontWeight = FontWeights.Bold,
            Cursor = System.Windows.Input.Cursors.Hand,
            Foreground = filled ? Brand.Ink950Brush : Brand.BoneBrush,
            Background = filled ? Brand.MarkerBrush : Brushes.Transparent,
            BorderBrush = Brand.Ink600Brush,
            BorderThickness = new Thickness(filled ? 0 : 1),
        };
        return b;
    }

    /// Shows the prompt modally and returns the trimmed value, or null if cancelled.
    public static string? Ask(string title, string value, string placeholder = "")
    {
        var win = new PromptWindow(title, value, placeholder);
        if (Application.Current?.MainWindow is { IsVisible: true } owner && owner != win)
            win.Owner = owner;
        return win.ShowDialog() == true ? win._field.Text.Trim() : null;
    }
}
