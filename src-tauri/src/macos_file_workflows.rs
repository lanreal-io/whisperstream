//! Native macOS panels and one-shot path-private file selections.
//!
//! The WebView receives only opaque tokens, display basenames for active
//! imports, and typed terminal states. Paths are kept in a mode-0600 registry
//! consumed by the Python Darwin owner; raw paths never enter frontend state or
//! a shared relocation request.

use serde::{Deserialize, Serialize};
use std::collections::BTreeMap;
use std::fs::{self, File, OpenOptions};
use std::io::{Read, Write};
use std::os::unix::fs::{OpenOptionsExt, PermissionsExt};
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use tauri_plugin_dialog::DialogExt;

const REGISTRY_FILE: &str = "macos-native-selections-v1.json";
const REGISTRY_LOCK_FILE: &str = "macos-native-selections-v1.json.lock";
const MIN_MODEL_STORAGE_BYTES: u64 = 483_256_769 + 64 * 1024 * 1024;
const MAX_NATIVE_SAVE_BYTES: usize = 64 * 1024 * 1024;
static TOKEN_COUNTER: AtomicU64 = AtomicU64::new(1);

#[derive(Clone, Debug, Deserialize, Serialize)]
pub(crate) struct NativeSelectionEntry {
    kind: String,
    path: String,
    display_name: String,
}

#[derive(Default, Deserialize, Serialize)]
struct NativeSelectionRegistry {
    schema_version: u8,
    selections: BTreeMap<String, NativeSelectionEntry>,
}

#[derive(Serialize)]
pub struct PublicNativeSelection {
    token: String,
    display_name: String,
}

#[derive(Serialize)]
pub struct NativePickerResult {
    status: &'static str,
    selections: Vec<PublicNativeSelection>,
    destination_token: Option<String>,
}

#[derive(Serialize)]
pub struct NativeSaveResult {
    status: &'static str,
}

fn cancelled_picker_result() -> NativePickerResult {
    NativePickerResult {
        status: "cancelled",
        selections: Vec::new(),
        destination_token: None,
    }
}

struct RegistryLock {
    file: File,
    path: PathBuf,
}

impl RegistryLock {
    fn acquire(data_root: &Path) -> Result<Self, ()> {
        fs::create_dir_all(data_root).map_err(|_| ())?;
        let path = data_root.join(REGISTRY_LOCK_FILE);
        let file = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .mode(0o600)
            .open(&path)
            .map_err(|_| ())?;
        // SAFETY: flock operates on this owned descriptor and is released in Drop.
        if unsafe { libc::flock(std::os::fd::AsRawFd::as_raw_fd(&file), libc::LOCK_EX) } != 0 {
            return Err(());
        }
        Ok(Self { file, path })
    }
}

impl Drop for RegistryLock {
    fn drop(&mut self) {
        // SAFETY: the descriptor remains valid until after this Drop body.
        let _ = unsafe { libc::flock(std::os::fd::AsRawFd::as_raw_fd(&self.file), libc::LOCK_UN) };
    }
}

fn data_root() -> Result<PathBuf, ()> {
    if let Some(value) = std::env::var_os("WHISPERSTREAM_DATA_DIR") {
        if !value.is_empty() {
            return Ok(PathBuf::from(value));
        }
    }
    let home = std::env::var_os("CFFIXED_USER_HOME")
        .filter(|value| !value.is_empty())
        .or_else(|| std::env::var_os("HOME").filter(|value| !value.is_empty()))
        .ok_or(())?;
    Ok(PathBuf::from(home)
        .join("Library")
        .join("Application Support")
        .join("Whisperstream"))
}

fn load_registry(path: &Path) -> Result<NativeSelectionRegistry, ()> {
    if !path.exists() {
        return Ok(NativeSelectionRegistry {
            schema_version: 1,
            selections: BTreeMap::new(),
        });
    }
    let mut raw = Vec::new();
    File::open(path)
        .and_then(|mut file| file.read_to_end(&mut raw))
        .map_err(|_| ())?;
    let registry: NativeSelectionRegistry = serde_json::from_slice(&raw).map_err(|_| ())?;
    if registry.schema_version != 1 {
        return Err(());
    }
    Ok(registry)
}

fn atomic_write_registry(path: &Path, registry: &NativeSelectionRegistry) -> Result<(), ()> {
    if registry.selections.is_empty() {
        fs::remove_file(path)
            .or_else(|error| {
                if error.kind() == std::io::ErrorKind::NotFound {
                    Ok(())
                } else {
                    Err(error)
                }
            })
            .map_err(|_| ())?;
        return Ok(());
    }
    let raw = serde_json::to_vec(registry).map_err(|_| ())?;
    let temporary = path.with_extension(format!(
        "tmp-{}-{}",
        std::process::id(),
        TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed),
    ));
    let write_result = (|| -> std::io::Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(&raw)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o600))?;
        Ok(())
    })();
    if write_result.is_err() {
        let _ = fs::remove_file(&temporary);
        return Err(());
    }
    Ok(())
}

