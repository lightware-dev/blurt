// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "VoiceDictate",
    platforms: [.macOS(.v13)],
    targets: [
        .executableTarget(
            name: "VoiceDictate",
            path: "Sources/VoiceDictate",
            linkerSettings: [
                .linkedFramework("AppKit"),
                .linkedFramework("AVFoundation"),
                .linkedFramework("Carbon"),
            ]
        )
    ]
)
