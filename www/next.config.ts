import type { NextConfig } from 'next'
import path from 'path'

const nextConfig: NextConfig = {
    // Pin the workspace root — a stray lockfile in $HOME otherwise confuses Turbopack.
    turbopack: { root: path.resolve(__dirname) },
}

export default nextConfig

// added by create cloudflare to enable calling `getCloudflareContext()` in `next dev`
import { initOpenNextCloudflareForDev } from '@opennextjs/cloudflare'
initOpenNextCloudflareForDev()
