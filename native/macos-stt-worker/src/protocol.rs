use std::collections::BTreeMap;
use std::fmt;
use std::io::{self, Read, Write};

use serde_json::{Map, Value};

pub const PROTOCOL_NAME: &str = "whisperstream-stt";
pub const PROTOCOL_VERSION: i64 = 1;
pub const FRAME_MAGIC: [u8; 4] = *b"WST1";
pub const MAX_HEADER_BYTES: usize = 256 * 1024;
pub const MAX_PAYLOAD_BYTES: usize = 128 * 1024 * 1024;
pub const MAX_AUDIO_SAMPLES: usize = MAX_PAYLOAD_BYTES / 4;
pub const MAX_REQUEST_ID_BYTES: usize = 128;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ProtocolErrorKind {
    Protocol,
    Mismatch,
    Validation,
    Capability,
}

#[derive(Debug)]
pub struct ProtocolError {
    pub kind: ProtocolErrorKind,
    message: &'static str,
}

impl ProtocolError {
    pub fn protocol(message: &'static str) -> Self {
        Self {
            kind: ProtocolErrorKind::Protocol,
            message,
        }
    }

    pub fn mismatch(message: &'static str) -> Self {
        Self {
            kind: ProtocolErrorKind::Mismatch,
            message,
        }
    }

    pub fn validation(message: &'static str) -> Self {
        Self {
            kind: ProtocolErrorKind::Validation,
            message,
        }
    }

    pub fn capability(message: &'static str) -> Self {
        Self {
            kind: ProtocolErrorKind::Capability,
            message,
        }
    }

    pub fn code(&self) -> &'static str {
        match self.kind {
            ProtocolErrorKind::Protocol => "protocol_error",
            ProtocolErrorKind::Mismatch => "protocol_mismatch",
            ProtocolErrorKind::Validation => "validation_error",
            ProtocolErrorKind::Capability => "capability_unsupported",
        }
    }

    pub fn category(&self) -> &'static str {
        match self.kind {
            ProtocolErrorKind::Protocol | ProtocolErrorKind::Mismatch => "protocol",
            ProtocolErrorKind::Validation => "validation",
            ProtocolErrorKind::Capability => "capability",
        }
    }
}

impl fmt::Display for ProtocolError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        formatter.write_str(self.message)
    }
}

impl std::error::Error for ProtocolError {}

#[derive(Debug)]
pub enum ReadFrameError {
    Eof,
    Fatal(ProtocolError),
    Io(io::Error),
}

impl From<io::Error> for ReadFrameError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

#[derive(Debug)]
pub struct Frame {
    pub header: Map<String, Value>,
    pub payload: Vec<u8>,
}

#[derive(Debug, Clone)]
pub struct Request {
    pub request_id: String,
    pub operation: String,
    pub backend: String,
    pub body: Map<String, Value>,
}

pub struct ErrorReply<'a> {
    pub request_id: Option<&'a str>,
    pub operation: Option<&'a str>,
    pub backend: Option<&'a str>,
    pub code: &'a str,
    pub category: &'a str,
    pub message: &'a str,
    pub exception_type: Option<&'a str>,
    pub error_chain: Option<&'a str>,
}

fn read_exact_frame<R: Read>(reader: &mut R, length: usize) -> Result<Vec<u8>, ReadFrameError> {
    let mut bytes = vec![0u8; length];
    let mut offset = 0usize;
    while offset < length {
        match reader.read(&mut bytes[offset..]) {
            Ok(0) => {
                return Err(ReadFrameError::Fatal(ProtocolError::protocol(
                    "unexpected EOF inside worker frame",
                )))
            }
            Ok(count) => offset += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(ReadFrameError::Io(error)),
        }
    }
    Ok(bytes)
}

