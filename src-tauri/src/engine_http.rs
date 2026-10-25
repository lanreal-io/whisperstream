//! Content-free localhost health and shutdown transport for the packaged engine.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::sync::atomic::{AtomicU64, Ordering};
use std::time::Duration;

const ENGINE_ADDR: &str = "127.0.0.1:38420";
const ENGINE_GENERATION_HEADER: &str = "x-whisperstream-engine-generation";
const MAX_RESPONSE_HEADER_BYTES: usize = 4096;
static GENERATION_SEQUENCE: AtomicU64 = AtomicU64::new(0);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum HealthProbe {
    Ready,
    NotReady,
    InvalidResponse,
}

pub fn new_generation_token(generation: u64) -> String {
    let timestamp = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_nanos();
    let sequence = GENERATION_SEQUENCE.fetch_add(1, Ordering::Relaxed);
    format!(
        "{:x}-{:x}-{timestamp:x}-{sequence:x}",
        std::process::id(),
        generation,
    )
}

pub fn probe_health(timeout: Duration, expected_generation: Option<&str>) -> HealthProbe {
    let Some(addr) = engine_addr() else {
        return HealthProbe::InvalidResponse;
    };
    probe_health_at(addr, timeout, expected_generation)
}

fn probe_health_at(
    addr: SocketAddr,
    timeout: Duration,
    expected_generation: Option<&str>,
) -> HealthProbe {
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, timeout) else {
        return HealthProbe::NotReady;
    };
    let _ = stream.set_read_timeout(Some(timeout));
    let _ = stream.set_write_timeout(Some(timeout));
    if stream
        .write_all(b"GET /api/health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n")
        .is_err()
    {
        return HealthProbe::NotReady;
    }
    read_response_headers(&mut stream)
        .map(|response| classify_response(&response, expected_generation))
        .unwrap_or(HealthProbe::NotReady)
}

