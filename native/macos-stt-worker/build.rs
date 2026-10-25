use std::{env, path::PathBuf, process::Command};

fn main() {
    println!("cargo:rerun-if-env-changed=MACOSX_DEPLOYMENT_TARGET");
    println!("cargo:rerun-if-changed=swift");
    println!("cargo:rerun-if-changed=Package.swift");
    println!("cargo:rerun-if-changed=Package.resolved");
    let root = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let output = PathBuf::from(env::var_os("OUT_DIR").unwrap()).join("owned-swift");
    let status = Command::new("swift").current_dir(&root)
        .args(["build", "--configuration", "release", "--product", "WhisperstreamAsrBridge",
               "--build-path"]).arg(&output)
        .args(["--disable-automatic-resolution", "--jobs", "2"])
        .status().expect("Swift compiler could not be started");
    assert!(status.success(), "owned Swift ASR bridge compilation failed");
    println!("cargo:rustc-link-search=native={}", output.join("release").display());
    println!("cargo:rustc-link-lib=static=WhisperstreamAsrBridge");
    for framework in ["Foundation", "AVFoundation", "CoreML", "Accelerate", "Metal", "MetalPerformanceShaders"] {
        println!("cargo:rustc-link-lib=framework={framework}");
    }
    println!("cargo:rustc-link-lib=dylib=swiftCore");
    println!("cargo:rustc-link-lib=c++");
}
