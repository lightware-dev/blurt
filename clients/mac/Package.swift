// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "Blurt",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "Blurt",
            path: "Sources/Blurt",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("AVFoundation"),
                // On-device transcription (LocalEngine.swift). The framework
                // itself dates to macOS 10.15, so linking it is safe at the
                // package's 13.0 deployment target — only the SpeechAnalyzer
                // symbols are 26+, and those are behind #available.
                .linkedFramework("Speech"),
                .linkedFramework("Carbon"),
                // Certificate pinning (CertTrust.swift): SecTrust evaluation and
                // the SHA-256 over the leaf's DER bytes.
                .linkedFramework("Security"),
                .linkedFramework("CryptoKit"),
            ]
        )
    ]
)