fn random_token() -> Result<String, ()> {
    let mut bytes = [0u8; 24];
    File::open("/dev/urandom")
        .and_then(|mut file| file.read_exact(&mut bytes))
        .map_err(|_| ())?;
    let mut encoded = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        use std::fmt::Write as _;
        write!(&mut encoded, "{byte:02x}").map_err(|_| ())?;
    }
    Ok(format!("macos-native:{encoded}"))
}

fn register_entries(entries: Vec<NativeSelectionEntry>) -> Result<Vec<PublicNativeSelection>, ()> {
    let root = data_root()?;
    register_entries_at(&root, entries)
}

pub(crate) fn register_entries_at(
    root: &Path,
    entries: Vec<NativeSelectionEntry>,
) -> Result<Vec<PublicNativeSelection>, ()> {
    let lock = RegistryLock::acquire(&root)?;
    let path = root.join(REGISTRY_FILE);
    let mut registry = load_registry(&path)?;
    let mut public = Vec::with_capacity(entries.len());
    for entry in entries {
        let token = random_token()?;
        public.push(PublicNativeSelection {
            token: token.clone(),
            display_name: entry.display_name.clone(),
        });
        registry.selections.insert(token, entry);
    }
    atomic_write_registry(&path, &registry)?;
    drop(lock);
    Ok(public)
}

fn validate_import_file(path: PathBuf) -> Result<NativeSelectionEntry, ()> {
    let canonical = path.canonicalize().map_err(|_| ())?;
    if !canonical.is_absolute() || !canonical.is_file() {
        return Err(());
    }
    let display_name = canonical
        .file_name()
        .and_then(|name| name.to_str())
        .filter(|name| !name.is_empty())
        .ok_or(())?
        .to_string();
    let extension = canonical
        .extension()
        .and_then(|extension| extension.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    if !matches!(extension.as_str(), "wav" | "mp3" | "m4a" | "flac" | "ogg") {
        return Err(());
    }
    Ok(NativeSelectionEntry {
        kind: "import-audio".to_string(),
        path: canonical.to_string_lossy().into_owned(),
        display_name,
    })
}

pub(crate) fn validate_model_storage(path: PathBuf) -> Result<NativeSelectionEntry, ()> {
    let canonical = path.canonicalize().map_err(|_| ())?;
    if !canonical.is_absolute() || !canonical.is_dir() {
        return Err(());
    }
    let model_target = canonical
        .join("Library")
        .join("Application Support")
        .join("FluidAudio")
        .join("Models")
        .join("parakeet-tdt-0.6b-v3");
    if model_target.exists() {
        return Err(());
    }
    let probe = canonical.join(format!(
        ".whisperstream-storage-probe-{}-{}",
        std::process::id(),
        TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed),
    ));
    let probe_result = (|| -> std::io::Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&probe)?;
        file.write_all(b"storage-probe-v1")?;
        file.sync_all()?;
        fs::remove_file(&probe)?;
        Ok(())
    })();
    if probe_result.is_err() {
        let _ = fs::remove_file(&probe);
        return Err(());
    }
    let path_bytes =
        std::ffi::CString::new(canonical.as_os_str().as_encoded_bytes()).map_err(|_| ())?;
    let mut statistics = std::mem::MaybeUninit::<libc::statvfs>::uninit();
    // SAFETY: path_bytes is NUL-terminated and statistics points to writable memory.
    if unsafe { libc::statvfs(path_bytes.as_ptr(), statistics.as_mut_ptr()) } != 0 {
        return Err(());
    }
    // SAFETY: a zero return initializes the complete statvfs value.
    let statistics = unsafe { statistics.assume_init() };
    let available = (statistics.f_bavail as u64).saturating_mul(statistics.f_frsize as u64);
    if available < MIN_MODEL_STORAGE_BYTES {
        return Err(());
    }
    Ok(NativeSelectionEntry {
        kind: "select-model-storage".to_string(),
        path: canonical.to_string_lossy().into_owned(),
        display_name: "Selected model storage".to_string(),
    })
}

