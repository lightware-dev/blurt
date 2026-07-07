using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;

namespace Blurt;

/// Draws the tray icon at runtime so the app ships as a single self-contained exe
/// with no .ico asset to embed. A simple microphone glyph, bone-white when idle
/// and coral when recording — echoing the Mac client's mic / mic.fill swap.
internal static class IconFactory
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr handle);

    public static Icon Mic(bool recording)
    {
        // Draw large and let the tray downscale — keeps edges smooth on hi-DPI.
        const int s = 32;
        using var bmp = new Bitmap(s, s);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);

            var color = recording ? Brand.GdiCoral : Brand.GdiBone;
            using var pen = new Pen(color, 2.4f) { StartCap = LineCap.Round, EndCap = LineCap.Round };
            using var brush = new SolidBrush(color);

            // Mic capsule (the head).
            var capsule = new RectangleF(12f, 5f, 8f, 14f);
            g.FillPath(brush, RoundedCapsule(capsule));

            // Pickup bracket: an arc cradling the capsule from below.
            g.DrawArc(pen, 9f, 9f, 14f, 14f, 20f, 140f);

            // Stem + base.
            g.DrawLine(pen, 16f, 23f, 16f, 27f);
            g.DrawLine(pen, 11.5f, 27.5f, 20.5f, 27.5f);
        }

        var hIcon = bmp.GetHicon();
        try
        {
            // Clone into a managed Icon so we can free the GDI handle immediately.
            using var tmp = Icon.FromHandle(hIcon);
            return (Icon)tmp.Clone();
        }
        finally { DestroyIcon(hIcon); }
    }

    private static GraphicsPath RoundedCapsule(RectangleF r)
    {
        var path = new GraphicsPath();
        float d = r.Width; // full-width rounding → capsule ends
        path.AddArc(r.X, r.Y, d, d, 180, 180);
        path.AddArc(r.X, r.Bottom - d, d, d, 0, 180);
        path.CloseFigure();
        return path;
    }
}
