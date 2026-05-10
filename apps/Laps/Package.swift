// swift-tools-version: 6.2

import PackageDescription

let package = Package(
    name: "Laps",
    platforms: [
        .macOS(.v26)
    ],
    products: [
        .executable(name: "Laps", targets: ["Laps"])
    ],
    dependencies: [
        .package(path: "/Users/USERNAME/tools/apple-app-tools")
    ],
    targets: [
        .target(name: "LapsCore"),
        .executableTarget(
            name: "Laps",
            dependencies: [
                "LapsCore",
                .product(name: "AppleAppDesignSystem", package: "apple-app-tools"),
                .product(name: "LocalAppUpdateKit", package: "apple-app-tools")
            ],
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
