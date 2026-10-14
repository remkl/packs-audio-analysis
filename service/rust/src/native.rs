use serde::Serialize;
use std::ffi::{CStr, CString, c_char, c_void};
#[repr(C)]
struct Metrics {
    samples: *const f32,
    count: usize,
    owner: *mut c_void,
    bpm: f32,
    bpm_confidence: f32,
    key_confidence: f32,
    key: [c_char; 64],
    scale: [c_char; 16],
    decode_ms: f64,
    rhythm_ms: f64,
    key_ms: f64,
}
unsafe extern "C" {
    pub(crate) fn pax_init();
    fn pax_analyze(
        filename: *const c_char,
        max_duration_seconds: f64,
        metrics: *mut Metrics,
    ) -> i32;
    fn pax_free(owner: *mut c_void);
}
struct NativeResult(Metrics);
impl Drop for NativeResult {
    fn drop(&mut self) {
        // SAFETY: The bridge transfers one vector owner after a successful call.
        unsafe { pax_free(self.0.owner) };
    }
}
fn peaks(audio: &[f32]) -> Vec<f64> {
    let buckets = audio.len().min(2048);
    if buckets == 0 {
        return Vec::new();
    }
    let short = audio.len() / buckets;
    let extra = audio.len() % buckets;
    let mut start = 0;
    let magnitudes: Vec<f32> = (0..buckets)
        .map(|i| {
            let end = start + short + usize::from(i < extra);
            let max = audio[start..end]
                .iter()
                .fold(0.0_f32, |acc, v| acc.max(v.abs()));
            start = end;
            max
        })
        .collect();
    let max = magnitudes.iter().copied().fold(0.0_f32, f32::max);
    magnitudes
        .iter()
        .map(|v| {
            if max == 0.0 {
                0.0
            } else {
                crate::rounding::round_decimal((f64::from(*v) / f64::from(max)).min(1.0), 4)
            }
        })
        .collect()
}
fn camelot(key: &str, scale: &str) -> &'static str {
    match (key, scale) {
        ("B", "major") => "1B",
        ("F#" | "Gb", "major") => "2B",
        ("Db" | "C#", "major") => "3B",
        ("Ab" | "G#", "major") => "4B",
        ("Eb" | "D#", "major") => "5B",
        ("Bb" | "A#", "major") => "6B",
        ("F", "major") => "7B",
        ("C", "major") => "8B",
        ("G", "major") => "9B",
        ("D", "major") => "10B",
        ("A", "major") => "11B",
        ("E", "major") => "12B",
        ("Ab" | "G#", "minor") => "1A",
        ("Eb" | "D#", "minor") => "2A",
        ("Bb" | "A#", "minor") => "3A",
        ("F", "minor") => "4A",
        ("C", "minor") => "5A",
        ("G", "minor") => "6A",
        ("D", "minor") => "7A",
        ("A", "minor") => "8A",
        ("E", "minor") => "9A",
        ("B", "minor") => "10A",
        ("F#" | "Gb", "minor") => "11A",
        ("Db" | "C#", "minor") => "12A",
        _ => "Unknown",
    }
}
#[derive(Serialize)]
pub(crate) struct Analysis {
    bpm: f64,
    bpm_confidence: f64,
    key_standard: String,
    key_camelot: &'static str,
    key_confidence: f64,
    pub(crate) duration: f64,
    peaks: Vec<f64>,
    preview: Vec<f64>,
}
fn empty_metrics() -> Metrics {
    Metrics {
        samples: std::ptr::null(),
        count: 0,
        owner: std::ptr::null_mut(),
        bpm: 0.0,
        bpm_confidence: 0.0,
        key_confidence: 0.0,
        key: [0; 64],
        scale: [0; 16],
        decode_ms: 0.0,
        rhythm_ms: 0.0,
        key_ms: 0.0,
    }
}
pub(crate) fn analyze(path: &std::path::Path, max_duration_seconds: f64) -> Result<Analysis, u16> {
    let path = CString::new(path.to_string_lossy().as_bytes()).map_err(|_| 1_u16)?;
    let mut raw = empty_metrics();
    // SAFETY: Filename is a live C string and raw has the C ABI layout.
    let code = unsafe { pax_analyze(path.as_ptr(), max_duration_seconds, &mut raw) };
    if code != 0 {
        return Err(code as u16);
    }
    let native = NativeResult(raw);
    let raw = &native.0;
    // SAFETY: The transferred owner keeps samples and terminated strings live.
    let (audio, key, scale) = unsafe {
        (
            std::slice::from_raw_parts(raw.samples, raw.count),
            CStr::from_ptr(raw.key.as_ptr()).to_string_lossy(),
            CStr::from_ptr(raw.scale.as_ptr()).to_string_lossy(),
        )
    };
    if ![raw.bpm, raw.bpm_confidence, raw.key_confidence]
        .iter()
        .all(|v| v.is_finite())
    {
        return Err(2);
    }
    if std::env::var("NATIVE_PROFILE").as_deref() == Ok("1") {
        eprintln!(
            "{}",
            serde_json::json!({"profile":"analysis", "decodeMs":raw.decode_ms,"rhythmMs":raw.rhythm_ms,"keyMs":raw.key_ms})
        );
    }
    let peaks = peaks(audio);
    let preview = peaks
        .chunks(peaks.len().div_ceil(96).max(1))
        .map(|chunk| chunk.iter().copied().fold(0.0, f64::max))
        .collect();
    let scale_title = if scale == "major" { "Major" } else { "Minor" };
    Ok(Analysis {
        bpm: crate::rounding::round_decimal(f64::from(raw.bpm), 1),
        bpm_confidence: f64::from(raw.bpm_confidence),
        key_standard: format!("{key} {scale_title}"),
        key_camelot: camelot(&key, &scale),
        key_confidence: f64::from(raw.key_confidence),
        duration: audio.len() as f64 / 44100.0,
        peaks,
        preview,
    })
}