pub fn read_frame<R: Read>(reader: &mut R) -> Result<Frame, ReadFrameError> {
    let mut prefix = [0u8; 12];
    let mut offset = 0usize;
    while offset < prefix.len() {
        match reader.read(&mut prefix[offset..]) {
            Ok(0) if offset == 0 => return Err(ReadFrameError::Eof),
            Ok(0) => {
                return Err(ReadFrameError::Fatal(ProtocolError::protocol(
                    "unexpected EOF inside worker frame",
                )))
            }
            Ok(count) => offset += count,
            Err(error) if error.kind() == io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(ReadFrameError::Io(error)),
        }
    }
    if prefix[0..4] != FRAME_MAGIC {
        return Err(ReadFrameError::Fatal(ProtocolError::protocol(
            "invalid worker frame magic",
        )));
    }
    let header_length = u32::from_be_bytes(prefix[4..8].try_into().unwrap()) as usize;
    let payload_length = u32::from_be_bytes(prefix[8..12].try_into().unwrap()) as usize;
    if header_length == 0 || header_length > MAX_HEADER_BYTES {
        return Err(ReadFrameError::Fatal(ProtocolError::protocol(
            "invalid worker frame header length",
        )));
    }
    if payload_length > MAX_PAYLOAD_BYTES {
        return Err(ReadFrameError::Fatal(ProtocolError::protocol(
            "invalid worker frame payload length",
        )));
    }
    let header_bytes = read_exact_frame(reader, header_length)?;
    let header = serde_json::from_slice::<Value>(&header_bytes)
        .map_err(|_| {
            ReadFrameError::Fatal(ProtocolError::protocol(
                "worker frame header is not valid UTF-8 JSON",
            ))
        })?
        .as_object()
        .cloned()
        .ok_or_else(|| {
            ReadFrameError::Fatal(ProtocolError::protocol(
                "worker frame header must be a JSON object",
            ))
        })?;
    let payload = read_exact_frame(reader, payload_length)?;
    Ok(Frame { header, payload })
}

pub fn write_frame<W: Write>(writer: &mut W, header: &Map<String, Value>) -> Result<(), io::Error> {
    let header_bytes = serde_json::to_vec(header)
        .map_err(|error| io::Error::new(io::ErrorKind::InvalidData, error))?;
    if header_bytes.is_empty() || header_bytes.len() > MAX_HEADER_BYTES {
        return Err(io::Error::new(
            io::ErrorKind::InvalidData,
            "worker reply header exceeded protocol bounds",
        ));
    }
    writer.write_all(&FRAME_MAGIC)?;
    writer.write_all(&(header_bytes.len() as u32).to_be_bytes())?;
    writer.write_all(&0u32.to_be_bytes())?;
    writer.write_all(&header_bytes)?;
    writer.flush()
}

fn valid_identifier(value: &str, backend: bool) -> bool {
    let mut chars = value.chars();
    let Some(first) = chars.next() else {
        return false;
    };
    if !first.is_ascii_alphanumeric() || (backend && first.is_ascii_uppercase()) {
        return false;
    }
    chars.all(|character| {
        character.is_ascii_alphanumeric() && (!backend || !character.is_ascii_uppercase())
            || matches!(character, '.' | '_' | '-')
            || (!backend && character == ':')
    })
}

pub fn request_metadata(
    header: &Map<String, Value>,
) -> (Option<String>, Option<String>, Option<String>) {
    fn safe(value: Option<&Value>) -> Option<String> {
        let text = value?.as_str()?;
        (text.len() <= MAX_REQUEST_ID_BYTES).then(|| text.to_owned())
    }
    (
        safe(header.get("request_id")),
        safe(header.get("operation")),
        safe(header.get("backend")),
    )
}

