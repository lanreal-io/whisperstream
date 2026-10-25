use std::{env, fs, path::PathBuf};

// Optional updater provider. A build that ships an updater names its native
// module in this variable; without it the app builds with no updater. The
// module is compiled in as `crate::official_updater` through a generated
// include in OUT_DIR, so this tree never names where the provider lives.
const UPDATER_MODULE_VAR: &str = "WHISPERSTREAM_UPDATER_RS";

fn main() {
    println!("cargo::rustc-check-cfg=cfg(official_updater)");
    println!("cargo:rerun-if-env-changed={UPDATER_MODULE_VAR}");
    if let Some(module) = updater_module() {
        if env::var_os("CARGO_FEATURE_OFFICIAL_UPDATER").is_some() {
            let out = PathBuf::from(env::var_os("OUT_DIR").expect("OUT_DIR"));
            let include = format!(
                "#[path = {:?}]\nmod official_updater;\n",
                module.to_str().unwrap_or_else(|| panic!(
                    "{UPDATER_MODULE_VAR} must be a UTF-8 path: {}",
                    module.display()
                )),
            );
            fs::write(out.join("official_updater.rs"), include)
                .expect("write the official updater include");
            println!("cargo:rustc-cfg=official_updater");
        }
    }
    tauri_build::build()
}

/// The provider's native module, or None when the build supplies no updater.
/// A variable that names a missing file is a broken official build, not a
/// source build, so it fails here instead of silently dropping the updater.
fn updater_module() -> Option<PathBuf> {
    let value = env::var_os(UPDATER_MODULE_VAR).filter(|value| !value.is_empty())?;
    let mut module = PathBuf::from(value);
    if module.is_relative() {
        module = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"))
            .join(module);
    }
    let module = module.canonicalize().unwrap_or_else(|error| {
        panic!("{UPDATER_MODULE_VAR} names {} ({error})", module.display())
    });
    // The provider belongs to the tree being built. A path into another
    // checkout (for example from a staged copy of this one) would compile
    // that checkout's updater into this build.
    let workspace = PathBuf::from(env::var_os("CARGO_MANIFEST_DIR").expect("CARGO_MANIFEST_DIR"))
        .join("..")
        .canonicalize()
        .expect("canonical workspace root");
    if !module.starts_with(&workspace) {
        panic!(
            "{UPDATER_MODULE_VAR} names {}, outside the workspace being built ({})",
            module.display(),
            workspace.display()
        );
    }
    // Windows canonical paths are verbatim (\\?\C:\...); keep the plain form
    // for drive paths so diagnostics and file!() read normally.
    let module = match module.to_str().and_then(|path| path.strip_prefix(r"\\?\")) {
        Some(plain) if plain.as_bytes().get(1) == Some(&b':') => PathBuf::from(plain),
        _ => module,
    };
    // The module and its sibling resources (such as an include_str! policy).
    println!(
        "cargo:rerun-if-changed={}",
        module.parent().unwrap_or(&module).display()
    );
    Some(module)
}
