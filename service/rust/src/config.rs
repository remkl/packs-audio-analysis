use crate::{AnyError, Failure};
use std::{path::PathBuf, time::Duration};

const PAX_R2_HOSTNAME: &str = "0f254469eceb1b88f5b5ff50405617cd.r2.cloudflarestorage.com";

#[derive(Clone, Debug)]
pub(crate) struct Config {
    pub(crate) username: String,
    pub(crate) password: String,
    pub(crate) hosts: Vec<String>,
    pub(crate) allow_http: bool,
    pub(crate) workers: usize,
    pub(crate) queue: usize,
    pub(crate) queue_wait: Duration,
    pub(crate) deadline: Duration,
    pub(crate) max_download_bytes: i64,
    pub(crate) download_timeout_seconds: f64,
    pub(crate) max_duration_seconds: f64,
    pub(crate) data_dir: PathBuf,
    pub(crate) temp_dir: PathBuf,
}

impl Config {
    pub(crate) fn new() -> Result<Self, AnyError> {
        Self::from_lookup(|name| std::env::var(name).ok())
    }

    fn from_lookup(mut lookup: impl FnMut(&str) -> Option<String>) -> Result<Self, AnyError> {
        let mut value = |name, fallback: &str| lookup(name).unwrap_or_else(|| fallback.to_owned());
        let username = value("AUDIO_ANALYSIS_BASIC_AUTH_USERNAME", "analysis");
        let password = value("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD", "");
        let hosts = value("AUDIO_ANALYSIS_ALLOWED_HOSTS", "")
            .split(',')
            .map(|host| host.trim().to_lowercase())
            .filter(|host| !host.is_empty())
            .collect::<Vec<_>>();
        let allow_http = value("AUDIO_ANALYSIS_ALLOW_HTTP", "false").to_lowercase() == "true";
        // Python converts every number before performing its ordered startup checks.
        let max_download_bytes = integer(&value("AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES", "157286400"))?;
        let workers = integer(&value("AUDIO_ANALYSIS_CONCURRENCY", "2"))?;
        let download_timeout_seconds =
            number(&value("AUDIO_ANALYSIS_DOWNLOAD_TIMEOUT_SECONDS", "90"))?;
        let deadline = number(&value("AUDIO_ANALYSIS_DEADLINE_SECONDS", "100"))?;
        let queue = integer(&value(
            "AUDIO_ANALYSIS_QUEUE_LIMIT",
            &workers.saturating_mul(2).to_string(),
        ))?;
        let queue_wait = number(&value("AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS", "20"))?;
        let max_duration_seconds = number(&value("AUDIO_ANALYSIS_MAX_DURATION_SECONDS", "300"))?;

        if password.is_empty() {
            return Err("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD must be set".into());
        }
        if hosts.is_empty() {
            return Err("AUDIO_ANALYSIS_ALLOWED_HOSTS must be set".into());
        }
        if !(1..=4).contains(&workers) {
            return Err("AUDIO_ANALYSIS_CONCURRENCY must be between 1 and 4".into());
        }
        if !(0.0 < deadline && deadline <= 110.0) {
            return Err("AUDIO_ANALYSIS_DEADLINE_SECONDS must be above 0 and at most 110".into());
        }
        if queue < 0 {
            return Err("AUDIO_ANALYSIS_QUEUE_LIMIT must be 0 or more".into());
        }
        if !(0.0 < queue_wait && queue_wait < deadline) {
            return Err(
                "AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS must be above 0 and below the deadline".into(),
            );
        }
        if !max_duration_seconds.is_finite() || max_duration_seconds <= 0.0 {
            return Err("AUDIO_ANALYSIS_MAX_DURATION_SECONDS must be above 0".into());
        }
        let data_dir = PathBuf::from(value(
            "AUDIO_ANALYSIS_DATA_DIR",
            &std::env::temp_dir().to_string_lossy(),
        ))
        .join("audio-analysis");
        let temp_dir = data_dir.join("tmp");
        Ok(Self {
            username,
            password,
            hosts,
            allow_http,
            workers: usize::try_from(workers)?,
            // A larger Python queue is operationally unbounded; avoid Semaphore overflow.
            queue: usize::try_from(queue)?.min(tokio::sync::Semaphore::MAX_PERMITS - 4),
            queue_wait: Duration::try_from_secs_f64(queue_wait)?,
            deadline: Duration::try_from_secs_f64(deadline)?,
            max_download_bytes,
            download_timeout_seconds,
            max_duration_seconds,
            data_dir,
            temp_dir,
        })
    }

