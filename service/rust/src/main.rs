mod config;
mod contract;
mod native;
mod resources;
mod rounding;
mod server;
mod worker;

use serde::{Deserialize, Serialize};
use serde_json::json;
use std::path::PathBuf;

const MAX_RESPONSE_BYTES: usize = 131072;
type AnyError = Box<dyn std::error::Error + Send + Sync>;

#[derive(Serialize, Deserialize)]
struct Job {
    source_url: String,
    target_url: Option<String>,
    request_id: String,
    directory: PathBuf,
}
#[derive(Debug)]
struct Failure {
    status: u16,
    stage: &'static str,
    detail: String,
    error_class: &'static str,
}
impl Failure {
    fn new(status: u16, stage: &'static str, detail: impl Into<String>) -> Self {
        Self {
            status,
            stage,
            detail: detail.into(),
            error_class: "ServiceError",
        }
    }
    fn classified(mut self, error_class: &'static str) -> Self {
        self.error_class = error_class;
        self
    }
    fn body(&self, id: &str) -> Vec<u8> {
        serde_json::to_vec(&json!({"detail":self.detail,"stage":self.stage,"requestId":id}))
            .unwrap_or_default()
    }
}
fn busy() -> Failure {
    Failure::new(503, "queue", "Audio analysis is busy")
}

fn main() -> Result<(), AnyError> {
    if std::env::args().any(|a| a == "--worker") {
        return worker::run();
    }
    // Reap killed encoder grandchildren before removing a job's temporary files.
    // SAFETY: Linux prctl changes this process's child adoption policy only.
    if unsafe { libc::prctl(libc::PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(server::serve())
}
