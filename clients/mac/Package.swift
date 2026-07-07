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
            ]
        )
    ]
)