fn read_response_headers(stream: &mut TcpStream) -> Option<Vec<u8>> {
    let mut response = Vec::with_capacity(512);
    let mut chunk = [0_u8; 512];
    while response.len() < MAX_RESPONSE_HEADER_BYTES {
        let remaining = MAX_RESPONSE_HEADER_BYTES - response.len();
        let chunk_length = chunk.len();
        let count = stream
            .read(&mut chunk[..remaining.min(chunk_length)])
            .ok()?;
        if count == 0 {
            break;
        }
        response.extend_from_slice(&chunk[..count]);
        if response.windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    (!response.is_empty()).then_some(response)
}

fn classify_response(response: &[u8], expected_generation: Option<&str>) -> HealthProbe {
    let status = String::from_utf8_lossy(response);
    let Some(first_line) = status.lines().next() else {
        return HealthProbe::InvalidResponse;
    };
    if first_line.starts_with("HTTP/1.1 200 ") || first_line == "HTTP/1.1 200" {
        if expected_generation.is_none_or(|expected| {
            response_generation(&status).is_some_and(|actual| actual == expected)
        }) {
            HealthProbe::Ready
        } else {
            HealthProbe::InvalidResponse
        }
    } else if first_line.starts_with("HTTP/") {
        HealthProbe::NotReady
    } else {
        HealthProbe::InvalidResponse
    }
}

fn response_generation(response: &str) -> Option<&str> {
    response.lines().skip(1).find_map(|line| {
        let (name, value) = line.split_once(':')?;
        name.eq_ignore_ascii_case(ENGINE_GENERATION_HEADER)
            .then(|| value.trim())
    })
}

pub fn request_shutdown(timeout: Duration, expected_generation: Option<&str>) -> bool {
    let Some(addr) = engine_addr() else {
        return false;
    };
    request_shutdown_at(addr, timeout, expected_generation)
}

fn request_shutdown_at(
    addr: SocketAddr,
    timeout: Duration,
    expected_generation: Option<&str>,
) -> bool {
    let Ok(mut stream) = TcpStream::connect_timeout(&addr, timeout) else {
        return false;
    };
    let _ = stream.set_write_timeout(Some(timeout));
    let generation_header = expected_generation
        .map(|value| format!("{ENGINE_GENERATION_HEADER}: {value}\r\n"))
        .unwrap_or_default();
    let request = format!(
        "POST /api/shutdown HTTP/1.1\r\nHost: 127.0.0.1\r\n{generation_header}Content-Length: 0\r\nConnection: close\r\n\r\n"
    );
    stream.write_all(request.as_bytes()).is_ok()
}

fn engine_addr() -> Option<SocketAddr> {
    ENGINE_ADDR.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::net::TcpListener;
    use std::thread;

    #[test]
    fn connection_failure_is_not_ready_and_bounded() {
        let listener = TcpListener::bind("127.0.0.1:0").expect("bind should pass");
        let address = listener.local_addr().expect("address should exist");
        drop(listener);
        let started = std::time::Instant::now();
        let result = TcpStream::connect_timeout(&address, Duration::from_millis(50));
        assert!(result.is_err());
        assert!(started.elapsed() < Duration::from_secs(1));
    }

    #[test]
    fn status_parser_accepts_only_http_success() {
        assert_eq!(
            classify_response(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}", None,),
            HealthProbe::Ready
        );
        assert_eq!(
            classify_response(b"HTTP/1.1 503 Service Unavailable\r\n\r\n", None),
            HealthProbe::NotReady
        );
        assert_eq!(
            classify_response(b"not-http", None),
            HealthProbe::InvalidResponse
        );
    }

    #[test]
    fn health_requires_the_exact_spawned_generation() {
        let response = b"HTTP/1.1 200 OK\r\nX-Whisperstream-Engine-Generation: generation-current\r\nContent-Length: 2\r\n\r\n{}";
        assert_eq!(
            classify_response(response, Some("generation-current")),
            HealthProbe::Ready
        );
        assert_eq!(
            classify_response(response, Some("generation-stale")),
            HealthProbe::InvalidResponse
        );
        assert_eq!(
            classify_response(b"HTTP/1.1 200 OK\r\n\r\n{}", Some("generation-current")),
            HealthProbe::InvalidResponse
        );
    }

    #[test]
    fn generation_tokens_are_generation_scoped_and_unique() {
        let first = new_generation_token(7);
        let second = new_generation_token(7);
        let third = new_generation_token(8);
        assert_ne!(first, second);
        assert_ne!(second, third);
        assert!(first.contains("-7-"));
        assert!(third.contains("-8-"));
    }

    #[test]
    fn production_engine_address_remains_fixed() {
        assert_eq!(engine_addr(), Some("127.0.0.1:38420".parse().unwrap()));
    }

    #[test]
    fn stale_listener_rejects_generation_and_survives_shutdown() {
        let listener =
            TcpListener::bind("127.0.0.1:0").expect("isolated loopback bind should pass");
        let address = listener
            .local_addr()
            .expect("listener address should exist");
        let stale_server = thread::spawn(move || {
            let mut requests = Vec::new();
            for _ in 0..3 {
                let (mut stream, _) = listener.accept().expect("request should connect");
                stream
                    .set_read_timeout(Some(Duration::from_secs(2)))
                    .expect("read timeout should apply");
                let mut buffer = [0_u8; 1024];
                let count = stream
                    .read(&mut buffer)
                    .expect("request should be readable");
                let request = String::from_utf8_lossy(&buffer[..count]).into_owned();
                let is_shutdown = request.starts_with("POST /api/shutdown ");
                requests.push(request);
                let response = if is_shutdown {
                    "HTTP/1.1 409 Conflict\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
                } else {
                    "HTTP/1.1 200 OK\r\nX-Whisperstream-Engine-Generation: generation-stale\r\nContent-Length: 2\r\nConnection: close\r\n\r\n{}"
                };
                let _ = stream.write_all(response.as_bytes());
            }
            requests
        });

        assert_eq!(
            probe_health_at(address, Duration::from_secs(2), Some("generation-current"),),
            HealthProbe::InvalidResponse
        );
        assert!(request_shutdown_at(
            address,
            Duration::from_secs(2),
            Some("generation-current")
        ));
        assert_eq!(
            probe_health_at(address, Duration::from_secs(2), Some("generation-current"),),
            HealthProbe::InvalidResponse
        );

        let requests = stale_server
            .join()
            .expect("stale server should exit cleanly");
        assert_eq!(requests.len(), 3);
        assert_eq!(
            requests
                .iter()
                .filter(|request| request.starts_with("GET /api/health "))
                .count(),
            2
        );
        let shutdown = requests
            .iter()
            .find(|request| request.starts_with("POST /api/shutdown "))
            .expect("generation-bound shutdown should be attempted");
        assert!(shutdown.contains("x-whisperstream-engine-generation: generation-current\r\n"));
    }
}
