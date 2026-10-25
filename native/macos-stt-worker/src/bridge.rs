//! Original, serial worker-only FFI ownership for the pinned Swift ASR bridge.
use std::cell::Cell;
use std::ffi::{c_char, c_void, CStr, CString};
use std::path::PathBuf;
use serde_json::{Map, Value};

extern "C" {
    fn ws_asr_create() -> *mut c_void;
    fn ws_asr_load(handle: *mut c_void, root: *const c_char) -> i32;
    fn ws_asr_ready(handle: *mut c_void) -> i32;
    fn ws_asr_transcribe(handle: *mut c_void, samples: *const f32, count: usize) -> *mut c_char;
    fn ws_asr_free(output: *mut c_char);
    fn ws_asr_destroy(handle: *mut c_void);
}

pub struct FluidAudio { handle: Cell<*mut c_void> }
pub struct AsrResult { pub text: String, pub body: Map<String, Value> }

impl FluidAudio {
    pub fn new() -> Result<Self, &'static str> {
        let handle = unsafe { ws_asr_create() };
        if handle.is_null() { return Err("bridge_create_failed"); }
        Ok(Self { handle: Cell::new(handle) })
    }
    pub fn is_apple_silicon(&self) -> bool { cfg!(target_arch = "aarch64") }
    pub fn is_asr_available(&self) -> bool {
        !self.handle.get().is_null() && unsafe { ws_asr_ready(self.handle.get()) == 1 }
    }
    pub fn init_asr(&self) -> Result<(), &'static str> {
        let home = std::env::var_os("CFFIXED_USER_HOME").filter(|h| !h.is_empty())
            .or_else(|| std::env::var_os("HOME").filter(|h| !h.is_empty()))
            .ok_or("model_home_absent")?;
        let root = PathBuf::from(home).join("Library/Application Support/FluidAudio/Models/parakeet-tdt-0.6b-v3");
        let root = CString::new(root.to_str().ok_or("model_path_invalid")?).map_err(|_| "model_path_invalid")?;
        if self.handle.get().is_null() { return Err("bridge_closed"); }
        if unsafe { ws_asr_load(self.handle.get(), root.as_ptr()) } != 1 {
            return Err("cached_model_load_failed");
        }
        Ok(())
    }
    pub fn transcribe_samples(&self, samples: &[f32]) -> Result<AsrResult, &'static str> {
        // TRANSCRIPT DATA - DO NOT LOG. Audio, result JSON and text remain
        // inside the owned worker pipe. Errors expose only fixed stage names.
        if !self.is_asr_available() { return Err("bridge_not_loaded"); }
        let output = unsafe { ws_asr_transcribe(self.handle.get(), samples.as_ptr(), samples.len()) };
        if output.is_null() { return Err("native_decode_failed"); }
        // Parse before freeing, and release on every parse/validation outcome.
        let parsed = unsafe { serde_json::from_slice::<Value>(CStr::from_ptr(output).to_bytes()) };
        unsafe { ws_asr_free(output) };
        let body = parsed.map_err(|_| "bridge_result_invalid")?.as_object().cloned().ok_or("bridge_result_invalid")?;
        let text = body.get("text").and_then(Value::as_str).ok_or("bridge_result_invalid")?.to_owned();
        Ok(AsrResult { text, body })
    }
    pub fn cleanup(&self) {
        let handle = self.handle.replace(std::ptr::null_mut());
        if !handle.is_null() { unsafe { ws_asr_destroy(handle) }; }
    }
}
impl Drop for FluidAudio { fn drop(&mut self) { self.cleanup(); } }