#[tauri::command]
pub async fn choose_macos_import_audio(app: tauri::AppHandle) -> NativePickerResult {
    let selected = app
        .dialog()
        .file()
        .add_filter("Audio", &["wav", "mp3", "m4a", "flac", "ogg"])
        .blocking_pick_files();
    let Some(paths) = selected else {
        return cancelled_picker_result();
    };
    if paths.is_empty() || paths.len() > 100 {
        return NativePickerResult {
            status: "failed",
            selections: Vec::new(),
            destination_token: None,
        };
    }
    let entries: Result<Vec<_>, ()> = paths
        .into_iter()
        .map(|path| {
            path.into_path()
                .map_err(|_| ())
                .and_then(validate_import_file)
        })
        .collect();
    match entries.and_then(register_entries) {
        Ok(selections) => NativePickerResult {
            status: "selected",
            selections,
            destination_token: None,
        },
        Err(()) => NativePickerResult {
            status: "failed",
            selections: Vec::new(),
            destination_token: None,
        },
    }
}

#[tauri::command]
pub async fn choose_macos_model_storage(app: tauri::AppHandle) -> NativePickerResult {
    let selected = app
        .dialog()
        .file()
        .set_title("Choose where to store models")
        .blocking_pick_folder();
    let Some(path) = selected else {
        return cancelled_picker_result();
    };
    let result = path
        .into_path()
        .map_err(|_| ())
        .and_then(validate_model_storage)
        .and_then(|entry| register_entries(vec![entry]));
    match result {
        Ok(mut selections) => NativePickerResult {
            status: "selected",
            destination_token: selections.pop().map(|selection| selection.token),
            selections: Vec::new(),
        },
        Err(()) => NativePickerResult {
            status: "failed",
            selections: Vec::new(),
            destination_token: None,
        },
    }
}

#[tauri::command]
pub fn release_macos_native_selections(tokens: Vec<String>) -> Result<(), String> {
    let root = data_root().map_err(|_| "native selection cleanup failed".to_string())?;
    release_native_selections_at(&root, tokens)
}

fn release_native_selections_at(root: &Path, tokens: Vec<String>) -> Result<(), String> {
    let lock =
        RegistryLock::acquire(root).map_err(|_| "native selection cleanup failed".to_string())?;
    let path = root.join(REGISTRY_FILE);
    let mut registry =
        load_registry(&path).map_err(|_| "native selection cleanup failed".to_string())?;
    for token in tokens {
        registry.selections.remove(&token);
    }
    atomic_write_registry(&path, &registry)
        .map_err(|_| "native selection cleanup failed".to_string())?;
    drop(lock);
    Ok(())
}

fn valid_save_request(kind: &str, default_name: &str) -> bool {
    let filename = Path::new(default_name);
    if filename.file_name().and_then(|name| name.to_str()) != Some(default_name) {
        return false;
    }
    let extension = filename
        .extension()
        .and_then(|extension| extension.to_str())
        .unwrap_or_default()
        .to_ascii_lowercase();
    matches!(
        (kind, extension.as_str()),
        ("export-transcript", "wav") | ("retain-eula", "txt") | ("export-logs", "zip")
    )
}

fn valid_save_payload_size(size: usize) -> bool {
    size <= MAX_NATIVE_SAVE_BYTES
}

#[tauri::command]
pub async fn save_macos_private_file(
    app: tauri::AppHandle,
    kind: String,
    default_name: String,
    payload: Vec<u8>,
) -> NativeSaveResult {
    if !valid_save_request(&kind, &default_name) || !valid_save_payload_size(payload.len()) {
        return NativeSaveResult { status: "failed" };
    }
    let extension = Path::new(&default_name)
        .extension()
        .and_then(|value| value.to_str())
        .unwrap_or_default();
    let selected = app
        .dialog()
        .file()
        .set_file_name(&default_name)
        .add_filter("Export", &[extension])
        .blocking_save_file();
    let Some(destination) = selected.and_then(|path| path.into_path().ok()) else {
        return NativeSaveResult {
            status: "cancelled",
        };
    };
    if !destination.is_absolute() {
        return NativeSaveResult { status: "failed" };
    }
    let parent = match destination.parent() {
        Some(parent) if parent.is_dir() => parent,
        _ => return NativeSaveResult { status: "failed" },
    };
    let temporary = parent.join(format!(
        ".whisperstream-export-{}-{}",
        std::process::id(),
        TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed),
    ));
    let result = (|| -> std::io::Result<()> {
        let mut file = OpenOptions::new()
            .create_new(true)
            .write(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(&payload)?;
        file.sync_all()?;
        fs::rename(&temporary, &destination)?;
        Ok(())
    })();
    if result.is_err() {
        let _ = fs::remove_file(&temporary);
        return NativeSaveResult { status: "failed" };
    }
    NativeSaveResult { status: "saved" }
}

