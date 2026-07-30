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
                .linkedFramework("Carbon"),
                // Certificate pinning (CertTrust.swift): SecTrust evaluation and
                // the SHA-256 over the leaf's DER bytes.
                .linkedFramework("Security"),
                .linkedFramework("CryptoKit"),
            ]
        )
    ]
)
