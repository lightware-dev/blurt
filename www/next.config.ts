import type { NextConfig } from 'next'
import path from 'path'

// Security response headers. See www/public/_headers for why these are what
// they are (and why `unsafe-inline` is unavoidable while Next inlines the
// hydration payload).
//
// This has to be declared in *both* places, which is annoying but not
// redundant: under OpenNext the document is rendered by the Worker, while
// /_next/static/* and everything in public/ is served straight off the
// Cloudflare assets binding without the Worker ever running. next.config's
// headers() covers the first, public/_headers covers the second, and neither
// covers the other — verified with `opennextjs-cloudflare preview`. Keep the
// two lists in step.
const SECURITY_HEADERS = [
    {
        key: 'Content-Security-Policy',
        value: [
            "default-src 'self'",
            "script-src 'self' 'unsafe-inline'",
            "style-src 'self' 'unsafe-inline'",
            "img-src 'self' data:",
            "font-src 'self'",
            "connect-src 'self'",
            "frame-ancestors 'none'",
            "base-uri 'self'",
            "form-action 'self'",
            "object-src 'none'",
        ].join('; '),
    },
    { key: 'Strict-Transport-Security', value: 'max-age=31536000; includeSubDomains' },
    { key: 'X-Content-Type-Options', value: 'nosniff' },
    { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
    { key: 'X-Frame-Options', value: 'DENY' },
]

const nextConfig: NextConfig = {
    // Pin the workspace root — a stray lockfile in $HOME otherwise confuses Turbopack.
    turbopack: { root: path.resolve(__dirname) },
    async headers() {
        return [{ source: '/:path*', headers: SECURITY_HEADERS }]
    },
}

export default nextConfig

// added by create cloudflare to enable calling `getCloudflareContext()` in `next dev`
import { initOpenNextCloudflareForDev } from '@opennextjs/cloudflare'
initOpenNextCloudflareForDev()