    pub(crate) fn validate(&self, value: &str, label: &str) -> Result<(), Failure> {
        let url = reqwest::Url::parse(value).map_err(|_| {
            Failure::new(422, "validate", "Invalid request body")
                .classified("RequestValidationError")
        })?;
        if url.scheme() != "https" && !(self.allow_http && url.scheme() == "http") {
            return Err(Failure::new(
                400,
                "validate",
                format!("{label} URL must use HTTPS"),
            ));
        }
        // urllib.parse.hostname removes the IPv6 brackets; url::Url retains them.
        let hostname = url
            .host_str()
            .unwrap_or_default()
            .trim_matches(['[', ']'])
            .to_lowercase();
        if hostname != PAX_R2_HOSTNAME && !self.hosts.contains(&hostname) {
            return Err(Failure::new(
                400,
                "validate",
                format!("{label} host is not allowed"),
            ));
        }
        Ok(())
    }
}

fn numeric_text(value: &str) -> Result<String, AnyError> {
    // Python 3.12's Unicode 15 decimal-digit blocks (unicodedata.decimal == 0).
    const ZEROES: &[u32] = &[
        48, 1632, 1776, 1984, 2406, 2534, 2662, 2790, 2918, 3046, 3174, 3302, 3430, 3558, 3664,
        3792, 3872, 4160, 4240, 6112, 6160, 6470, 6608, 6784, 6800, 6992, 7088, 7232, 7248, 42528,
        43216, 43264, 43472, 43504, 43600, 44016, 65296, 66720, 68912, 69734, 69872, 69942, 70096,
        70384, 70736, 70864, 71248, 71360, 71472, 71904, 72016, 72784, 73040, 73120, 73552, 92768,
        92864, 93008, 120782, 120792, 120802, 120812, 120822, 123200, 123632, 124144, 125264,
        130032,
    ];
    let value: String = value
        .trim()
        .chars()
        .map(|c| {
            let code = c as u32;
            ZEROES
                .iter()
                .find(|zero| code >= **zero && code < **zero + 10)
                .map_or(c, |zero| char::from(b'0' + (code - zero) as u8))
        })
        .collect();
    let bytes = value.as_bytes();
    for (index, byte) in bytes.iter().enumerate() {
        if *byte == b'_'
            && (index == 0
                || index + 1 == bytes.len()
                || !bytes[index - 1].is_ascii_digit()
                || !bytes[index + 1].is_ascii_digit())
        {
            return Err("Invalid numeric audio analysis configuration".into());
        }
    }
    Ok(value.replace('_', ""))
}

fn integer(value: &str) -> Result<i64, AnyError> {
    let text = numeric_text(value)?;
    let negative = text.starts_with('-');
    let digits = text.strip_prefix(['+', '-']).unwrap_or(&text);
    if digits.is_empty() || !digits.bytes().all(|c| c.is_ascii_digit()) {
        return Err("Invalid integer audio analysis configuration".into());
    }
    // Beyond the machine range, positive byte limits are effectively unbounded.
    // Preserve the sign and startup acceptance of Python's arbitrary-size ints.
    let value = digits.bytes().fold(0_i64, |n, c| {
        n.saturating_mul(10).saturating_add(i64::from(c - b'0'))
    });
    Ok(if negative { -value } else { value })
}

fn number(value: &str) -> Result<f64, AnyError> {
    numeric_text(value)?
        .parse()
        .map_err(|_| "Invalid numeric audio analysis configuration".into())
}

#[cfg(test)]
mod tests {
    use super::*;

    fn configured(overrides: &[(&str, &str)]) -> Result<Config, AnyError> {
        Config::from_lookup(|key| {
            overrides
                .iter()
                .find(|(name, _)| *name == key)
                .map(|(_, value)| (*value).to_owned())
                .or_else(|| match key {
                    "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD" => Some("secret".into()),
                    "AUDIO_ANALYSIS_ALLOWED_HOSTS" => Some("EXAMPLE.COM, ::1, ,".into()),
                    _ => None,
                })
        })
    }

