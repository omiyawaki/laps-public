// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "Laps",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .executable(name: "Laps", targets: ["Laps"])
    ],
    targets: [
        .target(name: "LapsCore"),
        .executableTarget(
            name: "Laps",
            dependencies: ["LapsCore"],
            path: "Sources/Laps",
            resources: [
                .process("Resources")
            ]
        ),
        .testTarget(
            name: "LapsCoreTests",
            dependencies: ["LapsCore"]
        )
    ]
)
