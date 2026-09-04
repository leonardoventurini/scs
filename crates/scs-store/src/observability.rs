//! Bounded in-memory observability for knowledge storage operations.
//!
//! Backend labels remain stable for existing clients while TSG owns the
//! underlying SQLite and vector operations. Events are intentionally
//! process-local and bounded so observing slow queries cannot become another
//! persistence path.

use std::collections::{HashMap, VecDeque};
use std::fmt;
use std::str::FromStr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

const DEFAULT_CAPACITY: usize = 2_000;
const DEFAULT_SLOW_MS: f64 = 100.0;
const DEFAULT_CRITICAL_MS: f64 = 500.0;
const MAX_DETAIL_LEN: usize = 240;

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QueryBackend {
    Sqlite,
    Vector,
    Maintenance,
}

impl fmt::Display for QueryBackend {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let value = match self {
            Self::Sqlite => "sqlite",
            Self::Vector => "vector",
            Self::Maintenance => "maintenance",
        };
        f.write_str(value)
    }
}

impl FromStr for QueryBackend {
    type Err = ();

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "sqlite" => Ok(Self::Sqlite),
            "vector" => Ok(Self::Vector),
            "maintenance" => Ok(Self::Maintenance),
            _ => Err(()),
        }
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, serde::Serialize, serde::Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum QueryStatus {
    Ok,
    Error,
}

impl fmt::Display for QueryStatus {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        let value = match self {
            Self::Ok => "ok",
            Self::Error => "error",
        };
        f.write_str(value)
    }
}

impl FromStr for QueryStatus {
    type Err = ();

    fn from_str(value: &str) -> Result<Self, Self::Err> {
        match value {
            "ok" => Ok(Self::Ok),
            "error" => Ok(Self::Error),
            _ => Err(()),
        }
    }
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct QueryOperationEvent {
    pub id: u64,
    pub started_at_unix_ms: u128,
    pub duration_ms: f64,
    pub wait_ms: Option<f64>,
    pub backend: QueryBackend,
    pub operation: String,
    pub target: String,
    pub detail: String,
    pub status: QueryStatus,
    pub row_count: Option<u64>,
    pub vector_count: Option<u64>,
    pub error: Option<String>,
    pub slow: bool,
    pub critical: bool,
    pub thread: String,
}

#[derive(Debug, Clone)]
pub struct QueryOperationEventInput {
    pub backend: QueryBackend,
    pub operation: String,
    pub target: String,
    pub detail: String,
    pub started_at: SystemTime,
    pub duration: Duration,
    pub wait: Option<Duration>,
    pub row_count: Option<u64>,
    pub vector_count: Option<u64>,
    pub error: Option<String>,
}

#[derive(Debug, Clone, Copy)]
pub struct QueryThresholds {
    pub slow_ms: f64,
    pub critical_ms: f64,
}

impl Default for QueryThresholds {
    fn default() -> Self {
        Self {
            slow_ms: DEFAULT_SLOW_MS,
            critical_ms: DEFAULT_CRITICAL_MS,
        }
    }
}

#[derive(Debug, Clone, Default)]
pub struct QuerySnapshotFilter {
    pub limit: Option<usize>,
    pub backend: Option<QueryBackend>,
    pub min_duration_ms: Option<f64>,
    pub status: Option<QueryStatus>,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct QueryOperationSummary {
    pub backend: QueryBackend,
    pub operation: String,
    pub count: u64,
    pub errors: u64,
    pub slow_count: u64,
    pub critical_count: u64,
    pub avg_ms: f64,
    pub p95_ms: f64,
    pub max_ms: f64,
}

#[derive(Debug, Clone, serde::Serialize)]
pub struct QueryOperationsSnapshot {
    pub events: Vec<QueryOperationEvent>,
    pub summaries: Vec<QueryOperationSummary>,
    pub retained_event_count: usize,
    pub dropped_event_count: u64,
    pub capacity: usize,
}

#[derive(Debug, Default)]
pub struct QueryObservation {
    wait: Option<Duration>,
    row_count: Option<u64>,
    vector_count: Option<u64>,
}

impl QueryObservation {
    pub fn set_wait(&mut self, wait: Duration) {
        self.wait = Some(wait);
    }

