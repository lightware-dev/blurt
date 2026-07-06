import type { Metadata } from 'next'
import './global.css'
import { Inter, Space_Grotesk, JetBrains_Mono } from 'next/font/google'

const inter = Inter({
    subsets: ['latin'],
    variable: '--font-inter',
    display: 'swap',
})

const space = Space_Grotesk({
    subsets: ['latin'],
    variable: '--font-space',
    display: 'swap',
})

const jetbrains = JetBrains_Mono({
    subsets: ['latin'],
    variable: '--font-jetbrains',
    display: 'swap',
})

const title = 'Blurt — yet another voice dictation app'
const description =
    'The world did not need another voice-to-text app. We made one anyway. Blurt runs on your own GPU, never phones home, and types what you meant — not what you mumbled. Say it badly, get it typed well.'

export const metadata: Metadata = {
    title,
    description,
    metadataBase: new URL('https://blurtvoice.com'),
    icons: { icon: '/favicon.svg' },
    openGraph: {
        type: 'website',
        url: 'https://blurtvoice.com',
        siteName: 'Blurt',
        title,
        description,
    },
    twitter: {
        card: 'summary_large_image',
        title,
        description,
    },
}

export default function RootLayout({ children }: { children: React.ReactNode }) {
    return (
        <html
            lang="en"
            className={`${inter.variable} ${space.variable} ${jetbrains.variable}`}
        >
            <body>{children}</body>
        </html>
    )
}