    #[test]
    fn startup_checks_preserve_order_and_boundaries() {
        for (overrides, expected) in [
            (
                vec![
                    ("AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD", ""),
                    ("AUDIO_ANALYSIS_ALLOWED_HOSTS", ""),
                ],
                "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD must be set",
            ),
            (
                vec![("AUDIO_ANALYSIS_ALLOWED_HOSTS", " , ")],
                "AUDIO_ANALYSIS_ALLOWED_HOSTS must be set",
            ),
            (
                vec![("AUDIO_ANALYSIS_CONCURRENCY", "0")],
                "AUDIO_ANALYSIS_CONCURRENCY must be between 1 and 4",
            ),
            (
                vec![("AUDIO_ANALYSIS_DEADLINE_SECONDS", "nan")],
                "AUDIO_ANALYSIS_DEADLINE_SECONDS must be above 0 and at most 110",
            ),
            (
                vec![("AUDIO_ANALYSIS_QUEUE_LIMIT", "-1")],
                "AUDIO_ANALYSIS_QUEUE_LIMIT must be 0 or more",
            ),
            (
                vec![("AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS", "100")],
                "AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS must be above 0 and below the deadline",
            ),
            (
                vec![("AUDIO_ANALYSIS_MAX_DURATION_SECONDS", "0")],
                "AUDIO_ANALYSIS_MAX_DURATION_SECONDS must be above 0",
            ),
        ] {
            assert_eq!(configured(&overrides).unwrap_err().to_string(), expected);
        }
    }

    #[test]
    fn defaults_dynamic_queue_and_python_numeric_options_are_preserved() {
        let config = configured(&[
            ("AUDIO_ANALYSIS_CONCURRENCY", " +3 "),
            ("AUDIO_ANALYSIS_ALLOW_HTTP", "TrUe"),
            ("AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES", "-1"),
            ("AUDIO_ANALYSIS_DOWNLOAD_TIMEOUT_SECONDS", "0"),
            ("AUDIO_ANALYSIS_DATA_DIR", "/scratch"),
            ("AUDIO_ANALYSIS_MAX_DURATION_SECONDS", "1e300"),
        ])
        .unwrap();
        assert_eq!(config.queue, 6);
        assert_eq!(config.max_download_bytes, -1);
        assert_eq!(config.download_timeout_seconds, 0.0);
        assert_eq!(config.max_duration_seconds, 1e300);
        assert_eq!(config.data_dir, PathBuf::from("/scratch/audio-analysis"));
        assert_eq!(
            config.temp_dir,
            PathBuf::from("/scratch/audio-analysis/tmp")
        );
        assert!(config.validate("http://[::1]/", "Source").is_ok());
        assert!(
            configured(&[("AUDIO_ANALYSIS_ALLOW_HTTP", " true ")])
                .unwrap()
                .validate("http://example.com/", "Source")
                .is_err()
        );
    }

    #[test]
    fn duration_must_be_finite_and_positive_without_changing_other_check_order() {
        for value in [
            "nan",
            "+NaN",
            "-NaN",
            "inf",
            "+Infinity",
            "-inf",
            "1e309",
            "0",
            "-0",
            "-1",
        ] {
            assert_eq!(
                configured(&[("AUDIO_ANALYSIS_MAX_DURATION_SECONDS", value)])
                    .unwrap_err()
                    .to_string(),
                "AUDIO_ANALYSIS_MAX_DURATION_SECONDS must be above 0"
            );
        }
        for value in ["0.000000001", "300", "1e300"] {
            let config = configured(&[("AUDIO_ANALYSIS_MAX_DURATION_SECONDS", value)]).unwrap();
            assert!(config.max_duration_seconds.is_finite() && config.max_duration_seconds > 0.0);
        }
        for (setting, value, error) in [
            (
                "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD",
                "",
                "AUDIO_ANALYSIS_BASIC_AUTH_PASSWORD must be set",
            ),
            (
                "AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS",
                "0",
                "AUDIO_ANALYSIS_QUEUE_WAIT_SECONDS must be above 0 and below the deadline",
            ),
        ] {
            assert_eq!(
                configured(&[
                    (setting, value),
                    ("AUDIO_ANALYSIS_MAX_DURATION_SECONDS", "nan")
                ])
                .unwrap_err()
                .to_string(),
                error
            );
        }
    }

    #[test]
    fn normalized_hosts_are_exact_with_the_existing_r2_exception() {
        let config = configured(&[]).unwrap();
        assert!(config.validate("https://EXAMPLE.com/", "Source").is_ok());
        assert!(
            config
                .validate(&format!("https://{PAX_R2_HOSTNAME}/"), "Target")
                .is_ok()
        );
        assert_eq!(
            config
                .validate("https://example.com.evil/", "Target")
                .unwrap_err()
                .detail,
            "Target host is not allowed"
        );
        assert_eq!(integer("1_024").unwrap(), 1024);
        assert!(integer("1__024").is_err());
    }
}
