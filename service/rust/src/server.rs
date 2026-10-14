use crate::{AnyError, Failure, Job, MAX_RESPONSE_BYTES, busy, config::Config, contract};
use axum::{
    Router,
    body::Body,
    extract::{Request, State},
    http::{HeaderMap, HeaderValue, StatusCode, header},
    response::{IntoResponse, Response},
};
use futures_util::StreamExt;
use serde_json::json;
use std::{
    fs::{self, File},
    future::IntoFuture,
    io::{Read, Write},
    path::PathBuf,
    sync::{
        Arc, Mutex as StdMutex,
        atomic::{AtomicBool, AtomicU64, AtomicUsize, Ordering},
    },
    time::{Duration, Instant},
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout, Command},
    sync::{Mutex, Notify, OwnedSemaphorePermit, Semaphore, watch},
};

static NEXT_ID: AtomicU64 = AtomicU64::new(0);
// Leave time for worker-group termination/reaping before the host's 30 s grace ends.
const SHUTDOWN_DRAIN: Duration = Duration::from_secs(25);
// Shared with Python; enforce on received bytes even without Content-Length.
const MAX_REQUEST_BODY_BYTES: usize = 64 * 1024;

async fn read_request_body(body: Body) -> Result<Vec<u8>, Failure> {
    let mut stream = body.into_data_stream();
    let mut bytes = Vec::new();
    while let Some(chunk) = stream.next().await {
        let chunk = chunk.map_err(|_| {
            Failure::new(400, "request", "There was an error parsing the body")
                .classified("HTTPException")
        })?;
        if chunk.len() > MAX_REQUEST_BODY_BYTES - bytes.len() {
            return Err(Failure::new(413, "request", "Request body is too large")
                .classified("HTTPException"));
        }
        bytes.extend_from_slice(&chunk);
    }
    Ok(bytes)
}

struct TempDirectory(PathBuf);
impl Drop for TempDirectory {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

struct Outcome {
    status: u16,
    body: Vec<u8>,
    stage: String,
    error_class: Option<String>,
}
impl Outcome {
    fn failure(error: Failure, id: &str) -> Self {
        Self {
            status: error.status,
            body: error.body(id),
            stage: error.stage.into(),
            error_class: Some(error.error_class.into()),
        }
    }
}

struct Worker {
    child: Child,
    stdin: ChildStdin,
    stdout: BufReader<ChildStdout>,
    stage: String,
    error_class: Option<String>,
}
impl Drop for Worker {
    fn drop(&mut self) {
        if let Some(pid) = self.child.id() {
            // SAFETY: Each worker owns a separate process group, including its media children.
            unsafe {
                libc::kill(-(pid as i32), libc::SIGKILL);
            }
        }
    }
}
impl Worker {
    async fn new(config: &Config) -> Result<Self, AnyError> {
        let mut command = Command::new(std::env::current_exe()?);
        command
            .arg("--worker")
            .env_clear()
            .env("PATH", "/usr/local/bin:/usr/bin:/bin")
            .env(
                "AUDIO_ANALYSIS_MAX_DOWNLOAD_BYTES",
                config.max_download_bytes.to_string(),
            )
            .env(
                "AUDIO_ANALYSIS_MAX_DURATION_SECONDS",
                config.max_duration_seconds.to_string(),
            )
            .env(
                "AUDIO_ANALYSIS_DOWNLOAD_TIMEOUT_SECONDS",
                config.download_timeout_seconds.to_string(),
            )
            .process_group(0)
            .stdin(std::process::Stdio::piped())
            .stdout(std::process::Stdio::piped())
            .kill_on_drop(true);
        let mut child = command.spawn()?;
        let stdin = child.stdin.take().ok_or("Missing worker stdin")?;
        let stdout = BufReader::new(child.stdout.take().ok_or("Missing worker stdout")?);
        let mut worker = Self {
            child,
            stdin,
            stdout,
            stage: "queue".into(),
            error_class: None,
        };
        let ready = tokio::time::timeout(Duration::from_secs(15), worker.read_frame()).await;
        if !matches!(ready, Ok(Ok((0, ref body))) if body == b"ready") {
            worker.stop().await;
            return Err("Audio analysis worker could not start".into());
        }
        Ok(worker)
    }

