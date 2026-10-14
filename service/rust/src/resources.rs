//! Opt-in preview measurements. No URLs, audio, or credentials enter these logs.
use serde_json::json;
use std::{
    collections::HashSet,
    fs,
    io::Write,
    path::Path,
    time::{Duration, SystemTime, UNIX_EPOCH},
};

fn rss_bytes(status: &str) -> Option<u64> {
    status.lines().find_map(|line| {
        line.strip_prefix("VmRSS:")?
            .split_whitespace()
            .next()?
            .parse::<u64>()
            .ok()?
            .checked_mul(1024)
    })
}

fn process_tree_rss() -> Option<u64> {
    let mut pending = vec![std::process::id()];
    let mut seen = HashSet::new();
    let mut total = 0_u64;
    while let Some(pid) = pending.pop() {
        if !seen.insert(pid) {
            continue;
        }
        let path = format!("/proc/{pid}");
        if let Ok(status) = fs::read_to_string(format!("{path}/status")) {
            total = total.checked_add(rss_bytes(&status)?)?;
        }
        // Tokio can spawn children from any thread, not just the group leader.
        if let Ok(tasks) = fs::read_dir(format!("{path}/task")) {
            for task in tasks.flatten() {
                if let Ok(children) = fs::read_to_string(task.path().join("children")) {
                    pending.extend(
                        children
                            .split_whitespace()
                            .filter_map(|p| p.parse::<u32>().ok()),
                    );
                }
            }
        }
    }
    (total > 0).then_some(total)
}

fn counter(path: &Path) -> Option<u64> {
    fs::read_to_string(path).ok()?.trim().parse().ok()
}

fn cgroup_root() -> Option<std::path::PathBuf> {
    let membership = fs::read_to_string("/proc/self/cgroup").ok()?;
    let relative = membership
        .lines()
        .find_map(|line| line.strip_prefix("0::"))?;
    // Do not fall back to an ancestor cgroup and label its usage as this service.
    let root = Path::new("/sys/fs/cgroup").join(relative.trim_start_matches('/'));
    root.join("memory.current").is_file().then_some(root)
}

pub(crate) fn start() -> Option<tokio::task::JoinHandle<()>> {
    if std::env::var("AUDIO_ANALYSIS_RESOURCE_PROFILE").as_deref() != Ok("1") {
        return None;
    }
    Some(tokio::spawn(async {
        let started = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap_or_default();
        let instance = format!("{}-{}", started.as_nanos(), std::process::id());
        let cgroup = cgroup_root();
        let mut clock = tokio::time::interval(Duration::from_millis(50));
        clock.set_missed_tick_behavior(tokio::time::MissedTickBehavior::Skip);
        let mut peak = None;
        let mut count = 0;
        let mut window_started = 0;
        loop {
            clock.tick().await;
            let sampled_at = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap_or_default()
                .as_millis();
            if count == 0 {
                window_started = sampled_at;
            }
            let current = process_tree_rss();
            peak = peak.max(current);
            count += 1;
            if count != 20 {
                continue;
            }
            let line = json!({
                "service": "audio-analysis", "event": "resource-sample",
                "instanceId": instance,
                "sampledAtUnixMs": sampled_at, "windowStartedAtUnixMs": window_started,
                "sampleIntervalMs": 50, "windowSamples": count,
                "processTreeRssBytes": current, "processTreePeakRssBytes": peak,
                "cgroupMemoryCurrentBytes": cgroup.as_ref().and_then(|p| counter(&p.join("memory.current"))),
                "cgroupMemoryPeakBytes": cgroup.as_ref().and_then(|p| counter(&p.join("memory.peak"))),
            });
            let mut output = std::io::stdout().lock();
            let _ = writeln!(output, "{line}");
            let _ = output.flush();
            peak = None;
            count = 0;
        }
    }))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn rss_uses_resident_pages_and_preserves_unavailable_values() {
        assert_eq!(
            rss_bytes("VmSize:\t2000 kB\nVmRSS:\t1024 kB\n"),
            Some(1048576)
        );
        assert_eq!(rss_bytes("VmSize:\t2000 kB\n"), None);
    }
}