pub fn validate_request(header: &Map<String, Value>) -> Result<Request, ProtocolError> {
    if header.get("protocol").and_then(Value::as_str) != Some(PROTOCOL_NAME) {
        return Err(ProtocolError::mismatch("unsupported STT worker protocol"));
    }
    if header.get("version").and_then(Value::as_i64) != Some(PROTOCOL_VERSION) {
        return Err(ProtocolError::mismatch(
            "unsupported STT worker protocol version; supported=1",
        ));
    }
    if header.get("kind").and_then(Value::as_str) != Some("request") {
        return Err(ProtocolError::validation(
            "worker envelope kind must be request",
        ));
    }
    let request_id = header
        .get("request_id")
        .and_then(Value::as_str)
        .ok_or_else(|| ProtocolError::validation("invalid request_id"))?;
    if request_id.is_empty()
        || request_id.len() > MAX_REQUEST_ID_BYTES
        || !valid_identifier(request_id, false)
    {
        return Err(ProtocolError::validation("invalid request_id"));
    }
    let operation = header
        .get("operation")
        .and_then(Value::as_str)
        .ok_or_else(|| ProtocolError::validation("invalid operation"))?;
    if operation.is_empty() || operation.len() > 64 || !valid_identifier(operation, false) {
        return Err(ProtocolError::validation("invalid operation"));
    }
    let backend = header
        .get("backend")
        .and_then(Value::as_str)
        .ok_or_else(|| ProtocolError::validation("invalid backend"))?;
    if backend.is_empty() || backend.len() > 64 || !valid_identifier(backend, true) {
        return Err(ProtocolError::validation("invalid backend"));
    }
    let body = match header.get("body") {
        None => Map::new(),
        Some(Value::Object(body)) => body.clone(),
        Some(_) => return Err(ProtocolError::validation("request body must be an object")),
    };
    Ok(Request {
        request_id: request_id.to_owned(),
        operation: operation.to_owned(),
        backend: backend.to_owned(),
        body,
    })
}

fn base_reply(
    request_id: Option<&str>,
    operation: Option<&str>,
    backend: Option<&str>,
) -> Map<String, Value> {
    let mut reply = Map::new();
    reply.insert("protocol".into(), Value::String(PROTOCOL_NAME.into()));
    reply.insert("version".into(), Value::Number(PROTOCOL_VERSION.into()));
    reply.insert("kind".into(), Value::String("reply".into()));
    reply.insert(
        "request_id".into(),
        request_id.map_or(Value::Null, |value| Value::String(value.into())),
    );
    reply.insert(
        "operation".into(),
        operation.map_or(Value::Null, |value| Value::String(value.into())),
    );
    reply.insert(
        "backend".into(),
        backend.map_or(Value::Null, |value| Value::String(value.into())),
    );
    reply
}

pub fn success_reply(request: &Request, body: Map<String, Value>) -> Map<String, Value> {
    let mut reply = base_reply(
        Some(&request.request_id),
        Some(&request.operation),
        Some(&request.backend),
    );
    reply.insert("ok".into(), Value::Bool(true));
    reply.insert("body".into(), Value::Object(body));
    reply
}

fn bounded_single_line(value: &str, max_bytes: usize) -> String {
    let flattened = value.replace(['\r', '\n'], " ");
    if flattened.len() <= max_bytes {
        return flattened;
    }
    let mut end = max_bytes;
    while !flattened.is_char_boundary(end) {
        end -= 1;
    }
    flattened[..end].to_owned()
}

pub fn error_reply(details: ErrorReply<'_>) -> Map<String, Value> {
    let mut reply = base_reply(details.request_id, details.operation, details.backend);
    reply.insert("ok".into(), Value::Bool(false));
    let mut error = Map::new();
    error.insert("code".into(), Value::String(details.code.into()));
    error.insert("category".into(), Value::String(details.category.into()));
    error.insert(
        "message".into(),
        Value::String(bounded_single_line(details.message, 2_048)),
    );
    if let Some(exception_type) = details.exception_type {
        error.insert(
            "exception_type".into(),
            Value::String(bounded_single_line(exception_type, 256)),
        );
    }
    if let Some(error_chain) = details.error_chain {
        let bytes = error_chain.as_bytes();
        let start = bytes.len().saturating_sub(192 * 1024);
        let mut safe_start = start;
        while safe_start < bytes.len() && !error_chain.is_char_boundary(safe_start) {
            safe_start += 1;
        }
        error.insert(
            "traceback".into(),
            Value::String(error_chain[safe_start..].to_owned()),
        );
    }
    reply.insert("error".into(), Value::Object(error));
    reply
}