    async fn read_frame(&mut self) -> Result<(u16, Vec<u8>), AnyError> {
        let mut head = [0; 6];
        self.stdout.read_exact(&mut head).await?;
        let length = u32::from_le_bytes([head[2], head[3], head[4], head[5]]) as usize;
        if length > MAX_RESPONSE_BYTES {
            return Err("Worker response too large".into());
        }
        let mut body = vec![0; length];
        self.stdout.read_exact(&mut body).await?;
        Ok((u16::from_le_bytes([head[0], head[1]]), body))
    }

    async fn exchange(&mut self, job: &Job) -> Result<Outcome, AnyError> {
        let mut request = serde_json::to_vec(job)?;
        request.push(b'\n');
        self.stdin.write_all(&request).await?;
        self.stdin.flush().await?;
        loop {
            let (status, body) = self.read_frame().await?;
            if status == 0 {
                let progress = String::from_utf8(body)?;
                if let Some(error) = progress.strip_prefix("error:") {
                    let (stage, class) = error.split_once(':').ok_or("Invalid worker progress")?;
                    self.stage = stage.into();
                    self.error_class = Some(class.into());
                } else {
                    self.stage = progress;
                }
                continue;
            }
            return Ok(Outcome {
                status,
                body,
                stage: self.stage.clone(),
                error_class: self.error_class.clone(),
            });
        }
    }

    async fn stop(&mut self) {
        let Some(pid) = self.child.id() else {
            return;
        };
        // SAFETY: Only this worker and its descendants belong to this group.
        unsafe {
            libc::kill(-(pid as i32), libc::SIGKILL);
        }
        let _ = self.child.wait().await;
        // The server is a subreaper. Reap adopted ffprobe/ffmpeg descendants too.
        loop {
            let mut status = 0;
            // SAFETY: waitpid writes a valid stack integer and selects only our worker's group.
            let reaped = unsafe { libc::waitpid(-(pid as i32), &mut status, libc::WNOHANG) };
            if reaped > 0 {
                continue;
            }
            if reaped < 0 {
                break;
            }
            tokio::time::sleep(Duration::from_millis(2)).await;
        }
    }

