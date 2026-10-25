use std::env;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Write};
use std::path::PathBuf;
use std::process;
use std::thread;
use std::time::{Duration, Instant};

use crate::bridge::FluidAudio;
use serde_json::{Map, Value};

use crate::protocol::{
    decode_audio, error_reply, object, read_frame, request_metadata, success_reply,
    validate_request, write_frame, ErrorReply, ProtocolError, ReadFrameError, Request,
    PROTOCOL_VERSION,
};
use crate::REQUIRED_SAMPLE_RATE;

const ENGINE_ID: &str = "parakeet";
const MODEL_NAME: &str = "parakeet-tdt-0.6b-v3";
const DIAGNOSTIC_DIR_ENV: &str = "WHISPERSTREAM_STT_WORKER_DIAGNOSTIC_DIR";
const TEST_DELAY_OPERATION_ENV: &str = "WHISPERSTREAM_STT_WORKER_TEST_DELAY_OPERATION";
const TEST_DELAY_MS_ENV: &str = "WHISPERSTREAM_STT_WORKER_TEST_DELAY_MS";
const TEST_EXCEPTION_OPERATION_ENV: &str = "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_OPERATION";
const TEST_EXCEPTION_MESSAGE_ENV: &str = "WHISPERSTREAM_STT_WORKER_TEST_EXCEPTION_MESSAGE";
const TEST_HARD_EXIT_OPERATION_ENV: &str = "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_OPERATION";
const TEST_HARD_EXIT_AFTER_REPLY_OPERATION_ENV: &str =
    "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_AFTER_REPLY_OPERATION";
const TEST_HARD_EXIT_ONCE_SENTINEL_ENV: &str =
    "WHISPERSTREAM_STT_WORKER_TEST_HARD_EXIT_ONCE_SENTINEL";
const TEST_MALFORMED_REPLY_OPERATION_ENV: &str =
    "WHISPERSTREAM_STT_WORKER_TEST_MALFORMED_REPLY_OPERATION";
const TEST_DIAGNOSTIC_OPERATION_ENV: &str = "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_OPERATION";
const TEST_DIAGNOSTIC_BYTES_ENV: &str = "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_BYTES";
const TEST_DIAGNOSTIC_VALUE_ENV: &str = "WHISPERSTREAM_STT_WORKER_TEST_DIAGNOSTIC_VALUE";
const MAX_DIAGNOSTIC_BYTES: u64 = 512 * 1024;

const REQUIRED_MODEL_ASSETS: &[&str] = &[
    "Preprocessor.mlmodelc/model.mil",
    "Encoder.mlmodelc/model.mil",
    "Decoder.mlmodelc/model.mil",
    "JointDecisionv3.mlmodelc/model.mil",
    "parakeet_v3_vocab.json",
];

#[derive(Debug)]
struct NativeError {
    exception_type: &'static str,
    message: &'static str,
    chain: String,
}

impl NativeError {
    fn new(
        exception_type: &'static str,
        message: &'static str,
        detail: impl std::fmt::Display,
    ) -> Self {
        Self {
            exception_type,
            message,
            chain: format!("{exception_type}: {message}\ncaused by: {detail}"),
        }
    }

    fn test_control(operation: &str) -> Self {
        let detail = env::var(TEST_EXCEPTION_MESSAGE_ENV).unwrap_or_else(|_| {
            format!("synthetic content-free macOS STT worker exception operation={operation}")
        });
        let chain = sanitize_diagnostic(&format!(
            "MacSttWorkerTestError: synthetic content-free macOS STT worker exception\n{detail}"
        ));
        Self {
            exception_type: "MacSttWorkerTestError",
            message: "synthetic content-free macOS STT worker exception",
            chain,
        }
    }
}

fn looks_like_path(value: &str) -> bool {
    let trimmed = value.trim_matches(|character: char| {
        matches!(character, '"' | '\'' | '(' | ')' | '[' | ']' | ',' | ';')
    });
    trimmed.starts_with('/')
        || trimmed.starts_with("~/")
        || (trimmed.len() >= 3
            && trimmed.as_bytes()[0].is_ascii_alphabetic()
            && trimmed.as_bytes()[1] == b':'
            && matches!(trimmed.as_bytes()[2], b'\\' | b'/'))
}

