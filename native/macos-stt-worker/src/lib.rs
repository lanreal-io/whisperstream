use std::fmt;

pub mod protocol;
pub mod bridge;
pub mod worker;

pub const REQUIRED_SAMPLE_RATE: u32 = 16_000;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum AudioFormatError {
    Truncated,
    NotWave,
    MissingFormat,
    MissingAudio,
    UnsupportedEncoding,
    UnsupportedChannels,
    UnsupportedSampleRate,
    UnsupportedBitDepth,
    InvalidBlockAlignment,
    InvalidAudioLength,
}

impl fmt::Display for AudioFormatError {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        write!(formatter, "invalid fixture audio: {self:?}")
    }
}

impl std::error::Error for AudioFormatError {}

fn read_u16_le(bytes: &[u8], offset: usize) -> Result<u16, AudioFormatError> {
    let raw = bytes
        .get(offset..offset + 2)
        .ok_or(AudioFormatError::Truncated)?;
    Ok(u16::from_le_bytes([raw[0], raw[1]]))
}

fn read_u32_le(bytes: &[u8], offset: usize) -> Result<u32, AudioFormatError> {
    let raw = bytes
        .get(offset..offset + 4)
        .ok_or(AudioFormatError::Truncated)?;
    Ok(u32::from_le_bytes([raw[0], raw[1], raw[2], raw[3]]))
}

/// Parse the worker's authoritative input shape: mono 16 kHz signed PCM16 WAV.
///
/// The canary deliberately feeds normalized samples into `transcribe_samples`
/// because that is the production worker boundary selected by protocol v1.
pub fn decode_pcm16_mono_16khz(bytes: &[u8]) -> Result<Vec<f32>, AudioFormatError> {
    if bytes.len() < 12 {
        return Err(AudioFormatError::Truncated);
    }
    if &bytes[0..4] != b"RIFF" || &bytes[8..12] != b"WAVE" {
        return Err(AudioFormatError::NotWave);
    }

    let mut offset = 12usize;
    let mut format_seen = false;
    let mut audio_range = None;

    while offset + 8 <= bytes.len() {
        let chunk_id = &bytes[offset..offset + 4];
        let chunk_len = read_u32_le(bytes, offset + 4)? as usize;
        let chunk_start = offset + 8;
        let chunk_end = chunk_start
            .checked_add(chunk_len)
            .ok_or(AudioFormatError::Truncated)?;
        if chunk_end > bytes.len() {
            return Err(AudioFormatError::Truncated);
        }

        if chunk_id == b"fmt " {
            if chunk_len < 16 {
                return Err(AudioFormatError::Truncated);
            }
            if read_u16_le(bytes, chunk_start)? != 1 {
                return Err(AudioFormatError::UnsupportedEncoding);
            }
            if read_u16_le(bytes, chunk_start + 2)? != 1 {
                return Err(AudioFormatError::UnsupportedChannels);
            }
            if read_u32_le(bytes, chunk_start + 4)? != REQUIRED_SAMPLE_RATE {
                return Err(AudioFormatError::UnsupportedSampleRate);
            }
            if read_u16_le(bytes, chunk_start + 14)? != 16 {
                return Err(AudioFormatError::UnsupportedBitDepth);
            }
            if read_u16_le(bytes, chunk_start + 12)? != 2 {
                return Err(AudioFormatError::InvalidBlockAlignment);
            }
            format_seen = true;
        } else if chunk_id == b"data" {
            audio_range = Some(chunk_start..chunk_end);
        }

        offset = chunk_end + (chunk_len & 1);
    }

    if !format_seen {
        return Err(AudioFormatError::MissingFormat);
    }
    let audio = audio_range.ok_or(AudioFormatError::MissingAudio)?;
    if audio.len() % 2 != 0 {
        return Err(AudioFormatError::InvalidAudioLength);
    }

    Ok(bytes[audio]
        .chunks_exact(2)
        .map(|sample| i16::from_le_bytes([sample[0], sample[1]]) as f32 / 32_768.0)
        .collect())
}

/// Match the existing API fixture policy: trim and compare case-insensitively.
pub fn normalize_expected_text(text: &str) -> String {
    text.trim().to_lowercase()
}

/// Exercise repeated calls through one long-lived decoder instance.
pub fn run_consecutive<T, E, F>(runs: usize, mut transcribe: F) -> Result<Vec<T>, E>
where
    F: FnMut() -> Result<T, E>,
{
    (0..runs).map(|_| transcribe()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn wav(sample_rate: u32, channels: u16, bits: u16, samples: &[i16]) -> Vec<u8> {
        let data_len = samples.len() * 2;
        let mut bytes = Vec::new();
        bytes.extend_from_slice(b"RIFF");
        bytes.extend_from_slice(&(36u32 + data_len as u32).to_le_bytes());
        bytes.extend_from_slice(b"WAVEfmt ");
        bytes.extend_from_slice(&16u32.to_le_bytes());
        bytes.extend_from_slice(&1u16.to_le_bytes());
        bytes.extend_from_slice(&channels.to_le_bytes());
        bytes.extend_from_slice(&sample_rate.to_le_bytes());
        let block_align = channels * (bits / 8);
        bytes.extend_from_slice(&(sample_rate * block_align as u32).to_le_bytes());
        bytes.extend_from_slice(&block_align.to_le_bytes());
        bytes.extend_from_slice(&bits.to_le_bytes());
        bytes.extend_from_slice(b"data");
        bytes.extend_from_slice(&(data_len as u32).to_le_bytes());
        for sample in samples {
            bytes.extend_from_slice(&sample.to_le_bytes());
        }
        bytes
    }

    #[test]
    fn validates_and_normalizes_pcm16_audio() {
        let samples = decode_pcm16_mono_16khz(&wav(16_000, 1, 16, &[-32_768, 0, 32_767]))
            .expect("valid fixture shape");
        assert_eq!(samples.len(), 3);
        assert_eq!(samples[0], -1.0);
        assert_eq!(samples[1], 0.0);
        assert!(samples[2] < 1.0);
    }

    #[test]
    fn rejects_non_authoritative_audio_shapes() {
        assert_eq!(
            decode_pcm16_mono_16khz(&wav(48_000, 1, 16, &[0])),
            Err(AudioFormatError::UnsupportedSampleRate)
        );
        assert_eq!(
            decode_pcm16_mono_16khz(&wav(16_000, 2, 16, &[0, 0])),
            Err(AudioFormatError::UnsupportedChannels)
        );
        assert_eq!(
            decode_pcm16_mono_16khz(&wav(16_000, 1, 8, &[0])),
            Err(AudioFormatError::UnsupportedBitDepth)
        );
    }

    #[test]
    fn normalization_matches_existing_e2e_policy() {
        assert_eq!(normalize_expected_text("  MiXeD Case\n"), "mixed case");
    }

    #[test]
    fn consecutive_helper_reuses_one_callable() {
        let mut calls = 0usize;
        let values = run_consecutive(2, || {
            calls += 1;
            Ok::<usize, ()>(calls)
        })
        .expect("two calls");
        assert_eq!(values, vec![1, 2]);
        assert_eq!(calls, 2);
    }
}