pub fn initialize_native_selection_registry() -> Result<(), String> {
    let root = data_root().map_err(|_| "native selection startup cleanup failed".to_string())?;
    let _lock = RegistryLock::acquire(&root)
        .map_err(|_| "native selection startup cleanup failed".to_string())?;
    fs::remove_file(root.join(REGISTRY_FILE))
        .or_else(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                Ok(())
            } else {
                Err(error)
            }
        })
        .map_err(|_| "native selection startup cleanup failed".to_string())
}

pub fn cleanup_native_selection_registry() {
    if let Ok(root) = data_root() {
        if let Ok(lock) = RegistryLock::acquire(&root) {
            let _ = fs::remove_file(root.join(REGISTRY_FILE));
            let lock_path = lock.path.clone();
            drop(lock);
            let _ = fs::remove_file(lock_path);
        }
    }
}

#[cfg(feature = "macos-phase4-probe")]
pub(crate) fn register_model_storage_for_packaged_probe(
    root: &Path,
    destination: PathBuf,
) -> Result<String, String> {
    let entry = validate_model_storage(destination)
        .map_err(|_| "native model-storage validation failed".to_string())?;
    let mut selections = register_entries_at(root, vec![entry])
        .map_err(|_| "native model-storage registration failed".to_string())?;
    selections
        .pop()
        .map(|selection| selection.token)
        .ok_or_else(|| "native model-storage registration was empty".to_string())
}

#[cfg(feature = "macos-phase4-probe")]
pub(crate) fn remove_packaged_probe_registry(root: &Path) -> Result<(), String> {
    let path = root.join(REGISTRY_FILE);
    if path.exists() {
        return Err("native model-storage token survived packaged probe".to_string());
    }
    fs::remove_file(root.join(REGISTRY_LOCK_FILE))
        .or_else(|error| {
            if error.kind() == std::io::ErrorKind::NotFound {
                Ok(())
            } else {
                Err(error)
            }
        })
        .map_err(|_| "native model-storage lock cleanup failed".to_string())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fresh_root(name: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "whisperstream-macos-file-workflow-{name}-{}-{}",
            std::process::id(),
            TOKEN_COUNTER.fetch_add(1, Ordering::Relaxed),
        ))
    }

    #[test]
    fn macos_file_workflows_registry_is_opaque_and_exactly_released() {
        let root = fresh_root("registry");
        fs::create_dir_all(&root).unwrap();
        let source = root.join("reviewed.wav");
        fs::write(&source, b"reviewed-public-audio").unwrap();
        let public =
            register_entries_at(&root, vec![validate_import_file(source.clone()).unwrap()])
                .unwrap();
        assert_eq!(public.len(), 1);
        assert!(!format!("{}", serde_json::to_string(&public).unwrap())
            .contains(root.to_string_lossy().as_ref()));
        release_native_selections_at(&root, vec![public[0].token.clone()]).unwrap();
        assert!(!root.join(REGISTRY_FILE).exists());
        fs::remove_file(root.join(REGISTRY_LOCK_FILE)).unwrap();
        fs::remove_file(source).unwrap();
        fs::remove_dir(root).unwrap();
    }

    #[test]
    fn macos_file_workflows_save_kinds_are_bounded() {
        assert!(valid_save_request(
            "retain-eula",
            "Whisperstream-EULA-1.4.txt"
        ));
        assert!(valid_save_request("export-logs", "whisperstream-logs.zip"));
        assert!(valid_save_request("export-transcript", "recording.wav"));
        assert!(!valid_save_request("retain-eula", "../terms.txt"));
        assert!(!valid_save_request("export-logs", "logs.txt"));
        assert!(valid_save_payload_size(MAX_NATIVE_SAVE_BYTES));
        assert!(!valid_save_payload_size(MAX_NATIVE_SAVE_BYTES + 1));
        assert_eq!(cancelled_picker_result().status, "cancelled");
    }

    #[test]
    fn macos_model_picker_rejects_an_occupied_exact_target() {
        let root = fresh_root("occupied-model");
        let target = root
            .join("Library")
            .join("Application Support")
            .join("FluidAudio")
            .join("Models")
            .join("parakeet-tdt-0.6b-v3");
        fs::create_dir_all(&target).unwrap();
        let sentinel = target.join("owner-sentinel");
        fs::write(&sentinel, b"keep-owner-data").unwrap();

        assert!(validate_model_storage(root.clone()).is_err());
        assert_eq!(fs::read(&sentinel).unwrap(), b"keep-owner-data");

        fs::remove_file(sentinel).unwrap();
        fs::remove_dir_all(root).unwrap();
    }
}
