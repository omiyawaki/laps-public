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
        .package(url: "https://github.com/omiyawaki/apple-app-tools-public.git", branch: "main")
    ],
    targets: [
        .target(name: "LapsCore"),
        .executableTarget(
            name: "Laps",
            dependencies: [
                "LapsCore",
                .product(name: "AppleAppDesignSystem", package: "apple-app-tools-public"),
                .product(name: "LocalAppUpdateKit", package: "apple-app-tools-public")
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