pub fn decode_audio(request: &Request, payload: &[u8]) -> Result<(Vec<f32>, u32), ProtocolError> {
    let audio = request
        .body
        .get("audio")
        .and_then(Value::as_object)
        .ok_or_else(|| ProtocolError::validation("audio metadata must be an object"))?;
    if audio.get("dtype").and_then(Value::as_str) != Some("float32-le") {
        return Err(ProtocolError::validation("audio dtype must be float32-le"));
    }
    let shape = audio
        .get("shape")
        .and_then(Value::as_array)
        .ok_or_else(|| {
            ProtocolError::validation("audio shape must be one non-negative dimension")
        })?;
    if shape.len() != 1 {
        return Err(ProtocolError::validation(
            "audio shape must be one non-negative dimension",
        ));
    }
    let shape_count = shape[0].as_u64().ok_or_else(|| {
        ProtocolError::validation("audio shape must be one non-negative dimension")
    })?;
    let sample_count = audio
        .get("sample_count")
        .and_then(Value::as_u64)
        .ok_or_else(|| ProtocolError::validation("audio sample_count does not match shape"))?;
    if sample_count != shape_count || sample_count > MAX_AUDIO_SAMPLES as u64 {
        return Err(ProtocolError::validation(
            "audio sample_count does not match shape",
        ));
    }
    let sample_rate = audio
        .get("sample_rate")
        .and_then(Value::as_u64)
        .filter(|value| *value > 0 && *value <= u32::MAX as u64)
        .ok_or_else(|| ProtocolError::validation("audio sample_rate must be a positive integer"))?;
    let expected_length = (sample_count as usize)
        .checked_mul(4)
        .ok_or_else(|| ProtocolError::validation("audio byte length does not match metadata"))?;
    let byte_length = audio
        .get("byte_length")
        .and_then(Value::as_u64)
        .ok_or_else(|| ProtocolError::validation("audio byte length does not match metadata"))?;
    if byte_length != expected_length as u64 || payload.len() != expected_length {
        return Err(ProtocolError::validation(
            "audio byte length does not match metadata",
        ));
    }
    let mut samples = Vec::with_capacity(sample_count as usize);
    for bytes in payload.chunks_exact(4) {
        samples.push(f32::from_le_bytes(bytes.try_into().unwrap()));
    }
    Ok((samples, sample_rate as u32))
}

pub fn object(entries: impl IntoIterator<Item = (&'static str, Value)>) -> Map<String, Value> {
    entries
        .into_iter()
        .map(|(key, value)| (key.to_owned(), value))
        .collect::<BTreeMap<_, _>>()
        .into_iter()
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn identifier_validation_matches_v1_ascii_contract() {
        assert!(valid_identifier("p123-4:ok.value", false));
        assert!(valid_identifier("parakeet-v3", true));
        assert!(!valid_identifier("contains spaces", false));
        assert!(!valid_identifier("Windows", true));
        assert!(!valid_identifier("windows:handle", true));
    }

    #[test]
    fn frame_round_trip_preserves_raw_payload() {
        let mut header = Map::new();
        header.insert("value".into(), Value::String("metadata".into()));
        let encoded_header = serde_json::to_vec(&header).unwrap();
        let payload = [1u8, 2, 3, 4];
        let mut wire = Vec::new();
        wire.extend_from_slice(&FRAME_MAGIC);
        wire.extend_from_slice(&(encoded_header.len() as u32).to_be_bytes());
        wire.extend_from_slice(&(payload.len() as u32).to_be_bytes());
        wire.extend_from_slice(&encoded_header);
        wire.extend_from_slice(&payload);
        let frame = read_frame(&mut wire.as_slice()).unwrap();
        assert_eq!(frame.header, header);
        assert_eq!(frame.payload, payload);
    }
}
