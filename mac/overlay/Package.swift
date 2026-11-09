// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "JarvisOverlay",
    platforms: [.macOS(.v14)],
    products: [
        .executable(name: "JarvisOverlay", targets: ["JarvisOverlay"]),
        .library(name: "OverlayCore", targets: ["OverlayCore"]),
    ],
    targets: [
        .target(name: "OverlayCore", path: "Sources/OverlayCore"),
        .executableTarget(
            name: "JarvisOverlay",
            dependencies: ["OverlayCore"],
            path: "Sources/JarvisOverlay"
        ),
        .testTarget(
            name: "OverlayCoreTests",
            dependencies: ["OverlayCore"],
            path: "Tests/OverlayCoreTests"
        ),
    ]
)
