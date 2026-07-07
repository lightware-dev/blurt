using System.Drawing;
using System.Drawing.Drawing2D;
using System.Runtime.InteropServices;

namespace Blurt;

/// Draws the tray icon at runtime so the app ships as a single self-contained exe
/// with no .ico asset to embed. The Blurt mark — an upright "highlighter bar"
/// inside a thin rounded-square border (from www/public/favicon.svg) — identical
/// to the Mac client's menu-bar glyph (Brand.menuBarIcon). Bone-white when idle,
/// coral when recording.
internal static class IconFactory
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool DestroyIcon(IntPtr handle);

    public static Icon Tray(bool recording)
    {
        // Draw large and let the tray downscale — keeps edges smooth on hi-DPI.
        const float s = 32f;
        using var bmp = new Bitmap((int)s, (int)s);
        using (var g = Graphics.FromImage(bmp))
        {
            g.SmoothingMode = SmoothingMode.AntiAlias;
            g.Clear(Color.Transparent);

            var color = recording ? Brand.GdiCoral : Brand.GdiBone;

            // Proportions mirror the Mac glyph (a 19pt canvas: 17pt square frame,
            // 1.2pt stroke, 4.4pt corners, a 3.5×9.5pt bar), scaled to 32px.
            float line = s * (1.2f / 19f);
            float side = s * (17f / 19f);
            float radius = s * (4.4f / 19f);

            // Thin rounded-square border, inset by half the stroke so it lands
            // crisply inside the frame.
            var frame = new RectangleF((s - side) / 2f, (s - side) / 2f, side, side);
            frame.Inflate(-line / 2f, -line / 2f);
            using (var pen = new Pen(color, line) { LineJoin = LineJoin.Round })
                g.DrawPath(pen, RoundedRect(frame, radius));

            // The upright bar, centered inside the frame.
            float bw = s * (3.5f / 19f), bh = s * (9.5f / 19f);
            var bar = new RectangleF((s - bw) / 2f, (s - bh) / 2f, bw, bh);
            using (var brush = new SolidBrush(color))
                g.FillPath(brush, RoundedRect(bar, s * (1.2f / 19f)));
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

    private static GraphicsPath RoundedRect(RectangleF r, float radius)
    {
        float d = radius * 2f;
        var path = new GraphicsPath();
        path.AddArc(r.X, r.Y, d, d, 180, 90);
        path.AddArc(r.Right - d, r.Y, d, d, 270, 90);
        path.AddArc(r.Right - d, r.Bottom - d, d, d, 0, 90);
        path.AddArc(r.X, r.Bottom - d, d, d, 90, 90);
        path.CloseFigure();
        return path;
    }
}