fn sanitize_diagnostic(value: &str) -> String {
    let prohibited = env::var(TEST_DIAGNOSTIC_VALUE_ENV).unwrap_or_default();
    value
        .replace('\r', "")
        .lines()
        .map(|line| {
            line.split_whitespace()
                .map(|token| {
                    if !prohibited.is_empty() && token.contains(&prohibited) {
                        return "[content redacted]".to_owned();
                    }
                    let trimmed = token.trim_matches(|character: char| {
                        matches!(character, '"' | '\'' | '(' | ')' | '[' | ']' | ',' | ';')
                    });
                    if trimmed.contains('@')
                        && trimmed.split_once('@').is_some_and(|(local, domain)| {
                            !local.is_empty() && domain.contains('.')
                        })
                    {
                        return "[email redacted]".to_owned();
                    }
                    if looks_like_path(trimmed)
                        || trimmed
                            .split_once('=')
                            .is_some_and(|(_, candidate)| looks_like_path(candidate))
                    {
                        return "[path redacted]".to_owned();
                    }
                    if let Some((key, _)) = trimmed.split_once('=') {
                        let lowered = key.to_ascii_lowercase();
                        if ["secret", "token", "password", "license", "api_key"]
                            .iter()
                            .any(|needle| lowered.contains(needle))
                        {
                            return format!("{key}=[secret redacted]");
                        }
                    }
                    token.to_owned()
                })
                .collect::<Vec<_>>()
                .join(" ")
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[derive(Debug)]
enum DispatchError {
    Protocol(ProtocolError),
    Native(NativeError),
}

impl From<ProtocolError> for DispatchError {
    fn from(error: ProtocolError) -> Self {
        Self::Protocol(error)
    }
}

struct Diagnostics {
    diagnostic_path: PathBuf,
    marker_path: PathBuf,
    started: Instant,
}

impl Diagnostics {
    fn create() -> io::Result<Self> {
        let root = env::var_os(DIAGNOSTIC_DIR_ENV)
            .filter(|value| !value.is_empty())
            .map(PathBuf::from)
            .unwrap_or_else(|| env::temp_dir().join("Whisperstream-stt-worker"));
        fs::create_dir_all(&root)?;
        let pid = process::id();
        let diagnostic_path = root.join(format!("worker-{pid}.diagnostic.log"));
        File::create(&diagnostic_path)?;
        Ok(Self {
            diagnostic_path,
            marker_path: root.join(format!("worker-{pid}.last-operation.json")),
            started: Instant::now(),
        })
    }

    fn marker(
        &self,
        backend: &str,
        operation: &str,
        request_id: Option<&str>,
        phase: &str,
    ) -> io::Result<()> {
        let mut marker = Map::new();
        marker.insert(
            "protocol_version".into(),
            Value::Number(PROTOCOL_VERSION.into()),
        );
        marker.insert("pid".into(), Value::Number(process::id().into()));
        marker.insert("backend".into(), Value::String(backend.into()));
        marker.insert("operation".into(), Value::String(operation.into()));
        marker.insert("phase".into(), Value::String(phase.into()));
        marker.insert(
            "monotonic_ns".into(),
            Value::Number((self.started.elapsed().as_nanos() as u64).into()),
        );
        if let Some(request_id) = request_id {
            marker.insert("request_id".into(), Value::String(request_id.into()));
        }
        let bytes = serde_json::to_vec(&marker)
            .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
        let mut file = File::create(&self.marker_path)?;
        file.write_all(&bytes)?;
        file.sync_all()
    }

    fn error_chain(&self, chain: &str) {
        let flattened = sanitize_diagnostic(chain);
        let Ok(mut file) = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&self.diagnostic_path)
        else {
            return;
        };
        let Ok(length) = file.metadata().map(|metadata| metadata.len()) else {
            return;
        };
        let remaining = MAX_DIAGNOSTIC_BYTES.saturating_sub(length) as usize;
        if remaining <= 1 {
            return;
        }
        let mut end = flattened.len().min(remaining - 1);
        while !flattened.is_char_boundary(end) {
            end -= 1;
        }
        let _ = file.write_all(&flattened.as_bytes()[..end]);
        let _ = file.write_all(b"\n");
        let _ = file.flush();
    }
}

struct Runtime {
    audio: Option<FluidAudio>,
}

impl Runtime {
    fn new() -> Self {
        Self { audio: None }
    }

    fn loaded(&self) -> bool {
        self.audio
            .as_ref()
            .is_some_and(FluidAudio::is_asr_available)
    }

    fn unload(&mut self) {
        if let Some(audio) = self.audio.take() {
            audio.cleanup();
        }
    }

    fn model_root() -> Result<PathBuf, NativeError> {
        let home = env::var_os("CFFIXED_USER_HOME")
            .filter(|value| !value.is_empty())
            .or_else(|| env::var_os("HOME").filter(|value| !value.is_empty()))
            .ok_or_else(|| {
                NativeError::new(
                    "MacSttModelUnavailable",
                    "isolated FluidAudio model home is unavailable",
                    "home environment is absent",
                )
            })?;
        Ok(PathBuf::from(home)
            .join("Library/Application Support/FluidAudio/Models")
            .join(MODEL_NAME))
    }

    fn validate_model_assets() -> Result<(), NativeError> {
        let root = Self::model_root()?;
        if REQUIRED_MODEL_ASSETS
            .iter()
            .any(|relative| !root.join(relative).is_file())
        {
            return Err(NativeError::new(
                "MacSttModelUnavailable",
                "prewarmed FluidAudio model assets are unavailable",
                "required cache asset is absent",
            ));
        }
        Ok(())
    }

    fn load(&mut self, body: &Map<String, Value>) -> Result<Map<String, Value>, DispatchError> {
        if body.len() != 1 || body.get("model_name").and_then(Value::as_str) != Some(MODEL_NAME) {
            return Err(ProtocolError::validation(
                "macOS Parakeet load requires the supported model_name",
            )
            .into());
        }
        if self.loaded() {
            return Ok(lifecycle_body("ready", true));
        }
        Self::validate_model_assets().map_err(DispatchError::Native)?;
        self.unload();
        let audio = FluidAudio::new().map_err(|error| {
            DispatchError::Native(NativeError::new(
                "FluidAudioBridgeError",
                "FluidAudio bridge creation failed",
                error,
            ))
        })?;
        if !audio.is_apple_silicon() {
            return Err(DispatchError::Native(NativeError::new(
                "MacSttArchitectureUnsupported",
                "FluidAudio requires native Apple Silicon for this milestone",
                "architecture check failed",
            )));
        }
        audio.init_asr().map_err(|error| {
            DispatchError::Native(NativeError::new(
                "FluidAudioInitializationError",
                "FluidAudio ASR initialization failed",
                error,
            ))
        })?;
        self.audio = Some(audio);
        Ok(lifecycle_body("ready", true))
    }

    fn transcribe(
        &self,
        request: &Request,
        payload: &[u8],
    ) -> Result<Map<String, Value>, DispatchError> {
        if request.body.keys().any(|key| key != "audio") {
            if request.body.contains_key("language") {
                return Err(ProtocolError::capability(
                    "macOS Parakeet language hints are unsupported",
                )
                .into());
            }
            return Err(ProtocolError::validation(
                "unsupported macOS Parakeet transcription field",
            )
            .into());
        }
        let (samples, sample_rate) = decode_audio(request, payload)?;
        if sample_rate != REQUIRED_SAMPLE_RATE {
            return Err(ProtocolError::validation("FluidAudio requires 16000 Hz audio").into());
        }
        let audio = self.audio.as_ref().ok_or_else(|| {
            DispatchError::Native(NativeError::new(
                "MacSttNotLoaded",
                "macOS Parakeet runtime is not loaded",
                "load must complete before inference",
            ))
        })?;
        let result = audio.transcribe_samples(&samples).map_err(|error| {
            DispatchError::Native(NativeError::new(
                "FluidAudioTranscriptionError",
                "FluidAudio transcription failed",
                error,
            ))
        })?;
        Ok(result.body)
    }

    fn dispatch(
        &mut self,
        request: &Request,
        payload: &[u8],
    ) -> Result<(Map<String, Value>, bool), DispatchError> {
        if request.backend != ENGINE_ID {
            return Err(ProtocolError::validation("worker backend is unavailable").into());
        }
        let no_payload = || {
            if payload.is_empty() {
                Ok(())
            } else {
                Err(ProtocolError::validation(
                    "control operation must not include a binary payload",
                ))
            }
        };
        match request.operation.as_str() {
            "hello" => {
                no_payload()?;
                let versions = request
                    .body
                    .get("supported_versions")
                    .and_then(Value::as_array)
                    .ok_or_else(|| {
                        ProtocolError::mismatch("no mutually supported protocol version")
                    })?;
                if !versions
                    .iter()
                    .any(|version| version.as_i64() == Some(PROTOCOL_VERSION))
                {
                    return Err(
                        ProtocolError::mismatch("no mutually supported protocol version").into(),
                    );
                }
                Ok((
                    object([
                        ("selected_version", Value::Number(PROTOCOL_VERSION.into())),
                        ("worker_pid", Value::Number(process::id().into())),
                        ("engine_id", Value::String(ENGINE_ID.into())),
                        ("capabilities", Value::Object(Map::new())),
                        (
                            "limits",
                            Value::Object(object([
                                ("audio_dtype", Value::String("float32-le".into())),
                                ("audio_rank", Value::Number(1.into())),
                            ])),
                        ),
                    ]),
                    false,
                ))
            }
            "load" => {
                no_payload()?;
                Ok((self.load(&request.body)?, false))
            }
            "unload" => {
                no_payload()?;
                if !request.body.is_empty() {
                    return Err(ProtocolError::validation("unload body must be empty").into());
                }
                self.unload();
                Ok((lifecycle_body("idle", false), false))
            }
            "health" => {
                no_payload()?;
                if !request.body.is_empty() {
                    return Err(ProtocolError::validation("health body must be empty").into());
                }
                let loaded = self.loaded();
                Ok((
                    object([
                        ("engine_id", Value::String(ENGINE_ID.into())),
                        (
                            "state",
                            Value::String(if loaded { "ready" } else { "idle" }.into()),
                        ),
                        ("loaded", Value::Bool(loaded)),
                    ]),
                    false,
                ))
            }
            "transcribe" | "transcribe_chunk" => Ok((self.transcribe(request, payload)?, false)),
            "preview" => {
                Err(ProtocolError::capability("macOS Parakeet preview is unsupported").into())
            }
            "update_hotwords" => {
                Err(ProtocolError::capability("macOS Parakeet hotwords are unsupported").into())
            }
            "set_language_hint" => Err(ProtocolError::capability(
                "macOS Parakeet language hints are unsupported",
            )
            .into()),
            "shutdown" => {
                no_payload()?;
                if !request.body.is_empty() {
                    return Err(ProtocolError::validation("shutdown body must be empty").into());
                }
                self.unload();
                Ok((lifecycle_body("shutdown", false), true))
            }
            _ => Err(ProtocolError::validation("unsupported worker operation").into()),
        }
    }
}

impl Drop for Runtime {
    fn drop(&mut self) {
        self.unload();
    }
}

fn lifecycle_body(state: &str, loaded: bool) -> Map<String, Value> {
    object([
        ("state", Value::String(state.into())),
        ("loaded", Value::Bool(loaded)),
    ])
}

fn should_fire_once() -> bool {
    match env::var_os(TEST_HARD_EXIT_ONCE_SENTINEL_ENV) {
        Some(value) if !value.is_empty() => {
            let sentinel = PathBuf::from(value);
            if let Some(parent) = sentinel.parent() {
                let _ = fs::create_dir_all(parent);
            }
            OpenOptions::new()
                .write(true)
                .create_new(true)
                .open(sentinel)
                .is_ok()
        }
        _ => true,
    }
}

fn private_test_controls(operation: &str, diagnostics: &Diagnostics) -> Result<(), NativeError> {
    if env::var(TEST_DELAY_OPERATION_ENV).as_deref() == Ok(operation) {
        let delay_ms = env::var(TEST_DELAY_MS_ENV)
            .ok()
            .and_then(|value| value.parse::<u64>().ok())
            .unwrap_or(0)
            .min(10_000);
        thread::sleep(Duration::from_millis(delay_ms));
    }
    if env::var(TEST_EXCEPTION_OPERATION_ENV).as_deref() == Ok(operation) {
        return Err(NativeError::test_control(operation));
    }
    if env::var(TEST_DIAGNOSTIC_OPERATION_ENV).as_deref() == Ok(operation) {
        let byte_count = env::var(TEST_DIAGNOSTIC_BYTES_ENV)
            .ok()
            .and_then(|value| value.parse::<usize>().ok())
            .unwrap_or(0)
            .min(2 * MAX_DIAGNOSTIC_BYTES as usize);
        if byte_count > 0 {
            let probe = format!("native-diagnostic-probe {}", "x".repeat(byte_count));
            diagnostics.error_chain(&probe);
            eprintln!("{probe}");
        }
    }
    if env::var(TEST_HARD_EXIT_OPERATION_ENV).as_deref() == Ok(operation) {
        if should_fire_once() {
            process::exit(86);
        }
    }
    Ok(())
}

fn send_protocol_error<W: Write>(
    output: &mut W,
    metadata: (Option<String>, Option<String>, Option<String>),
    error: &ProtocolError,
) -> io::Result<()> {
    let message = error.to_string();
    let reply = error_reply(ErrorReply {
        request_id: metadata.0.as_deref(),
        operation: metadata.1.as_deref(),
        backend: metadata.2.as_deref(),
        code: error.code(),
        category: error.category(),
        message: &message,
        exception_type: None,
        error_chain: None,
    });
    write_frame(output, &reply)
}

pub fn run_protocol_worker() -> i32 {
    let diagnostics = match Diagnostics::create() {
        Ok(diagnostics) => diagnostics,
        Err(_) => return 2,
    };
    if diagnostics
        .marker("unselected", "startup", None, "ready")
        .is_err()
    {
        return 2;
    }
    let stdin = io::stdin();
    let stdout = io::stdout();
    let mut input = stdin.lock();
    let mut output = stdout.lock();
    let mut runtime = Runtime::new();
    let mut negotiated = false;

    loop {
        let frame = match read_frame(&mut input) {
            Ok(frame) => frame,
            Err(ReadFrameError::Eof) => {
                runtime.unload();
                let _ = diagnostics.marker(ENGINE_ID, "eof", None, "complete");
                return 0;
            }
            Err(ReadFrameError::Fatal(error)) => {
                let _ =
                    send_protocol_error(&mut output, (None, None, Some(ENGINE_ID.into())), &error);
                let _ = diagnostics.marker(ENGINE_ID, "malformed_frame", None, "fatal");
                return 2;
            }
            Err(ReadFrameError::Io(error)) => {
                diagnostics.error_chain(&format!("MacSttProtocolIoError: {error}"));
                return 2;
            }
        };

        let metadata = request_metadata(&frame.header);
        let request = match validate_request(&frame.header) {
            Ok(request) => request,
            Err(error) => {
                if send_protocol_error(&mut output, metadata, &error).is_err() {
                    return 2;
                }
                continue;
            }
        };
        if !negotiated && request.operation != "hello" {
            let error = ProtocolError::mismatch("hello negotiation is required");
            if send_protocol_error(
                &mut output,
                (
                    Some(request.request_id),
                    Some(request.operation),
                    Some(request.backend),
                ),
                &error,
            )
            .is_err()
            {
                return 2;
            }
            continue;
        }
        if diagnostics
            .marker(
                &request.backend,
                &request.operation,
                Some(&request.request_id),
                "active",
            )
            .is_err()
        {
            return 2;
        }

        let dispatch = private_test_controls(&request.operation, &diagnostics)
            .map_err(DispatchError::Native)
            .and_then(|()| runtime.dispatch(&request, &frame.payload));
        match dispatch {
            Ok((body, should_exit)) => {
                if request.operation == "hello" {
                    negotiated = true;
                }
                if diagnostics
                    .marker(
                        &request.backend,
                        &request.operation,
                        Some(&request.request_id),
                        "complete",
                    )
                    .is_err()
                {
                    return 2;
                }
                if env::var(TEST_MALFORMED_REPLY_OPERATION_ENV).as_deref()
                    == Ok(request.operation.as_str())
                {
                    if output.write_all(b"BAD!\0\0\0\0\0\0\0\0").is_err() || output.flush().is_err()
                    {
                        return 2;
                    }
                } else if write_frame(&mut output, &success_reply(&request, body)).is_err() {
                    return 2;
                }
                if env::var(TEST_HARD_EXIT_AFTER_REPLY_OPERATION_ENV).as_deref()
                    == Ok(request.operation.as_str())
                    && should_fire_once()
                {
                    process::exit(87);
                }
                if should_exit {
                    return 0;
                }
            }
            Err(DispatchError::Protocol(error)) => {
                if send_protocol_error(
                    &mut output,
                    (
                        Some(request.request_id),
                        Some(request.operation),
                        Some(request.backend),
                    ),
                    &error,
                )
                .is_err()
                {
                    return 2;
                }
            }
            Err(DispatchError::Native(error)) => {
                diagnostics.error_chain(&error.chain);
                let reply = error_reply(ErrorReply {
                    request_id: Some(&request.request_id),
                    operation: Some(&request.operation),
                    backend: Some(&request.backend),
                    code: "python_exception",
                    category: "python",
                    message: error.message,
                    exception_type: Some(error.exception_type),
                    error_chain: Some(&error.chain),
                });
                if write_frame(&mut output, &reply).is_err() {
                    return 2;
                }
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn mac_capability_map_is_explicitly_empty() {
        let runtime = Runtime::new();
        assert!(!runtime.loaded());
        let hello = Request {
            request_id: "test-1".into(),
            operation: "hello".into(),
            backend: ENGINE_ID.into(),
            body: object([(
                "supported_versions",
                Value::Array(vec![Value::Number(PROTOCOL_VERSION.into())]),
            )]),
        };
        let (body, _) = runtime_dispatch_without_model(runtime, &hello);
        assert_eq!(body.get("capabilities"), Some(&Value::Object(Map::new())));
    }

    fn runtime_dispatch_without_model(
        mut runtime: Runtime,
        request: &Request,
    ) -> (Map<String, Value>, bool) {
        runtime.dispatch(request, &[]).expect("hello dispatch")
    }
}