    pub fn set_rows(&mut self, rows: usize) {
        self.row_count = Some(rows as u64);
    }

    pub fn set_vectors(&mut self, vectors: usize) {
        self.vector_count = Some(vectors as u64);
    }
}

#[derive(Debug)]
pub struct QueryRecorder {
    capacity: usize,
    thresholds: QueryThresholds,
    next_id: AtomicU64,
    dropped: AtomicU64,
    events: Mutex<VecDeque<QueryOperationEvent>>,
}

impl QueryRecorder {
    pub fn new(capacity: usize, thresholds: QueryThresholds) -> Self {
        Self {
            capacity: capacity.max(1),
            thresholds,
            next_id: AtomicU64::new(1),
            dropped: AtomicU64::new(0),
            events: Mutex::new(VecDeque::with_capacity(capacity.max(1))),
        }
    }

    pub fn record(&self, input: QueryOperationEventInput) {
        let elapsed_ms = duration_ms(input.duration);
        let wait_ms = input.wait.map(duration_ms);
        let status = if input.error.is_some() {
            QueryStatus::Error
        } else {
            QueryStatus::Ok
        };
        let event = QueryOperationEvent {
            id: self.next_id.fetch_add(1, Ordering::Relaxed),
            started_at_unix_ms: input
                .started_at
                .duration_since(UNIX_EPOCH)
                .map(|duration| duration.as_millis())
                .unwrap_or(0),
            duration_ms: elapsed_ms,
            wait_ms,
            backend: input.backend,
            operation: input.operation,
            target: sanitize_detail(&input.target),
            detail: sanitize_detail(&input.detail),
            status,
            row_count: input.row_count,
            vector_count: input.vector_count,
            error: input.error.map(|value| sanitize_detail(&value)),
            slow: elapsed_ms >= self.thresholds.slow_ms,
            critical: elapsed_ms >= self.thresholds.critical_ms,
            thread: thread_label(),
        };

        if event.critical {
            log::warn!(
                "critical query operation backend={} operation={} duration_ms={:.1} wait_ms={:?} detail={}",
                event.backend,
                event.operation,
                event.duration_ms,
                event.wait_ms,
                event.detail,
            );
        } else if event.slow {
            log::debug!(
                "slow query operation backend={} operation={} duration_ms={:.1} wait_ms={:?}",
                event.backend,
                event.operation,
                event.duration_ms,
                event.wait_ms,
            );
        }

        let mut events = self.events.lock().unwrap_or_else(|err| err.into_inner());
        while events.len() >= self.capacity {
            events.pop_front();
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
        events.push_back(event);
    }

    pub fn snapshot(&self, filter: QuerySnapshotFilter) -> QueryOperationsSnapshot {
        let all_events: Vec<QueryOperationEvent> = self
            .events
            .lock()
            .unwrap_or_else(|err| err.into_inner())
            .iter()
            .cloned()
            .collect();

        let retained_event_count = all_events.len();
        let filtered: Vec<QueryOperationEvent> = all_events
            .into_iter()
            .filter(|event| {
                filter
                    .backend
                    .is_none_or(|backend| event.backend == backend)
            })
            .filter(|event| filter.status.is_none_or(|status| event.status == status))
            .filter(|event| {
                filter
                    .min_duration_ms
                    .is_none_or(|min_duration_ms| event.duration_ms >= min_duration_ms)
            })
            .collect();

        let summaries = summarize_events(&filtered);
        let limit = filter.limit.unwrap_or(200).min(self.capacity);
        let events = if filtered.len() > limit {
            filtered[filtered.len() - limit..].to_vec()
        } else {
            filtered
        };

        QueryOperationsSnapshot {
            events,
            summaries,
            retained_event_count,
            dropped_event_count: self.dropped.load(Ordering::Relaxed),
            capacity: self.capacity,
        }
    }

    pub fn clear(&self) {
        self.events
            .lock()
            .unwrap_or_else(|err| err.into_inner())
            .clear();
    }
}

pub fn observe_result<T, E, F>(
    backend: QueryBackend,
    operation: &'static str,
    target: impl Into<String>,
    detail: impl Into<String>,
    f: F,
) -> Result<T, E>
where
    E: fmt::Display,
    F: FnOnce(&mut QueryObservation) -> Result<T, E>,
{
    let started_at = SystemTime::now();
    let started = Instant::now();
    let mut observation = QueryObservation::default();
    let result = f(&mut observation);
    let error = result.as_ref().err().map(|err| err.to_string());
    record(QueryOperationEventInput {
        backend,
        operation: operation.to_string(),
        target: target.into(),
        detail: detail.into(),
        started_at,
        duration: started.elapsed(),
        wait: observation.wait,
        row_count: observation.row_count,
        vector_count: observation.vector_count,
        error,
    });
    result
}

pub fn global_recorder() -> &'static QueryRecorder {
    static RECORDER: OnceLock<QueryRecorder> = OnceLock::new();
    RECORDER.get_or_init(|| QueryRecorder::new(DEFAULT_CAPACITY, QueryThresholds::default()))
}

pub fn record(input: QueryOperationEventInput) {
    global_recorder().record(input);
}

pub fn snapshot(filter: QuerySnapshotFilter) -> QueryOperationsSnapshot {
    global_recorder().snapshot(filter)
}

pub fn clear() {
    global_recorder().clear();
}

pub fn sanitize_detail(value: &str) -> String {
    let mut parts = Vec::new();
    for token in value.split_whitespace() {
        let Some((key, raw_value)) = token.split_once('=') else {
            if !looks_sensitive(token) {
                parts.push(truncate(token));
            }
            continue;
        };
        if is_sensitive_key(key) || looks_sensitive(raw_value) {
            parts.push(format!("{key}=<redacted>"));
        } else {
            parts.push(format!("{key}={}", truncate(raw_value)));
        }
    }
    truncate(&parts.join(" "))
}

fn summarize_events(events: &[QueryOperationEvent]) -> Vec<QueryOperationSummary> {
    let mut groups: HashMap<(QueryBackend, String), Vec<&QueryOperationEvent>> = HashMap::new();
    for event in events {
        groups
            .entry((event.backend, event.operation.clone()))
            .or_default()
            .push(event);
    }

    let mut summaries: Vec<QueryOperationSummary> = groups
        .into_iter()
        .map(|((backend, operation), items)| {
            let mut durations: Vec<f64> = items.iter().map(|event| event.duration_ms).collect();
            durations.sort_by(|a, b| a.partial_cmp(b).unwrap_or(std::cmp::Ordering::Equal));
            let count = durations.len() as u64;
            let total: f64 = durations.iter().sum();
            let p95_idx = ((durations.len() as f64 * 0.95).ceil() as usize)
                .saturating_sub(1)
                .min(durations.len().saturating_sub(1));

            QueryOperationSummary {
                backend,
                operation,
                count,
                errors: items
                    .iter()
                    .filter(|event| event.status == QueryStatus::Error)
                    .count() as u64,
                slow_count: items.iter().filter(|event| event.slow).count() as u64,
                critical_count: items.iter().filter(|event| event.critical).count() as u64,
                avg_ms: if count > 0 { total / count as f64 } else { 0.0 },
                p95_ms: durations.get(p95_idx).copied().unwrap_or(0.0),
                max_ms: durations.last().copied().unwrap_or(0.0),
            }
        })
        .collect();

    summaries.sort_by(|a, b| {
        b.max_ms
            .partial_cmp(&a.max_ms)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    summaries
}

fn duration_ms(duration: Duration) -> f64 {
    duration.as_secs_f64() * 1000.0
}

fn thread_label() -> String {
    std::thread::current()
        .name()
        .map(str::to_string)
        .unwrap_or_else(|| format!("{:?}", std::thread::current().id()))
}

fn is_sensitive_key(key: &str) -> bool {
    matches!(
        key,
        "path" | "repo_path" | "file_path" | "query" | "content" | "metadata" | "embedding"
    )
}

fn looks_sensitive(value: &str) -> bool {
    value.contains("/Users/")
        || value.contains("/Volumes/")
        || value.contains('\\')
        || value.starts_with('/')
        || value.len() > 96
}

fn truncate(value: &str) -> String {
    if value.len() <= MAX_DETAIL_LEN {
        value.to_string()
    } else {
        let truncated: String = value.chars().take(MAX_DETAIL_LEN).collect();
        format!("{truncated}...")
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn input(operation: impl Into<String>, duration: Duration) -> QueryOperationEventInput {
        QueryOperationEventInput {
            backend: QueryBackend::Sqlite,
            operation: operation.into(),
            target: "nodes".to_string(),
            detail: String::new(),
            started_at: SystemTime::now(),
            duration,
            wait: None,
            row_count: Some(1),
            vector_count: None,
            error: None,
        }
    }

    #[test]
    fn recorder_retains_recent_events_only() {
        let recorder = QueryRecorder::new(3, QueryThresholds::default());
        for idx in 0..5 {
            recorder.record(input(format!("op-{idx}"), Duration::from_millis(1)));
        }

        let snapshot = recorder.snapshot(QuerySnapshotFilter::default());
        assert_eq!(snapshot.events.len(), 3);
        assert_eq!(snapshot.events[0].operation, "op-2");
        assert_eq!(snapshot.events[2].operation, "op-4");
        assert_eq!(snapshot.dropped_event_count, 2);
    }

    #[test]
    fn recorder_marks_slow_and_critical_events() {
        let recorder = QueryRecorder::new(
            10,
            QueryThresholds {
                slow_ms: 100.0,
                critical_ms: 500.0,
            },
        );

        recorder.record(QueryOperationEventInput {
            backend: QueryBackend::Vector,
            operation: "VectorIndex::search".to_string(),
            target: "index.usearch".to_string(),
            detail: "limit=20".to_string(),
            started_at: SystemTime::now(),
            duration: Duration::from_millis(750),
            wait: Some(Duration::from_millis(12)),
            row_count: None,
            vector_count: Some(20),
            error: None,
        });

        let event = recorder
            .snapshot(QuerySnapshotFilter::default())
            .events
            .remove(0);
        assert!(event.slow);
        assert!(event.critical);
        assert_eq!(event.wait_ms, Some(12.0));
    }

    #[test]
    fn recorder_filters_and_summarizes_events() {
        let recorder = QueryRecorder::new(10, QueryThresholds::default());
        recorder.record(input("fast", Duration::from_millis(5)));
        recorder.record(input("slow", Duration::from_millis(150)));
        recorder.record(QueryOperationEventInput {
            error: Some("boom".to_string()),
            ..input("slow", Duration::from_millis(250))
        });

        let snapshot = recorder.snapshot(QuerySnapshotFilter {
            min_duration_ms: Some(100.0),
            ..QuerySnapshotFilter::default()
        });

        assert_eq!(snapshot.events.len(), 2);
        let slow_summary = snapshot
            .summaries
            .iter()
            .find(|summary| summary.operation == "slow")
            .unwrap();
        assert_eq!(slow_summary.count, 2);
        assert_eq!(slow_summary.errors, 1);
        assert_eq!(slow_summary.slow_count, 2);
        assert_eq!(slow_summary.max_ms, 250.0);
    }

    #[test]
    fn detail_sanitizer_redacts_paths_and_long_values() {
        let sanitized =
            sanitize_detail("repo_path=/Users/alice/private/project query=secret limit=20");
        assert!(!sanitized.contains("/Users/alice"));
        assert!(!sanitized.contains("secret"));
        assert!(sanitized.contains("limit=20"));
    }

    #[test]
    fn recorder_handles_many_events_without_unbounded_growth() {
        let recorder = QueryRecorder::new(2_000, QueryThresholds::default());
        for idx in 0..100_000 {
            recorder.record(QueryOperationEventInput {
                detail: idx.to_string(),
                ..input("stress", Duration::from_micros(50))
            });
        }
        let snapshot = recorder.snapshot(QuerySnapshotFilter::default());
        assert_eq!(snapshot.retained_event_count, 2_000);
        assert_eq!(snapshot.capacity, 2_000);
    }

    #[test]
    fn clear_preserves_monotonic_event_ids() {
        let recorder = QueryRecorder::new(10, QueryThresholds::default());
        recorder.record(input("before", Duration::from_millis(1)));
        let first = recorder.snapshot(QuerySnapshotFilter::default()).events[0].id;

        recorder.clear();
        recorder.record(input("after", Duration::from_millis(1)));
        let second = recorder.snapshot(QuerySnapshotFilter::default()).events[0].id;

        assert!(second > first);
    }
}
