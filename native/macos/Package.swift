// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "ClaudeAO3Mac",
    platforms: [.macOS(.v13)],
    products: [.executable(name: "ClaudeAO3Mac", targets: ["ClaudeAO3"])],
    dependencies: [
        .package(url: "https://github.com/migueldeicaza/SwiftTerm.git", exact: "1.15.0")
    ],
    targets: [
        .executableTarget(
            name: "ClaudeAO3",
            dependencies: [.product(name: "SwiftTerm", package: "SwiftTerm")]
        )
    ]
)