    async fn run(&mut self, job: &Job, remaining: Duration, pool: &Pool) -> Outcome {
        self.stage = "queue".into();
        self.error_class = None;
        let mut force = pool.force.subscribe();
        let already_forced = *force.borrow();
        let result = if already_forced {
            Err("shutdown")
        } else {
            tokio::select! {
                result = tokio::time::timeout(remaining, self.exchange(job)) => result.map_err(|_| "deadline"),
                _ = force.changed() => Err("shutdown"),
            }
        };
        if let Ok(Ok(outcome)) = result {
            return outcome;
        }
        let (status, detail, class) = match result {
            Err("shutdown") => (503, "Audio analysis is busy", "ServiceError"),
            Err(_) => (504, "Audio analysis took too long", "TimeoutError"),
            _ => (500, "Audio analysis failed", "RuntimeError"),
        };
        let stage = if status == 503 {
            "queue".into()
        } else {
            self.stage.clone()
        };
        self.stop().await;
        // An interrupted reply can never enter the next request. Retire the process before returning the slot.
        if !pool.closing.load(Ordering::Acquire) {
            match Self::new(&pool.config).await {
                Ok(replacement) => *self = replacement,
                Err(_) => pool.close(),
            }
        }
        Outcome {
            status,
            body: serde_json::to_vec(
                &json!({"detail":detail,"stage":stage,"requestId":job.request_id}),
            )
            .unwrap_or_default(),
            stage,
            error_class: Some(class.into()),
        }
    }
}

struct Pool {
    config: Config,
    workers: Vec<Mutex<Worker>>,
    free: StdMutex<Vec<usize>>,
    slots: Arc<Semaphore>,
    admission: Arc<Semaphore>,
    closing: AtomicBool,
    force: watch::Sender<bool>,
    inflight: AtomicUsize,
    drained: Notify,
}
struct Lease {
    pool: Arc<Pool>,
    index: usize,
    _slot: OwnedSemaphorePermit,
    _admission: OwnedSemaphorePermit,
}
impl Drop for Lease {
    fn drop(&mut self) {
        if let Ok(mut free) = self.pool.free.lock() {
            free.push(self.index);
        }
    }
}
struct ActiveJob(Arc<Pool>);
impl Drop for ActiveJob {
    fn drop(&mut self) {
        if self.0.inflight.fetch_sub(1, Ordering::AcqRel) == 1 {
            self.0.drained.notify_one();
        }
    }
}
impl Pool {
    async fn new(config: Config) -> Result<Arc<Self>, AnyError> {
        fs::create_dir_all(&config.data_dir)?;
        fs::create_dir_all(&config.temp_dir)?;
        let mut workers = Vec::with_capacity(config.workers);
        for _ in 0..config.workers {
            match Worker::new(&config).await {
                Ok(worker) => workers.push(Mutex::new(worker)),
                Err(error) => {
                    for worker in &workers {
                        worker.lock().await.stop().await;
                    }
                    return Err(error);
                }
            }
        }
        Ok(Arc::new(Self {
            free: StdMutex::new((0..config.workers).collect()),
            slots: Arc::new(Semaphore::new(config.workers)),
            admission: Arc::new(Semaphore::new(config.workers + config.queue)),
            workers,
            config,
            closing: AtomicBool::new(false),
            force: watch::channel(false).0,
            inflight: AtomicUsize::new(0),
            drained: Notify::new(),
        }))
    }
    fn close(&self) {
        self.closing.store(true, Ordering::Release);
        self.admission.close();
        self.slots.close();
    }
    async fn acquire(self: &Arc<Self>) -> Result<Lease, Failure> {
        let admission = self
            .admission
            .clone()
            .try_acquire_owned()
            .map_err(|_| busy())?;
        let slot = tokio::time::timeout(self.config.queue_wait, self.slots.clone().acquire_owned())
            .await
            .map_err(|_| busy().classified("TimeoutError"))?
            .map_err(|_| busy())?;
        let index = self
            .free
            .lock()
            .map_err(|_| Failure::new(500, "queue", "Audio analysis failed"))?
            .pop()
            .ok_or_else(|| Failure::new(500, "queue", "Audio analysis failed"))?;
        Ok(Lease {
            pool: self.clone(),
            index,
            _slot: slot,
            _admission: admission,
        })
    }
    async fn wait_drained(&self) {
        while self.inflight.load(Ordering::Acquire) != 0 {
            self.drained.notified().await;
        }
    }
    async fn stop_workers(&self) {
        for worker in &self.workers {
            worker.lock().await.stop().await;
        }
    }
}

fn request_id(headers: &HeaderMap) -> String {
    if let Some(id) = headers
        .get("x-pax-request-id")
        .and_then(|v| v.to_str().ok())
        && (8..=64).contains(&id.len())
        && id
            .bytes()
            .all(|c| c.is_ascii_alphanumeric() || c == b'_' || c == b'-')
    {
        return id.into();
    }
    let mut bytes = [0u8; 16];
    if File::open("/dev/urandom")
        .and_then(|mut f| f.read_exact(&mut bytes))
        .is_ok()
    {
        bytes[6] = (bytes[6] & 0x0f) | 0x40;
        bytes[8] = (bytes[8] & 0x3f) | 0x80;
        return bytes.iter().map(|b| format!("{b:02x}")).collect();
    }
    format!(
        "{:016x}{:016x}",
        std::process::id(),
        NEXT_ID.fetch_add(1, Ordering::Relaxed)
    )
}

fn response(outcome: Outcome, id: &str) -> Response {
    let mut result = (
        StatusCode::from_u16(outcome.status).unwrap_or(StatusCode::INTERNAL_SERVER_ERROR),
        [(header::CONTENT_TYPE, "application/json")],
        outcome.body,
    )
        .into_response();
    if let Ok(value) = id.parse() {
        result.headers_mut().insert("x-pax-request-id", value);
    }
    if outcome.status == 401 {
        result
            .headers_mut()
            .insert(header::WWW_AUTHENTICATE, HeaderValue::from_static("Basic"));
    }
    if outcome.status == 503 {
        result
            .headers_mut()
            .insert(header::RETRY_AFTER, HeaderValue::from_static("15"));
    }
    result
}

async fn execute(
    pool: Arc<Pool>,
    payload: contract::Payload,
    id: String,
    _active: ActiveJob,
) -> Outcome {
    let started = Instant::now();
    let lease = match pool.acquire().await {
        Ok(lease) => lease,
        Err(error) => return Outcome::failure(error, &id),
    };
    let ordinal = NEXT_ID.fetch_add(1, Ordering::Relaxed);
    let directory = pool
        .config
        .temp_dir
        .join(format!("job-{}-{ordinal}", std::process::id()));
    // Private directory and create_dir (not create_dir_all) prevent reuse of stale paths.
    use std::os::unix::fs::DirBuilderExt;
    if fs::DirBuilder::new()
        .mode(0o700)
        .create(&directory)
        .is_err()
    {
        return Outcome::failure(
            Failure::new(500, "download", "Audio analysis failed").classified("OSError"),
            &id,
        );
    }
    let _temp = TempDirectory(directory.clone());
    let job = Job {
        source_url: payload.source_url,
        target_url: payload.target_url,
        request_id: id,
        directory,
    };
    let remaining = pool.config.deadline.saturating_sub(started.elapsed());
    pool.workers[lease.index]
        .lock()
        .await
        .run(&job, remaining, &pool)
        .await
}

struct RequestLog {
    started: Instant,
    route: String,
    method: String,
    id: String,
    status: u16,
    stage: String,
    error_class: Option<String>,
}
impl RequestLog {
    fn record(&mut self, outcome: &Outcome) {
        self.status = outcome.status;
        self.stage.clone_from(&outcome.stage);
        self.error_class.clone_from(&outcome.error_class);
    }
}
impl Drop for RequestLog {
    fn drop(&mut self) {
        let line = json!({"service":"audio-analysis","route":self.route,"method":self.method,"status":self.status,
            "durationMs":(self.started.elapsed().as_secs_f64()*1000.0).round_ties_even() as u64,
            "stage":self.stage,"errorClass":self.error_class,"requestId":self.id});
        let mut output = std::io::stdout().lock();
        let _ = writeln!(output, "{line}");
        let _ = output.flush();
    }
}

fn route_path(path: &str) -> String {
    // ASGI routes see the URL-decoded path; Axum's raw URI retains percent escapes.
    let mut bytes = Vec::with_capacity(path.len());
    let mut input = path.as_bytes().iter().copied().peekable();
    while let Some(byte) = input.next() {
        if byte == b'%' {
            let mut ahead = input.clone();
            if let (Some(high), Some(low)) = (ahead.next(), ahead.next())
                && let (Some(high), Some(low)) =
                    ((high as char).to_digit(16), (low as char).to_digit(16))
            {
                bytes.push((high * 16 + low) as u8);
                input = ahead;
                continue;
            }
        }
        bytes.push(byte);
    }
    String::from_utf8_lossy(&bytes).into_owned()
}

async fn dispatch(State(pool): State<Arc<Pool>>, request: Request) -> Response {
    let id = request_id(request.headers());
    let decoded_path = route_path(request.uri().path());
    let path = decoded_path.as_str();
    let route = if matches!(path, "/healthz" | "/api/analyze-r2") {
        path
    } else {
        "other"
    };
    // Until admission this guard logs even if a client drops the body mid-read.
    let mut log = Some(RequestLog {
        started: Instant::now(),
        route: route.into(),
        method: request.method().to_string(),
        id: id.clone(),
        status: 500,
        stage: "request".into(),
        error_class: None,
    });
    let (outcome, extras) = dispatch_inner(pool, request, &id, &mut log).await;
    if let Some(log) = log.as_mut() {
        log.record(&outcome);
    }
    let redirect = outcome.status == 307;
    let mut response = response(outcome, &id);
    response.headers_mut().extend(extras);
    if redirect {
        response.headers_mut().remove(header::CONTENT_TYPE);
    }
    response
}

async fn dispatch_inner(
    pool: Arc<Pool>,
    request: Request<Body>,
    id: &str,
    log: &mut Option<RequestLog>,
) -> (Outcome, HeaderMap) {
    let decoded_path = route_path(request.uri().path());
    let path = decoded_path.as_str();
    let method = request.method().as_str();
    let mut extra = HeaderMap::new();
    let trimmed = path.trim_end_matches('/');
    if path != trimmed && matches!(trimmed, "/healthz" | "/api/analyze-r2") {
        let host = request
            .headers()
            .get(header::HOST)
            .and_then(|v| v.to_str().ok())
            .unwrap_or("localhost");
        let query = request
            .uri()
            .query()
            .map(|q| format!("?{q}"))
            .unwrap_or_default();
        if let Ok(location) = format!("http://{host}{trimmed}{query}").parse() {
            extra.insert(header::LOCATION, location);
        }
        return (
            Outcome {
                status: 307,
                body: Vec::new(),
                stage: "request".into(),
                error_class: None,
            },
            extra,
        );
    }
    let allowed = match path {
        "/healthz" => "GET",
        "/api/analyze-r2" => "POST",
        _ => "",
    };
    if allowed.is_empty() {
        return (
            Outcome::failure(
                Failure::new(404, "request", "Not Found").classified("HTTPException"),
                id,
            ),
            extra,
        );
    }
    if method != allowed {
        extra.insert(
            header::ALLOW,
            HeaderValue::from_static(if allowed == "GET" { "GET" } else { "POST" }),
        );
        return (
            Outcome::failure(
                Failure::new(405, "request", "Method Not Allowed").classified("HTTPException"),
                id,
            ),
            extra,
        );
    }
    if path == "/healthz" {
        let release = std::env::var("VERCEL_GIT_COMMIT_SHA").unwrap_or_default();
        let release = if release.trim().is_empty() {
            "local"
        } else {
            release.trim()
        };
        return (
            Outcome {
                status: 200,
                body: serde_json::to_vec(&json!({"status":"ok","release":release}))
                    .unwrap_or_default(),
                stage: "request".into(),
                error_class: None,
            },
            extra,
        );
    }
    let (parts, body) = request.into_parts();
    let bytes = match read_request_body(body).await {
        Ok(bytes) => bytes,
        Err(error) => return (Outcome::failure(error, id), extra),
    };
    let payload = match contract::validate_request(&pool.config, &parts.headers, &bytes) {
        Ok(payload) => payload,
        Err(error) => return (Outcome::failure(error, id), extra),
    };
    pool.inflight.fetch_add(1, Ordering::AcqRel);
    let active = ActiveJob(pool.clone());
    // Own admitted work independently of the socket. The shutdown controller tracks and drains it.
    let owned_id = id.to_owned();
    let owned_log = log.take();
    let outcome = match tokio::spawn(async move {
        // Admitted work owns its completion log even if the socket disappears.
        let mut log = owned_log;
        let outcome = execute(pool, payload, owned_id, active).await;
        if let Some(log) = log.as_mut() {
            log.record(&outcome);
        }
        outcome
    })
    .await
    {
        Ok(outcome) => outcome,
        Err(_) => Outcome::failure(
            Failure::new(500, "analyze", "Audio analysis failed").classified("RuntimeError"),
            id,
        ),
    };
    (outcome, extra)
}

pub(crate) async fn serve() -> Result<(), AnyError> {
    let config = Config::new()?;
    let port: u16 = std::env::var("PORT")
        .ok()
        .filter(|s| !s.is_empty())
        .unwrap_or_else(|| "80".into())
        .parse()?;
    let listener = tokio::net::TcpListener::bind(("0.0.0.0", port)).await?;
    // Register before workers start so SIGTERM cannot skip the cleanup controller.
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    let pool = Pool::new(config).await?;
    let _resource_profile = crate::resources::start();
    let app = Router::new().fallback(dispatch).with_state(pool.clone());
    let notified = Arc::new(Notify::new());
    let shutdown_notice = notified.clone();
    let shutdown_pool = pool.clone();
    let server = axum::serve(listener, app)
        .with_graceful_shutdown(async move {
            tokio::select! { _ = terminate.recv() => {}, _ = interrupt.recv() => {} }
            shutdown_pool.close();
            shutdown_notice.notify_one();
        })
        .into_future();
    tokio::pin!(server);
    let mut result = None;
    tokio::select! {
        ended = &mut server => result = Some(ended),
        _ = notified.notified() => {},
    }
    pool.close();
    // Live sockets and detached work share one bounded drain. Remember whether
    // serve has completed so the force path never polls a completed future.
    let drain = async {
        if result.is_none() {
            result = Some((&mut server).await);
        }
        pool.wait_drained().await;
    };
    if tokio::time::timeout(SHUTDOWN_DRAIN, drain).await.is_err() {
        pool.force.send_replace(true);
        pool.wait_drained().await;
        // Give interrupted handlers time to flush their final response. A stuck
        // HTTP client cannot extend shutdown beyond the host's grace period.
        if result.is_none() {
            result = tokio::time::timeout(Duration::from_secs(1), &mut server)
                .await
                .ok();
        }
    }
    pool.force.send_replace(true);
    pool.wait_drained().await;
    pool.stop_workers().await;
    result.unwrap_or(Ok(()))?;
    Ok(())
}

#[cfg(test)]
mod request_tests {
    use super::*;
    use axum::body::Bytes;

    #[tokio::test]
    async fn request_body_limit_accepts_exact_size_and_stops_on_excess_streamed_bytes() {
        assert_eq!(
            read_request_body(Body::from(vec![b'x'; MAX_REQUEST_BODY_BYTES]))
                .await
                .unwrap()
                .len(),
            MAX_REQUEST_BODY_BYTES
        );
        let received = Arc::new(AtomicUsize::new(0));
        let observed = received.clone();
        let chunks = futures_util::stream::iter([
            Bytes::from(vec![b'x'; MAX_REQUEST_BODY_BYTES]),
            Bytes::from_static(b"x"),
            Bytes::from_static(b"unread"),
        ])
        .map(move |chunk| {
            observed.fetch_add(1, Ordering::Relaxed);
            Ok::<_, std::io::Error>(chunk)
        });
        let error = read_request_body(Body::from_stream(chunks))
            .await
            .unwrap_err();
        assert_eq!(error.status, 413);
        assert_eq!(error.stage, "request");
        assert_eq!(error.detail, "Request body is too large");
        assert_eq!(error.error_class, "HTTPException");
        assert_eq!(received.load(Ordering::Relaxed), 2);
    }
}
