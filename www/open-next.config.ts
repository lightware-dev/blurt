import { defineCloudflareConfig } from '@opennextjs/cloudflare'

export default defineCloudflareConfig({
    // Uncomment to enable R2 cache — see https://opennext.js.org/cloudflare/caching
    // incrementalCache: r2IncrementalCache,
})
