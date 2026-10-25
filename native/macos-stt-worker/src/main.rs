use std::env;
use std::fs;
use std::path::PathBuf;
use std::process::ExitCode;
use std::time::Instant;

use whisperstream_macos_stt_worker::bridge::FluidAudio;
use whisperstream_macos_stt_worker::{
    decode_pcm16_mono_16khz, normalize_expected_text, run_consecutive,
};

struct Args {
    fixture: PathBuf,
    expected: PathBuf,
    runs: usize,
}

fn parse_args() -> Result<Args, &'static str> {
    let mut args = env::args_os().skip(1);
    let mut fixture = None;
    let mut expected = None;
    let mut runs = None;
    while let Some(argument) = args.next() {
        match argument.to_str() {
            Some("--fixture") => fixture = args.next().map(PathBuf::from),
            Some("--expected") => expected = args.next().map(PathBuf::from),
            Some("--runs") => {
                runs = args
                    .next()
                    .and_then(|value| value.to_str().and_then(|raw| raw.parse().ok()))
            }
            _ => return Err("arguments"),
        }
    }
    let runs = runs.ok_or("runs")?;
    if runs == 0 || runs > 8 {
        return Err("runs");
    }
    Ok(Args {
        fixture: fixture.ok_or("fixture")?,
        expected: expected.ok_or("expected")?,
        runs,
    })
}

fn run() -> Result<(), &'static str> {
    let args = parse_args()?;
    let fixture_bytes = fs::read(&args.fixture).map_err(|_| "fixture_read")?;
    let samples = decode_pcm16_mono_16khz(&fixture_bytes).map_err(|_| "audio_format")?;
    let expected = fs::read_to_string(&args.expected).map_err(|_| "expected_read")?;
    let normalized_expected = normalize_expected_text(&expected);

    let audio = FluidAudio::new().map_err(|_| "bridge_create")?;
    if !audio.is_apple_silicon() {
        return Err("architecture");
    }

    let init_started = Instant::now();
    audio.init_asr().map_err(|_| "asr_initialize")?;
    let init_ms = init_started.elapsed().as_secs_f64() * 1_000.0;

    let mut durations_ms = Vec::with_capacity(args.runs);
    let results = run_consecutive(args.runs, || {
        let started = Instant::now();
        let result = audio
            .transcribe_samples(&samples)
            .map_err(|_| "transcribe")?;
        durations_ms.push(started.elapsed().as_secs_f64() * 1_000.0);
        Ok::<String, &'static str>(normalize_expected_text(&result.text))
    })?;
    let matches_expected = results.iter().all(|result| result == &normalized_expected);
    let repeatable = results.windows(2).all(|pair| pair[0] == pair[1]);
    audio.cleanup();

    if !matches_expected {
        return Err("fixture_mismatch");
    }
    if !repeatable {
        return Err("repeatability");
    }

    let durations = durations_ms
        .iter()
        .map(|duration| format!("{duration:.3}"))
        .collect::<Vec<_>>()
        .join(",");
    println!(
        "{{\"schema\":1,\"apple_silicon\":true,\"sample_rate\":16000,\"sample_count\":{},\"runs\":{},\"init_ms\":{init_ms:.3},\"transcription_ms\":[{durations}],\"matched\":true,\"repeatable\":true}}",
        samples.len(),
        args.runs,
    );
    Ok(())
}

fn main() -> ExitCode {
    if env::args_os().len() == 1 {
        let code = whisperstream_macos_stt_worker::worker::run_protocol_worker();
        return ExitCode::from(code as u8);
    }
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(stage) => {
            eprintln!("canary_failed stage={stage}");
            ExitCode::FAILURE
        }
    }
}
