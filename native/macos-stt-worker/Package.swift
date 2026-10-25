// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "WhisperstreamAsrBridge",
    platforms: [.macOS(.v14)],
    products: [.library(name: "WhisperstreamAsrBridge", type: .static,
                        targets: ["WhisperstreamAsrBridge"])],
    dependencies: [.package(url: "https://github.com/FluidInference/FluidAudio.git",
        revision: "56607d90b97de7d95a731200563bf49aa5beef20")],
    targets: [.target(name: "WhisperstreamAsrBridge",
        dependencies: [.product(name: "FluidAudio", package: "FluidAudio")],
        path: "swift")],
    swiftLanguageModes: [.v5]
)
