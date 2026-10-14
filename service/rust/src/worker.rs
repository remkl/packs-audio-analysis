use crate::{AnyError, Failure, Job, MAX_RESPONSE_BYTES, native};
use axum::http::header;
use flate2::{Decompress, FlushDecompress, Status};
use reqwest::blocking::Client;
use serde::Serialize;
use sha2::{Digest, Sha256};
use std::{
    error::Error,
    fs::File,
    io::{self, BufRead, Read, Write},
    path::{Path, PathBuf},
    time::Duration,
};

fn transport_failure(stage: &'static str, class: &'static str) -> Failure {
    Failure::new(
        502,
        stage,
        if stage == "download" {
            "Could not download audio source"
        } else {
            "Playback upload failed"
        },
    )
    .classified(class)
}

fn transport_error(error: &(dyn Error + 'static), stage: &'static str, reading: bool) -> Failure {
    let mut current = Some(error);
    let (mut timeout, mut connect, mut protocol) = (false, false, false);
    while let Some(error) = current {
        if let Some(error) = error.downcast_ref::<reqwest::Error>() {
            timeout |= error.is_timeout();
            connect |= error.is_connect();
        }
        if let Some(error) = error.downcast_ref::<hyper::Error>() {
            protocol |= error.is_incomplete_message() || error.is_parse();
        }
        if let Some(error) = error.downcast_ref::<io::Error>() {
            timeout |= error.kind() == io::ErrorKind::TimedOut;
            protocol |= error.kind() == io::ErrorKind::UnexpectedEof;
            if let Some(inner) = error.get_ref() {
                current = Some(inner);
                continue;
            }
        }
        current = error.source();
    }
    let class = if connect {
        if timeout {
            "ConnectTimeout"
        } else {
            "ConnectError"
        }
    } else if timeout {
        if reading {
            "ReadTimeout"
        } else {
            "WriteTimeout"
        }
    } else if protocol {
        "RemoteProtocolError"
    } else if reading {
        "ReadError"
    } else {
        "WriteError"
    };
    transport_failure(stage, class)
}

fn io_class(error: &io::Error) -> &'static str {
    match error.kind() {
        io::ErrorKind::NotFound => "FileNotFoundError",
        io::ErrorKind::PermissionDenied => "PermissionError",
        io::ErrorKind::AlreadyExists => "FileExistsError",
        io::ErrorKind::IsADirectory => "IsADirectoryError",
        io::ErrorKind::NotADirectory => "NotADirectoryError",
        _ => "OSError",
    }
}

struct ContentDecoder {
    engine: Decompress,
    raw_fallback: bool,
    first: bool,
    ended: bool,
}
impl ContentDecoder {
    fn new(encoding: &str) -> Option<Self> {
        let (engine, raw_fallback) = match encoding {
            "gzip" => (Decompress::new_gzip(15), false),
            "deflate" => (Decompress::new(true), true),
            _ => return None,
        };
        Some(Self {
            engine,
            raw_fallback,
            first: true,
            ended: false,
        })
    }
    fn decode(
        &mut self,
        input: &[u8],
        emit: &mut impl FnMut(&[u8]) -> Result<(), Failure>,
        stage: &'static str,
    ) -> Result<(), Failure> {
        if self.ended {
            return Ok(());
        }
        let mut offset = 0;
        let first = self.first;
        self.first = false;
        loop {
            let mut output = [0_u8; 65536];
            let (before_in, before_out) = (self.engine.total_in(), self.engine.total_out());
            let status =
                match self
                    .engine
                    .decompress(&input[offset..], &mut output, FlushDecompress::None)
                {
                    Ok(status) => status,
                    Err(_) if first && self.raw_fallback => {
                        self.engine = Decompress::new(false);
                        self.raw_fallback = false;
                        self.first = true;
                        return self.decode(input, emit, stage);
                    }
                    Err(_) => return Err(transport_failure(stage, "DecodingError")),
                };
            let consumed = (self.engine.total_in() - before_in) as usize;
            let produced = (self.engine.total_out() - before_out) as usize;
            offset += consumed;
            if produced != 0 {
                emit(&output[..produced])?;
            }
            if status == Status::StreamEnd {
                self.ended = true;
                return Ok(());
            }
            if consumed == 0 && produced == 0 {
                return Ok(());
            }
            if offset == input.len() && produced < output.len() {
                return Ok(());
            }
        }
    }
}

