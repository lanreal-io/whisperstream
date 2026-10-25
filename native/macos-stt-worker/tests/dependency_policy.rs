use std::{fs, path::PathBuf};

const REVISION: &str = "56607d90b97de7d95a731200563bf49aa5beef20";
fn root() -> PathBuf { PathBuf::from(env!("CARGO_MANIFEST_DIR")) }

#[test]
fn dependency_uses_exact_direct_swift_revision() {
    let manifest = fs::read_to_string(root().join("Cargo.toml")).unwrap();
    let lock = fs::read_to_string(root().join("Cargo.lock")).unwrap();
    assert!(!manifest.contains("fluidaudio-rs"));
    assert!(!lock.contains("fluidaudio-rs"));
    let swift = fs::read_to_string(root().join("Package.swift")).unwrap();
    assert!(swift.contains("https://github.com/FluidInference/FluidAudio.git"));
    assert!(swift.contains(REVISION));
    assert!(!swift.contains("branch:"));
    let resolved = fs::read_to_string(root().join("Package.resolved")).unwrap();
    assert!(resolved.contains(REVISION));
}

#[test]
fn license_audit_retains_separate_shipping_review() {
    let policy = fs::read_to_string(root().join("license-audit.toml")).unwrap();
    assert!(policy.contains("NOT_REVIEWED_FOR_SHIPPING"));
    assert!(policy.contains("tracked_license_text = true"));
    assert!(policy.contains(REVISION));
    let lock = fs::read_to_string(root().join("swift-dependency.lock.json")).unwrap();
    assert!(lock.contains(REVISION));
}

#[test]
fn owned_bridge_loads_cached_models_and_owns_json_lifetime() {
    let swift = fs::read_to_string(root().join("swift/WhisperstreamAsrBridge.swift")).unwrap();
    assert!(swift.contains("MLModel(contentsOf:"));
    assert!(!swift.contains("downloadAndLoad("));
    assert!(!swift.contains("loadWithAutoRecovery("));
    assert!(swift.contains("var state = try TdtDecoderState()"));
    assert!(swift.contains("result.tokenTimings"));
    assert!(swift.contains("ws_asr_free"));
    let rust = fs::read_to_string(root().join("src/bridge.rs")).unwrap();
    assert!(rust.contains("ws_asr_free(output)"));
}