fn content_decoders(headers: &axum::http::HeaderMap) -> Vec<ContentDecoder> {
    let encodings = headers
        .get_all(header::CONTENT_ENCODING)
        .iter()
        .filter_map(|value| value.to_str().ok())
        .collect::<Vec<_>>()
        .join(",");
    encodings
        .split(',')
        .rev()
        .filter_map(|encoding| ContentDecoder::new(encoding.trim()))
        .collect()
}

fn decode_content(
    decoders: &mut [ContentDecoder],
    input: &[u8],
    emit: &mut impl FnMut(&[u8]) -> Result<(), Failure>,
    stage: &'static str,
) -> Result<(), Failure> {
    match decoders.split_first_mut() {
        None => emit(input),
        Some((first, rest)) => first.decode(
            input,
            &mut |bytes| decode_content(rest, bytes, emit, stage),
            stage,
        ),
    }
}

fn variable<T: std::str::FromStr>(name: &str, fallback: &str) -> Result<T, AnyError>
where
    T::Err: std::error::Error + Send + Sync + 'static,
{
    Ok(std::env::var(name)
        .unwrap_or_else(|_| fallback.into())
        .trim()
        .parse()?)
}

fn subprocess(
    program: &str,
    args: &[&str],
    path: &Path,
) -> Result<std::process::Output, io::Error> {
    std::process::Command::new(program)
        .args(args)
        .arg(path)
        .env_clear()
        .env("PATH", "/usr/local/bin:/usr/bin:/bin")
        .stdin(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .output()
}
fn source_path(job: &Job) -> PathBuf {
    let suffix = reqwest::Url::parse(&job.source_url)
        .ok()
        .and_then(|u| {
            Path::new(u.path())
                .extension()
                .map(|s| s.to_string_lossy().to_lowercase())
        })
        .filter(|s| s.chars().count() < 10 && !s.is_empty() && s.chars().all(char::is_alphanumeric))
        .unwrap_or_else(|| "audio".into());
    job.directory.join(format!("source.{suffix}"))
}
fn download(client: &Client, job: &Job) -> Result<PathBuf, Failure> {
    let limit: i64 = variable("AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES", "157286400")
        .map_err(|_| transport_failure("download", "ValueError"))?;
    download_with_limit(client, job, limit)
}
fn download_with_limit(client: &Client, job: &Job, limit: i64) -> Result<PathBuf, Failure> {
    let fail = || Failure::new(502, "download", "Could not download audio source");
    let mut result = client
        .get(&job.source_url)
        .send()
        .map_err(|error| transport_error(&error, "download", true))?;
    if result.status().as_u16() != 200 {
        return Err(fail());
    }
    if let Some(length) = result.headers().get(header::CONTENT_LENGTH) {
        let length = length
            .to_str()
            .map_err(|_| fail().classified("ValueError"))?
            .parse::<i64>()
            .map_err(|_| fail().classified("ValueError"))?;
        if length > limit {
            return Err(Failure::new(
                413,
                "download",
                "Audio file exceeds size limit",
            ));
        }
    }
    // Keep the wire Content-Length for the early limit, then count decoded bytes,
    // exactly as httpx.Response.aiter_bytes() does in the Python service.
    let mut decoders = content_decoders(result.headers());
    let path = source_path(job);
    let mut output = File::create(&path).map_err(|error| {
        Failure::new(500, "download", "Audio analysis failed").classified(io_class(&error))
    })?;
    let mut buffer = vec![0; 65536];
    let mut total = 0_i64;
    loop {
        let count = result
            .read(&mut buffer)
            .map_err(|error| transport_error(&error, "download", true))?;
        if count == 0 {
            break;
        }
        write_decoded(
            &mut decoders,
            &buffer[..count],
            &mut output,
            &mut total,
            limit,
        )?;
    }
    write_decoded(&mut decoders, &[], &mut output, &mut total, limit)?;
    Ok(path)
}
fn write_decoded(
    decoders: &mut [ContentDecoder],
    bytes: &[u8],
    output: &mut File,
    total: &mut i64,
    limit: i64,
) -> Result<(), Failure> {
    let mut pending = None;
    // httpx finishes decoding each network chunk before the caller checks its
    // size or writes it. Retain that error precedence without buffering expansion.
    decode_content(
        decoders,
        bytes,
        &mut |bytes| {
            if pending.is_some() || bytes.is_empty() {
                return Ok(());
            }
            *total = total.saturating_add(bytes.len() as i64);
            if *total > limit {
                pending = Some(Failure::new(
                    413,
                    "download",
                    "Audio file exceeds size limit",
                ));
            } else if let Err(error) = output.write_all(bytes) {
                pending = Some(transport_failure("download", io_class(&error)));
            }
            Ok(())
        },
        "download",
    )?;
    pending.map_or(Ok(()), Err)
}
#[derive(Serialize)]
struct Playback {
    #[serde(rename = "sizeBytes")]
    size_bytes: u64,
    sha256: String,
}
const MEDIA_FORMATS: &str = "wav,aiff,mp3,flac,ogg,mov,matroska,webm";

fn encode(source: &Path, path: &Path) -> Result<Playback, Failure> {
    let fail = || Failure::new(422, "encode", "Playback encoding failed");
    let status = std::process::Command::new("ffmpeg")
        .args([
            "-nostdin",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            MEDIA_FORMATS,
            // Automatic frame threads can exhaust the inherited memory limit.
            "-threads",
            "1",
            "-i",
        ])
        .arg(source)
        .args(["-vn", "-codec:a", "libmp3lame", "-b:a", "192k"])
        .arg(path)
        .env_clear()
        .env("PATH", "/usr/local/bin:/usr/bin:/bin")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map_err(|error| {
            Failure::new(500, "encode", "Audio analysis failed").classified(io_class(&error))
        })?;
    if !status.success() {
        return Err(fail());
    }
    let mut file = File::open(path).map_err(|error| {
        Failure::new(500, "encode", "Audio analysis failed").classified(io_class(&error))
    })?;
    let mut hash = Sha256::new();
    let mut buffer = vec![0; 1024 * 1024];
    let mut size_bytes = 0;
    loop {
        let n = file.read(&mut buffer).map_err(|error| {
            Failure::new(500, "encode", "Audio analysis failed").classified(io_class(&error))
        })?;
        if n == 0 {
            break;
        }
        size_bytes += n as u64;
        hash.update(&buffer[..n]);
    }
    Ok(Playback {
        size_bytes,
        sha256: format!("{:x}", hash.finalize()),
    })
}
#[derive(Serialize)]
struct Combined {
    #[serde(flatten)]
    analysis: native::Analysis,
    playback: Option<Playback>,
}
fn progress(stage: &str) {
    let mut output = io::stdout().lock();
    let _ = output.write_all(&0u16.to_le_bytes());
    let _ = output.write_all(&(stage.len() as u32).to_le_bytes());
    let _ = output.write_all(stage.as_bytes());
    let _ = output.flush();
}

fn duration_limit_message(limit: f64) -> String {
    let (value, unit) = if limit % 60.0 == 0.0 {
        (limit / 60.0, "minutes")
    } else {
        (limit, "seconds")
    };
    let mut buffer = [0_u8; 64];
    // Python's :g defaults to six significant digits and a two-digit exponent.
    // Both processes retain the C numeric locale; snprintf uses the same format.
    // SAFETY: The buffer is writable, its size is correct, and %.6g accepts double.
    let length = unsafe {
        libc::snprintf(
            buffer.as_mut_ptr().cast(),
            buffer.len(),
            c"%.6g".as_ptr(),
            value,
        )
    };
    let number = usize::try_from(length)
        .ok()
        .filter(|length| *length < buffer.len())
        .and_then(|length| std::str::from_utf8(&buffer[..length]).ok())
        .map(str::to_owned)
        .unwrap_or_else(|| value.to_string());
    format!("Audio is longer than {number} {unit}")
}

fn run_job(client: &Client, job: &Job) -> Result<Combined, Failure> {
    progress("download");
    let source = download(client, job)?;
    progress("probe");
    let probe = subprocess(
        "ffprobe",
        &[
            "-v",
            "error",
            "-protocol_whitelist",
            "file",
            "-format_whitelist",
            MEDIA_FORMATS,
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
        ],
        &source,
    )
    .map_err(|error| {
        Failure::new(500, "probe", "Audio analysis failed").classified(io_class(&error))
    })?;
    if !probe.status.success() {
        return Err(Failure::new(422, "probe", "Audio could not be decoded"));
    }
    let duration = std::str::from_utf8(&probe.stdout)
        .ok()
        .and_then(|s| s.trim().parse::<f64>().ok())
        .filter(|n| n.is_finite());
    let limit: f64 = variable("AUDIO_ANALYSIS_MAX_DURATION_SECONDS", "300")
        .map_err(|_| Failure::new(500, "probe", "Audio analysis failed"))?;
    let too_long = |stage| Failure::new(413, stage, duration_limit_message(limit));
    if duration.is_some_and(|d| d > limit) {
        return Err(too_long("probe"));
    }
    let playback_path = job.directory.join("playback.mp3");
    progress("analyze");
    let analysis = native::analyze(&source, limit).map_err(|code| match code {
        1 => {
            Failure::new(422, "decode", "Audio could not be decoded").classified("UndecodableAudio")
        }
        3 => Failure::new(500, "analyze", "Audio analysis failed").classified("RuntimeError"),
        4 => too_long("decode").classified("AudioTooLong"),
        5 => Failure::new(413, "decode", "Decoded audio exceeds memory limit")
            .classified("DecodedAudioTooLarge"),
        _ => Failure::new(422, "analyze", "Audio could not be analyzed")
            .classified("AnalysisRejected"),
    });
    let analysis = analysis?;
    if analysis.duration > limit {
        return Err(too_long("decode"));
    }
    let mut playback = None;
    if let Some(target) = &job.target_url {
        progress("encode");
        let result = encode(&source, &playback_path)?;
        progress("upload");
        upload(target, &playback_path, result.size_bytes)?;
        playback = Some(result);
    }
    Ok(Combined { analysis, playback })
}
pub(crate) fn run() -> Result<(), AnyError> {
    // SAFETY: Each process initializes the engine once and runs one analysis at a time.
    unsafe { native::pax_init() };
    limit_address_space()?;
    progress("ready");
    let mut output = io::stdout().lock();
    for line in io::stdin().lock().lines() {
        let job: Job = serde_json::from_str(&line?)?;
        let result = client_for_job().and_then(|client| run_job(&client, &job));
        let (status, body) = match result {
            Ok(value) => (200, serde_json::to_vec(&value)?),
            Err(error) => {
                progress(&format!("error:{}:{}", error.stage, error.error_class));
                (error.status, error.body(&job.request_id))
            }
        };
        if body.len() > MAX_RESPONSE_BYTES {
            return Err("Worker response too large".into());
        }
        output.write_all(&u16::to_le_bytes(status))?;
        output.write_all(&(body.len() as u32).to_le_bytes())?;
        output.write_all(&body)?;
        output.flush()?;
    }
    Ok(())
}

fn limit_address_space() -> Result<(), AnyError> {
    // Include loaded libraries/emulator mappings in a fixed startup baseline.
    // Never rebase per job: retained allocations cannot increase this budget.
    const ADDITIONAL_BYTES: u64 = 512 * 1024 * 1024;
    let status = std::fs::read_to_string("/proc/self/status")?;
    let baseline = status
        .lines()
        .find_map(|line| line.strip_prefix("VmSize:"))
        .and_then(|line| line.split_whitespace().next())
        .ok_or("Worker virtual memory size is unavailable")?
        .parse::<u64>()?
        .checked_mul(1024)
        .ok_or("Worker virtual memory size overflow")?;
    let ceiling = baseline
        .checked_add(ADDITIONAL_BYTES)
        .ok_or("Worker virtual memory limit overflow")?;
    let mut limit = libc::rlimit {
        rlim_cur: 0,
        rlim_max: 0,
    };
    // SAFETY: getrlimit/setrlimit operate on this worker only with valid pointers.
    if unsafe { libc::getrlimit(libc::RLIMIT_AS, &mut limit) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    limit.rlim_cur = limit.rlim_cur.min(limit.rlim_max).min(ceiling);
    if unsafe { libc::setrlimit(libc::RLIMIT_AS, &limit) } != 0 {
        return Err(io::Error::last_os_error().into());
    }
    eprintln!(
        "{}",
        serde_json::json!({
            "service": "audio-analysis", "event": "worker-memory-limit",
            "workerPid": std::process::id(), "baselineVirtualBytes": baseline,
            "additionalVirtualBytes": ADDITIONAL_BYTES, "virtualLimitBytes": limit.rlim_cur,
        })
    );
    Ok(())
}

fn transfer_timeout() -> Result<Option<Duration>, Failure> {
    let seconds: f64 = variable("AUDIO_ANALYSIS_DOWNLOAD_TIMEOUT_SECONDS", "90")
        .map_err(|_| transport_failure("download", "ValueError"))?;
    if seconds == f64::INFINITY || seconds.is_nan() {
        return Ok(None);
    }
    Duration::try_from_secs_f64(seconds.max(0.0))
        .map(Some)
        .map_err(|_| transport_failure("download", "ValueError"))
}

fn client_headers() -> axum::http::HeaderMap {
    let mut headers = axum::http::HeaderMap::new();
    headers.insert(
        header::ACCEPT_ENCODING,
        axum::http::HeaderValue::from_static("gzip, deflate"),
    );
    headers.insert(
        header::USER_AGENT,
        axum::http::HeaderValue::from_static("python-httpx/0.28.1"),
    );
    headers
}

fn upload(target: &str, path: &Path, size: u64) -> Result<(), Failure> {
    let timeout = transfer_timeout()?;
    let file = File::open(path).map_err(|error| {
        Failure::new(500, "upload", "Audio analysis failed").classified(io_class(&error))
    })?;
    // This runtime owns the request and all network tasks. Dropping a timed-out
    // request and then this runtime closes the socket before the file is removed.
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
        .map_err(|_| transport_failure("upload", "RuntimeError"))?
        .block_on(upload_async(target, file, size, timeout))
}

async fn upload_async(
    target: &str,
    file: File,
    size: u64,
    timeout: Option<Duration>,
) -> Result<(), Failure> {
    let mut builder = reqwest::Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .default_headers(client_headers())
        .pool_max_idle_per_host(0);
    if let Some(timeout) = timeout {
        builder = builder.connect_timeout(timeout);
    }
    let client = builder
        .build()
        .map_err(|error| transport_error(&error, "upload", false))?;
    let (progress, mut changed) = tokio::sync::watch::channel((0_u64, false));
    let stream = futures_util::stream::try_unfold(
        (file, progress, 0_u64),
        move |(mut file, progress, sequence)| async move {
            let mut bytes = vec![0_u8; 1024 * 1024];
            let count = file.read(&mut bytes)?;
            progress.send_replace((sequence + count as u64, sequence + count as u64 >= size));
            if count == 0 {
                return Ok::<_, io::Error>(None);
            }
            bytes.truncate(count);
            Ok(Some((bytes, (file, progress, sequence + count as u64))))
        },
    );
    let future = client
        .put(target)
        .header(header::CONTENT_TYPE, "audio/mpeg")
        .header(header::CONTENT_LENGTH, size)
        .body(reqwest::Body::wrap_stream(stream))
        .send();
    tokio::pin!(future);
    let mut response = if let Some(timeout) = timeout {
        let idle = tokio::time::sleep(timeout);
        tokio::pin!(idle);
        let mut progress_open = true;
        loop {
            tokio::select! {
                biased;
                result = &mut future => break result.map_err(|error| transport_error(&error, "upload", changed.borrow().1))?,
                update = changed.changed(), if progress_open => {
                    if update.is_err() { progress_open = false; }
                    idle.as_mut().reset(tokio::time::Instant::now() + timeout);
                }
                _ = &mut idle => {
                    let state = *changed.borrow();
                    return Err(transport_failure("upload", if state.0 == 0 { "ConnectTimeout" } else if state.1 { "ReadTimeout" } else { "WriteTimeout" }));
                }
            }
        }
    } else {
        future
            .await
            .map_err(|error| transport_error(&error, "upload", changed.borrow().1))?
    };
    let status = response.status();
    let mut decoders = content_decoders(response.headers());
    loop {
        let chunk = match timeout {
            Some(timeout) => tokio::time::timeout(timeout, response.chunk())
                .await
                .map_err(|_| transport_failure("upload", "ReadTimeout"))?,
            None => response.chunk().await,
        }
        .map_err(|error| transport_error(&error, "upload", true))?;
        let Some(chunk) = chunk else {
            break;
        };
        decode_content(&mut decoders, &chunk, &mut |_| Ok(()), "upload")?;
    }
    decode_content(&mut decoders, &[], &mut |_| Ok(()), "upload")?;
    if status.is_success() {
        Ok(())
    } else {
        Err(transport_failure("upload", "ServiceError"))
    }
}

fn client_for_job() -> Result<Client, Failure> {
    let invalid = || {
        Failure::new(502, "download", "Could not download audio source").classified("ValueError")
    };
    let timeout = transfer_timeout()?;
    make_client(timeout).map_err(|_| invalid())
}

fn make_client(timeout: Option<Duration>) -> Result<Client, reqwest::Error> {
    Client::builder()
        .redirect(reqwest::redirect::Policy::none())
        .default_headers(client_headers())
        .timeout(timeout)
        .connect_timeout(timeout)
        .pool_max_idle_per_host(0)
        .build()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::{
        net::{TcpListener, TcpStream},
        sync::atomic::{AtomicU64, Ordering},
        thread,
    };
    static NEXT: AtomicU64 = AtomicU64::new(0);

    #[test]
    fn duration_errors_use_python_general_number_formatting() {
        for (limit, expected) in [
            (300.0, "5 minutes"),
            (0.00001, "1e-05 seconds"),
            (1.23456789, "1.23457 seconds"),
            (999999.9, "1e+06 seconds"),
            (60_000_000.0, "1e+06 minutes"),
            (1800.00006, "1800 seconds"),
        ] {
            assert_eq!(
                duration_limit_message(limit),
                format!("Audio is longer than {expected}")
            );
        }
    }
    struct Fixture(PathBuf);
    impl Fixture {
        fn new() -> Self {
            let path = std::env::temp_dir().join(format!(
                "pax-transport-test-{}-{}",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            std::fs::create_dir(&path).unwrap();
            Self(path)
        }
        fn job(&self, source: String) -> Job {
            Job {
                source_url: source,
                target_url: None,
                request_id: "transport-test".into(),
                directory: self.0.clone(),
            }
        }
    }
    impl Drop for Fixture {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }
    fn server(
        handler: impl FnOnce(TcpStream) + Send + 'static,
    ) -> (String, thread::JoinHandle<()>) {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/audio.wav", listener.local_addr().unwrap());
        let handle = thread::spawn(move || {
            let (stream, _) = listener.accept().unwrap();
            stream
                .set_read_timeout(Some(Duration::from_secs(5)))
                .unwrap();
            handler(stream);
        });
        (url, handle)
    }
    fn request_head(stream: &mut TcpStream) -> String {
        let mut head = Vec::new();
        let mut byte = [0];
        while !head.ends_with(b"\r\n\r\n") {
            stream.read_exact(&mut byte).unwrap();
            head.push(byte[0]);
        }
        String::from_utf8(head).unwrap()
    }
    fn response(stream: &mut TcpStream, encoding: &str, body: &[u8]) {
        write!(stream, "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nContent-Encoding: {encoding}\r\nConnection: close\r\n\r\n", body.len()).unwrap();
        let _ = stream.write_all(body);
    }
    fn compressed(data: &[u8], kind: &str) -> Vec<u8> {
        match kind {
            "gzip" => {
                let mut encoder =
                    flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
                encoder.write_all(data).unwrap();
                encoder.finish().unwrap()
            }
            "deflate" => {
                let mut encoder =
                    flate2::write::ZlibEncoder::new(Vec::new(), flate2::Compression::default());
                encoder.write_all(data).unwrap();
                encoder.finish().unwrap()
            }
            _ => {
                let mut encoder =
                    flate2::write::DeflateEncoder::new(Vec::new(), flate2::Compression::default());
                encoder.write_all(data).unwrap();
                encoder.finish().unwrap()
            }
        }
    }
    #[test]
    fn compressed_downloads_preserve_wire_and_decoded_size_checks() {
        let data = vec![42_u8; 16384];
        for encoding in ["gzip", "deflate", "raw-deflate", "gzip, deflate"] {
            let bytes = if encoding == "gzip, deflate" {
                compressed(&compressed(&data, "gzip"), "deflate")
            } else {
                compressed(&data, encoding)
            };
            for limit in [4, 4096, 32768] {
                let wire = bytes.clone();
                let label = if encoding == "raw-deflate" {
                    "deflate"
                } else {
                    encoding
                };
                let (url, handle) = server(move |mut stream| {
                    request_head(&mut stream);
                    response(&mut stream, label, &wire);
                });
                let fixture = Fixture::new();
                let result = download_with_limit(
                    &make_client(Some(Duration::from_secs(2))).unwrap(),
                    &fixture.job(url),
                    limit,
                );
                if limit < data.len() as i64 {
                    let error = result.unwrap_err();
                    assert_eq!((error.status, error.error_class), (413, "ServiceError"));
                } else {
                    assert_eq!(std::fs::read(result.unwrap()).unwrap(), data);
                }
                handle.join().unwrap();
            }
        }
    }
    #[test]
    fn corrupt_and_truncated_transport_errors_match_httpx_classes() {
        for (encoding, bytes, declared, expected) in [
            ("gzip", b"not gzip".to_vec(), 8, "DecodingError"),
            ("identity", b"short".to_vec(), 100, "RemoteProtocolError"),
        ] {
            let (url, handle) = server(move |mut stream| {
                request_head(&mut stream);
                write!(stream, "HTTP/1.1 200 OK\r\nContent-Length: {declared}\r\nContent-Encoding: {encoding}\r\nConnection: close\r\n\r\n").unwrap();
                let _ = stream.write_all(&bytes);
            });
            let fixture = Fixture::new();
            let error = download_with_limit(
                &make_client(Some(Duration::from_secs(2))).unwrap(),
                &fixture.job(url),
                1000,
            )
            .unwrap_err();
            assert_eq!((error.status, error.error_class), (502, expected));
            handle.join().unwrap();
        }
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let url = format!("http://{}/", listener.local_addr().unwrap());
        drop(listener);
        let fixture = Fixture::new();
        assert_eq!(
            download_with_limit(
                &make_client(Some(Duration::from_secs(1))).unwrap(),
                &fixture.job(url),
                1000
            )
            .unwrap_err()
            .error_class,
            "ConnectError"
        );
    }
    #[test]
    fn download_timeout_resets_for_each_read() {
        for stalled in [false, true] {
            let (url, handle) = server(move |mut stream| {
                request_head(&mut stream);
                stream
                    .write_all(
                        b"HTTP/1.1 200 OK\r\nContent-Length: 20\r\nConnection: close\r\n\r\n",
                    )
                    .unwrap();
                for _ in 0..20 {
                    if stream.write_all(b"x").is_err() {
                        break;
                    }
                    thread::sleep(Duration::from_millis(if stalled { 400 } else { 25 }));
                }
            });
            let fixture = Fixture::new();
            let result = download_with_limit(
                &make_client(Some(Duration::from_millis(150))).unwrap(),
                &fixture.job(url),
                1000,
            );
            if stalled {
                assert_eq!(result.unwrap_err().error_class, "ReadTimeout");
            } else {
                assert_eq!(std::fs::read(result.unwrap()).unwrap(), vec![b'x'; 20]);
            }
            handle.join().unwrap();
        }
    }
    #[test]
    fn upload_timeout_resets_as_body_moves_and_distinguishes_read_stalls() {
        let timeout = Duration::from_millis(350);
        for mode in ["steady", "write-stall", "read-stall"] {
            let (url, handle) = server(move |mut stream| {
                let head = request_head(&mut stream);
                let length: usize = head
                    .lines()
                    .find_map(|line| {
                        line.to_ascii_lowercase()
                            .strip_prefix("content-length: ")
                            .and_then(|n| n.parse().ok())
                    })
                    .unwrap();
                if mode == "write-stall" {
                    thread::sleep(Duration::from_millis(900));
                    return;
                }
                let mut buffer = [0_u8; 262144];
                let mut read = 0;
                while read < length {
                    match stream.read(&mut buffer[..(length - read).min(262144)]) {
                        Ok(0) | Err(_) => return,
                        Ok(n) => read += n,
                    };
                    if mode == "steady" {
                        thread::sleep(Duration::from_millis(12));
                    }
                }
                if mode == "read-stall" {
                    thread::sleep(Duration::from_millis(900));
                }
                let _ = stream.write_all(
                    b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\nConnection: close\r\n\r\n",
                );
            });
            let fixture = Fixture::new();
            let path = fixture.0.join("playback.mp3");
            let file = File::create(&path).unwrap();
            file.set_len(16 * 1024 * 1024).unwrap();
            drop(file);
            let runtime = tokio::runtime::Builder::new_current_thread()
                .enable_all()
                .build()
                .unwrap();
            let started = std::time::Instant::now();
            let result = runtime.block_on(upload_async(
                &url,
                File::open(path).unwrap(),
                16 * 1024 * 1024,
                Some(timeout),
            ));
            match mode {
                "steady" => {
                    assert!(result.is_ok(), "{result:?}");
                    assert!(started.elapsed() > timeout);
                }
                "write-stall" => assert_eq!(result.unwrap_err().error_class, "WriteTimeout"),
                _ => assert_eq!(result.unwrap_err().error_class, "ReadTimeout"),
            }
            drop(runtime);
            handle.join().unwrap();
        }
    }
}
