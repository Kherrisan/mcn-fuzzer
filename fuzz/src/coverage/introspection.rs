//! In-memory coverage-analysis introspection and its read-only HTTP MCP service.
//!
//! The fuzzer thread owns the proposal index. Tokio only shares
//! [`IntrospectionStore`]. Counts live on each proposal record and are updated
//! in place; nothing here persists or restores history.

use std::{
    cell::RefCell,
    collections::{BTreeMap, BTreeSet},
    net::TcpListener,
    os::fd::AsRawFd,
    path::{Path, PathBuf},
    rc::Rc,
    sync::{Arc, Mutex, RwLock},
    time::{SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};
use serde_json::{json, Value};

use crate::{
    coverage::{
        proposal::Proposal,
        symbol_map::{load_index_map, Frame},
    },
    runtime::report::CaseReport,
};

const DEFAULT_LIST_LIMIT: usize = 50;
const MAX_LIST_LIMIT: usize = 200;
const DEFAULT_INDEX_LIMIT: usize = 256;
const MAX_INDEX_LIMIT: usize = 1024;
const MAX_ERROR_BYTES: usize = 4096;

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd, Serialize, Deserialize)]
pub struct ProposalId(String);

impl ProposalId {
    pub fn coverage(request_id: &str) -> Self {
        Self(format!("coverage:{request_id}"))
    }

    pub fn synthesis(request_id: &str) -> Self {
        Self(format!("synthesis:{request_id}"))
    }

    pub fn as_str(&self) -> &str {
        &self.0
    }
}

impl std::fmt::Display for ProposalId {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        self.0.fmt(formatter)
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Ord, PartialOrd)]
struct GeneratorKey(PathBuf);

#[derive(Clone, Debug)]
pub struct CampaignIdentity {
    pub run_id: String,
    pub started_at_unix_ms: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, Serialize, Deserialize, schemars::JsonSchema)]
#[serde(rename_all = "snake_case")]
pub enum ProposalOrigin {
    Coverage,
    AutonomousSynthesis,
}

#[derive(
    Clone, Copy, Debug, Eq, PartialEq, Serialize, serde::Deserialize, schemars::JsonSchema,
)]
#[serde(rename_all = "snake_case")]
pub enum ProposalStatus {
    Proposing,
    Queued,
    Skipped,
    Synthesizing,
    Synthesized,
    Failed,
    Cancelled,
}

impl ProposalStatus {
    fn is_terminal(self) -> bool {
        matches!(
            self,
            Self::Skipped | Self::Synthesized | Self::Failed | Self::Cancelled
        )
    }

    fn as_str(self) -> &'static str {
        match self {
            Self::Proposing => "proposing",
            Self::Queued => "queued",
            Self::Skipped => "skipped",
            Self::Synthesizing => "synthesizing",
            Self::Synthesized => "synthesized",
            Self::Failed => "failed",
            Self::Cancelled => "cancelled",
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct ProposalIdentity {
    pub id: ProposalId,
    pub origin: ProposalOrigin,
    pub analysis_request_id: Option<String>,
    pub session: String,
    pub snapshot: Option<String>,
    pub created_at_unix_ms: u64,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct GeneratorArtifacts {
    pub generator_py: PathBuf,
    pub params_json: PathBuf,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct MutationStats {
    attempts: u64,
    changed_candidates: u64,
    skipped_or_unchanged: u64,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct ExecutionStats {
    started: u64,
    completed: u64,
    valid: u64,
    invalid: u64,
    infrastructure_errors: u64,
    target_outcomes: BTreeMap<String, u64>,
    generator_statuses: BTreeMap<String, u64>,
    generator_exits: BTreeMap<String, u64>,
    last_error: Option<ExecutionError>,
}

#[derive(Clone, Serialize, Deserialize)]
struct ExecutionError {
    at_unix_ms: u64,
    category: String,
    message: String,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct ContributionStats {
    corpus_cases: BTreeSet<u64>,
    feedback_novelty_indices: BTreeSet<u32>,
}

#[derive(Clone, Default, Serialize, Deserialize)]
struct ProposalStats {
    mutations: MutationStats,
    executions: ExecutionStats,
    contribution: ContributionStats,
}

struct ProposalData {
    status: ProposalStatus,
    proposal: Option<Arc<Proposal>>,
    synthesis_request_id: Option<String>,
    artifacts: Option<GeneratorArtifacts>,
    stats: ProposalStats,
}

#[derive(Serialize, Deserialize)]
pub(crate) struct ProposalSnapshot {
    pub(crate) identity: ProposalIdentity,
    pub(crate) status: ProposalStatus,
    pub(crate) proposal: Option<Proposal>,
    pub(crate) synthesis_request_id: Option<String>,
    pub(crate) artifacts: Option<GeneratorArtifacts>,
    #[serde(default)]
    stats: ProposalStats,
}

impl ProposalSnapshot {
    pub(crate) fn legacy(identity: ProposalIdentity, status: ProposalStatus,
                         proposal: Proposal) -> Self {
        Self { identity, status, proposal: Some(proposal), synthesis_request_id: None,
               artifacts: None, stats: ProposalStats::default() }
    }
}

pub struct ProposalRecord {
    identity: ProposalIdentity,
    data: RwLock<ProposalData>,
}

enum SymbolLookup {
    Available(Arc<BTreeMap<u32, Frame>>),
    Unavailable { reason: String },
}

pub struct IntrospectionStore {
    campaign: CampaignIdentity,
    proposals: RwLock<Vec<Arc<ProposalRecord>>>,
    symbols: SymbolLookup,
}

pub struct RegistryWriter {
    by_id: BTreeMap<ProposalId, Arc<ProposalRecord>>,
    generator_owners: BTreeMap<GeneratorKey, ProposalId>,
    shared: Arc<IntrospectionStore>,
}

/// Fuzzer-thread owner of the proposal index. `Rc` keeps it off other threads.
#[derive(Clone)]
pub struct IntrospectionHandle {
    writer: Rc<RefCell<RegistryWriter>>,
}

struct HostListener {
    listener: TcpListener,
}

static HOST_LISTENER: Mutex<Option<HostListener>> = Mutex::new(None);

/// Bind `127.0.0.1:0` in the network namespace that still contains the HC core.
///
/// PID 1 calls this after the private core starts and before `CLONE_NEWNET`.
/// The socket stays with that namespace; the later Tokio thread adopts the fd.
pub fn bind_host_listener() -> Result<(), libafl::Error> {
    let listener = TcpListener::bind((std::net::Ipv4Addr::LOCALHOST, 0)).map_err(|error| {
        libafl::Error::unknown(format!(
            "binding the campaign introspection listener on 127.0.0.1:0: {error}"
        ))
    })?;
    listener.set_nonblocking(true).map_err(|error| {
        libafl::Error::unknown(format!(
            "setting the introspection listener nonblocking: {error}"
        ))
    })?;
    confirm_cloexec(listener.as_raw_fd()).map_err(libafl::Error::unknown)?;
    let mut slot = HOST_LISTENER
        .lock()
        .unwrap_or_else(|error| error.into_inner());
    if slot.is_some() {
        return Err(libafl::Error::unknown(
            "campaign introspection listener is already bound".to_string(),
        ));
    }
    *slot = Some(HostListener { listener });
    Ok(())
}

/// Take the listener prepared before network isolation. `None` when analysis is off.
pub fn take_host_listener() -> Option<TcpListener> {
    HOST_LISTENER
        .lock()
        .unwrap_or_else(|error| error.into_inner())
        .take()
        .map(|prepared| prepared.listener)
}

fn confirm_cloexec(fd: i32) -> Result<(), String> {
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 {
        return Err(format!(
            "F_GETFD on the introspection listener failed: {}",
            std::io::Error::last_os_error()
        ));
    }
    if flags & libc::FD_CLOEXEC != 0 {
        return Ok(());
    }
    let rc = unsafe { libc::fcntl(fd, libc::F_SETFD, flags | libc::FD_CLOEXEC) };
    if rc < 0 {
        return Err(format!(
            "F_SETFD FD_CLOEXEC on the introspection listener failed: {}",
            std::io::Error::last_os_error()
        ));
    }
    let flags = unsafe { libc::fcntl(fd, libc::F_GETFD) };
    if flags < 0 || flags & libc::FD_CLOEXEC == 0 {
        return Err("introspection listener is not FD_CLOEXEC".to_string());
    }
    Ok(())
}

impl IntrospectionHandle {
    pub fn open(campaign: CampaignIdentity, index_map: Option<&Path>) -> Self {
        Self {
            writer: Rc::new(RefCell::new(RegistryWriter::open(campaign, index_map))),
        }
    }

    pub fn with<T>(&self, body: impl FnOnce(&RegistryWriter) -> T) -> T {
        body(&self.writer.borrow())
    }

    pub fn with_mut<T>(&self, body: impl FnOnce(&mut RegistryWriter) -> T) -> T {
        body(&mut self.writer.borrow_mut())
    }

    pub fn store(&self) -> Arc<IntrospectionStore> {
        self.with(RegistryWriter::store)
    }

    pub(crate) fn snapshot(&self) -> Result<Vec<ProposalSnapshot>, String> {
        self.with(|writer| {
            let records = writer.shared.proposals.read().map_err(|_| "proposal list poisoned")?;
            records.iter().map(|record| {
                let data = record.read_data()?;
                Ok(ProposalSnapshot {
                    identity: record.identity.clone(),
                    status: data.status,
                    proposal: data.proposal.as_deref().cloned(),
                    synthesis_request_id: data.synthesis_request_id.clone(),
                    artifacts: data.artifacts.clone(),
                    stats: data.stats.clone(),
                })
            }).collect()
        })
    }

    pub(crate) fn restore(&self, snapshots: Vec<ProposalSnapshot>) -> Result<(), String> {
        self.with_mut(|writer| {
            for snapshot in snapshots {
                let record = writer.register(snapshot.identity)?;
                *record.data.write().map_err(|_| "proposal record poisoned")? = ProposalData {
                    status: snapshot.status,
                    proposal: snapshot.proposal.map(Arc::new),
                    synthesis_request_id: snapshot.synthesis_request_id,
                    artifacts: snapshot.artifacts,
                    stats: snapshot.stats,
                };
            }
            Ok(())
        })
    }

    pub fn register_coverage(
        &self,
        request_id: &str,
        session: &str,
        snapshot: &str,
    ) -> Result<(), String> {
        self.with_mut(|writer| {
            writer.register(ProposalIdentity {
                id: ProposalId::coverage(request_id),
                origin: ProposalOrigin::Coverage,
                analysis_request_id: Some(request_id.to_string()),
                session: session.to_string(),
                snapshot: Some(snapshot.to_string()),
                created_at_unix_ms: now_ms(),
            })?;
            Ok(())
        })
    }

    pub fn register_autonomous(&self, request_id: &str, session: &str) -> Result<(), String> {
        self.with_mut(|writer| {
            writer.register(ProposalIdentity {
                id: ProposalId::synthesis(request_id),
                origin: ProposalOrigin::AutonomousSynthesis,
                analysis_request_id: None,
                session: session.to_string(),
                snapshot: None,
                created_at_unix_ms: now_ms(),
            })
        })?;
        let record = self
            .with(|writer| writer.get(&ProposalId::synthesis(request_id)))
            .ok_or_else(|| format!("proposal synthesis:{request_id} is not registered"))?;
        record.synthesis_started(request_id.to_string())
    }

    pub fn accept(&self, id: &ProposalId, proposal: Arc<Proposal>) -> Result<(), String> {
        let record = self
            .with(|writer| writer.get(id))
            .ok_or_else(|| format!("unknown proposal {id}"))?;
        record.accept_proposal(proposal)
    }

    pub fn mark_skipped(&self, id: &ProposalId) -> Result<(), String> {
        let record = self
            .with(|writer| writer.get(id))
            .ok_or_else(|| format!("unknown proposal {id}"))?;
        record.set_status(ProposalStatus::Skipped)
    }

    /// Mark the coverage or synthesis proposal for this request failed.
    /// Unknown requests are left alone. Terminal proposals return an error.
    pub fn mark_failed(&self, request_id: &str) -> Result<(), String> {
        let id = {
            let writer = self.writer.borrow();
            if writer.get(&ProposalId::coverage(request_id)).is_some() {
                ProposalId::coverage(request_id)
            } else if writer.get(&ProposalId::synthesis(request_id)).is_some() {
                ProposalId::synthesis(request_id)
            } else {
                return Ok(());
            }
        };
        let record = self
            .with(|writer| writer.get(&id))
            .ok_or_else(|| format!("unknown proposal {id}"))?;
        record.set_status(ProposalStatus::Failed)
    }

    pub fn synthesis_started(&self, request_id: &str) -> Result<(), String> {
        let id = ProposalId::coverage(request_id);
        let record = self
            .with(|writer| writer.get(&id))
            .ok_or_else(|| format!("unknown proposal {id}"))?;
        record.synthesis_started(request_id.to_string())
    }

    /// Bind `artifacts.generator_py` before publishing. A path owned by another
    /// proposal is returned as an error and does not change status.
    pub fn publish(
        &self,
        request_id: &str,
        artifacts: GeneratorArtifacts,
        proposal: Option<Arc<Proposal>>,
    ) -> Result<(), String> {
        let id = self.with(|writer| {
            if writer.get(&ProposalId::coverage(request_id)).is_some() {
                Some(ProposalId::coverage(request_id))
            } else if writer.get(&ProposalId::synthesis(request_id)).is_some() {
                Some(ProposalId::synthesis(request_id))
            } else {
                None
            }
        });
        let Some(id) = id else {
            return Err(format!(
                "proposal for request {request_id} is not registered"
            ));
        };
        self.with_mut(|writer| writer.bind_generator(&artifacts.generator_py, id.clone()))?;
        let record = self
            .with(|writer| writer.get(&id))
            .ok_or_else(|| format!("unknown proposal {id}"))?;
        if let Some(proposal) = proposal {
            record.accept_proposal(proposal)?;
        }
        record.publish_generator(artifacts)
    }

    pub fn cancel_incomplete(&self) {
        self.with_mut(|writer| writer.cancel_incomplete());
    }

    pub fn owner(&self, path: &Path) -> Option<Arc<ProposalRecord>> {
        self.with(|writer| writer.owner_for_generator(path))
    }
}

pub(crate) fn resolve_owner(
    handle: &IntrospectionHandle,
    origin_request: Option<&str>,
    generator: &Path,
) -> Result<Option<Arc<ProposalRecord>>, String> {
    let writer = handle.writer.borrow();
    let by_origin = origin_request.and_then(|request| {
        writer
            .get(&ProposalId::coverage(request))
            .or_else(|| writer.get(&ProposalId::synthesis(request)))
    });
    let by_path = writer.owner_for_generator(generator);
    match (by_origin, by_path) {
        (Some(origin_record), Some(path_record)) if !Arc::ptr_eq(&origin_record, &path_record) => {
            Err(format!(
                "generator {} is bound to {}, which conflicts with TraceOrigin {}",
                generator.display(),
                path_record.id(),
                origin_record.id()
            ))
        }
        (Some(record), _) | (None, Some(record)) => Ok(Some(record)),
        (None, None) => Ok(None),
    }
}

pub(crate) fn note_mutation(
    handle: &IntrospectionHandle,
    origin_request: Option<&str>,
    generator: &Path,
    changed: bool,
) -> Result<(), String> {
    let Some(record) = resolve_owner(handle, origin_request, generator)? else {
        return Ok(());
    };
    record.record_mutation(changed);
    Ok(())
}

/// `corpus_id == None` is a filtered input or a solution. Neither adds a corpus case.
pub(crate) fn note_evaluated_corpus(
    handle: &IntrospectionHandle,
    origin_request: Option<&str>,
    generator: &Path,
    corpus_id: Option<u64>,
    novelties: Option<&[usize]>,
) -> Result<(), String> {
    let Some(corpus_id) = corpus_id else {
        return Ok(());
    };
    let Some(record) = resolve_owner(handle, origin_request, generator)? else {
        return Ok(());
    };
    record.corpus_added(corpus_id, novelties.unwrap_or(&[]));
    Ok(())
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|value| value.as_millis() as u64)
        .unwrap_or(0)
}

fn trim_message(text: &str) -> String {
    let mut owned = text.to_string();
    if owned.len() > MAX_ERROR_BYTES {
        owned.truncate(MAX_ERROR_BYTES);
    }
    owned
}

impl RegistryWriter {
    pub fn open(campaign: CampaignIdentity, index_map: Option<&Path>) -> Self {
        let symbols = match index_map {
            Some(path) => match load_index_map(path) {
                Ok(map) => SymbolLookup::Available(Arc::new(map)),
                Err(reason) => SymbolLookup::Unavailable { reason },
            },
            None => SymbolLookup::Unavailable {
                reason: "coverage.index_map is not configured".to_string(),
            },
        };
        let shared = Arc::new(IntrospectionStore {
            campaign,
            proposals: RwLock::new(Vec::new()),
            symbols,
        });
        Self {
            by_id: BTreeMap::new(),
            generator_owners: BTreeMap::new(),
            shared,
        }
    }

    pub fn store(&self) -> Arc<IntrospectionStore> {
        Arc::clone(&self.shared)
    }

    pub fn register(&mut self, identity: ProposalIdentity) -> Result<Arc<ProposalRecord>, String> {
        if self.by_id.contains_key(&identity.id) {
            return Err(format!("proposal {} is already registered", identity.id));
        }
        let status = match identity.origin {
            ProposalOrigin::Coverage => ProposalStatus::Proposing,
            ProposalOrigin::AutonomousSynthesis => ProposalStatus::Synthesizing,
        };
        let record = Arc::new(ProposalRecord {
            identity,
            data: RwLock::new(ProposalData {
                status,
                proposal: None,
                synthesis_request_id: None,
                artifacts: None,
                stats: ProposalStats::default(),
            }),
        });
        self.by_id
            .insert(record.identity.id.clone(), Arc::clone(&record));
        self.shared
            .proposals
            .write()
            .map_err(|_| "introspection list lock poisoned".to_string())?
            .push(Arc::clone(&record));
        Ok(record)
    }

    pub fn get(&self, id: &ProposalId) -> Option<Arc<ProposalRecord>> {
        self.by_id.get(id).cloned()
    }

    pub fn bind_generator(&mut self, path: &Path, id: ProposalId) -> Result<(), String> {
        let key = GeneratorKey(canonicalize_generator(path));
        if let Some(existing) = self.generator_owners.get(&key) {
            if existing != &id {
                return Err(format!(
                    "generator {} is already bound to {existing}, not {id}",
                    key.0.display()
                ));
            }
            return Ok(());
        }
        if !self.by_id.contains_key(&id) {
            return Err(format!("proposal {id} is not registered"));
        }
        self.generator_owners.insert(key, id);
        Ok(())
    }

    /// `path` must already be the canonical key stored by [`Self::bind_generator`].
    pub fn owner_for_generator(&self, path: &Path) -> Option<Arc<ProposalRecord>> {
        self.generator_owners
            .get(&GeneratorKey(path.to_path_buf()))
            .and_then(|id| self.by_id.get(id).cloned())
    }

    pub fn cancel_incomplete(&self) {
        for record in self.by_id.values() {
            let _ = record.set_status(ProposalStatus::Cancelled);
        }
    }
}

fn canonicalize_generator(path: &Path) -> PathBuf {
    std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf())
}

impl ProposalRecord {
    pub fn id(&self) -> &ProposalId {
        &self.identity.id
    }

    fn write_data(&self) -> Result<std::sync::RwLockWriteGuard<'_, ProposalData>, String> {
        self.data
            .write()
            .map_err(|_| "introspection record lock poisoned".to_string())
    }

    fn read_data(&self) -> Result<std::sync::RwLockReadGuard<'_, ProposalData>, String> {
        self.data
            .read()
            .map_err(|_| "introspection record lock poisoned".to_string())
    }

    pub fn accept_proposal(&self, proposal: Arc<Proposal>) -> Result<(), String> {
        let mut data = self.write_data()?;
        if data.status.is_terminal() {
            return Err(format!(
                "proposal {} is already {}",
                self.identity.id,
                data.status.as_str()
            ));
        }
        data.proposal = Some(proposal);
        if data.status == ProposalStatus::Proposing {
            data.status = ProposalStatus::Queued;
        }
        Ok(())
    }

    pub fn set_status(&self, status: ProposalStatus) -> Result<(), String> {
        let mut data = self.write_data()?;
        if data.status.is_terminal() {
            return Err(format!(
                "proposal {} is already {}",
                self.identity.id,
                data.status.as_str()
            ));
        }
        data.status = status;
        Ok(())
    }

    pub fn synthesis_started(&self, request_id: String) -> Result<(), String> {
        let mut data = self.write_data()?;
        if data.status.is_terminal() {
            return Err(format!(
                "proposal {} is already {}",
                self.identity.id,
                data.status.as_str()
            ));
        }
        data.synthesis_request_id = Some(request_id);
        data.status = ProposalStatus::Synthesizing;
        Ok(())
    }

    pub fn publish_generator(&self, artifacts: GeneratorArtifacts) -> Result<(), String> {
        let mut data = self.write_data()?;
        if data.status.is_terminal() {
            return Err(format!(
                "proposal {} is already {}",
                self.identity.id,
                data.status.as_str()
            ));
        }
        data.artifacts = Some(artifacts);
        data.status = ProposalStatus::Synthesized;
        Ok(())
    }

    pub fn record_mutation(&self, changed: bool) {
        if let Ok(mut data) = self.write_data() {
            data.stats.mutations.attempts += 1;
            if changed {
                data.stats.mutations.changed_candidates += 1;
            } else {
                data.stats.mutations.skipped_or_unchanged += 1;
            }
        }
    }

    pub fn execution_started(&self) {
        if let Ok(mut data) = self.write_data() {
            data.stats.executions.started += 1;
            data.stats.executions.last_error = None;
        }
    }

    pub fn execution_finished(&self, report: &CaseReport) {
        if let Ok(mut data) = self.write_data() {
            data.stats.executions.completed += 1;
            if report.valid {
                data.stats.executions.valid += 1;
            } else {
                data.stats.executions.invalid += 1;
            }
            *data
                .stats
                .executions
                .target_outcomes
                .entry(report.target_status.clone())
                .or_default() += 1;
            *data
                .stats
                .executions
                .generator_statuses
                .entry(report.generator_status.clone())
                .or_default() += 1;
            *data
                .stats
                .executions
                .generator_exits
                .entry(report.generator_exit.clone())
                .or_default() += 1;
        }
    }

    pub fn execution_failed(&self, category: &str, message: &str) {
        if let Ok(mut data) = self.write_data() {
            data.stats.executions.infrastructure_errors += 1;
            data.stats.executions.last_error = Some(ExecutionError {
                at_unix_ms: now_ms(),
                category: category.to_string(),
                message: trim_message(message),
            });
        }
    }

    pub fn corpus_added(&self, corpus_id: u64, novelty_indices: &[usize]) {
        let Ok(mut data) = self.write_data() else {
            return;
        };
        if !data.stats.contribution.corpus_cases.insert(corpus_id) {
            return;
        }
        for index in novelty_indices {
            if *index == 0 {
                continue;
            }
            let Ok(index) = u32::try_from(*index) else {
                continue;
            };
            data.stats
                .contribution
                .feedback_novelty_indices
                .insert(index);
        }
    }
}

#[derive(Clone, Copy)]
struct PageRequest {
    offset: usize,
    limit: usize,
}

fn page_limit(limit: Option<usize>, default: usize, max: usize) -> Result<usize, String> {
    let limit = limit.unwrap_or(default);
    if limit == 0 || limit > max {
        return Err(format!("limit must be from 1 to {max}"));
    }
    Ok(limit)
}

fn page_slice<T: Clone>(items: &[T], request: PageRequest) -> (Vec<T>, Option<usize>) {
    if request.offset >= items.len() {
        return (Vec::new(), None);
    }
    let end = (request.offset + request.limit).min(items.len());
    let next = if end < items.len() { Some(end) } else { None };
    (items[request.offset..end].to_vec(), next)
}

impl IntrospectionStore {
    pub fn list_proposals(
        &self,
        statuses: Option<&[ProposalStatus]>,
        offset: Option<usize>,
        limit: Option<usize>,
    ) -> Result<Value, String> {
        let limit = page_limit(limit, DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT)?;
        let offset = offset.unwrap_or(0);
        let records = {
            let guard = self
                .proposals
                .read()
                .map_err(|_| "introspection list lock poisoned".to_string())?;
            guard.iter().cloned().collect::<Vec<_>>()
        };
        let mut filtered = Vec::new();
        for record in records {
            let include = match statuses {
                Some(statuses) if !statuses.is_empty() => {
                    statuses.contains(&record.read_data()?.status)
                }
                _ => true,
            };
            if include {
                filtered.push(record);
            }
        }
        let total = filtered.len();
        let (page, next_offset) = page_slice(&filtered, PageRequest { offset, limit });
        let items: Vec<Value> = page
            .iter()
            .map(|record| record.summary())
            .collect::<Result<_, _>>()?;
        Ok(self.response(json!({
            "total": total,
            "offset": offset,
            "limit": limit,
            "items": items,
            "next_offset": next_offset,
        })))
    }

    pub fn get_proposal(
        &self,
        id: &ProposalId,
        index_offset: Option<usize>,
        index_limit: Option<usize>,
    ) -> Result<Value, String> {
        self.get_proposal_view(id, index_offset, index_limit, false)
    }

    fn get_proposal_view(
        &self,
        id: &ProposalId,
        index_offset: Option<usize>,
        index_limit: Option<usize>,
        summary_only: bool,
    ) -> Result<Value, String> {
        let limit = page_limit(index_limit, DEFAULT_INDEX_LIMIT, MAX_INDEX_LIMIT)?;
        let offset = index_offset.unwrap_or(0);
        let records = self
            .proposals
            .read()
            .map_err(|_| "introspection list lock poisoned".to_string())?;
        let record = records
            .iter()
            .find(|record| &record.identity.id == id)
            .cloned()
            .ok_or_else(|| format!("unknown proposal {id}"))?;
        drop(records);
        Ok(self.response(record.detail(offset, limit, summary_only)?))
    }

    pub fn resolve_indices(&self, indices: &[u32]) -> Result<Value, String> {
        if indices.is_empty() || indices.len() > MAX_INDEX_LIMIT {
            return Err(format!(
                "indices must contain from 1 to {MAX_INDEX_LIMIT} items"
            ));
        }
        let items: Vec<Value> = indices
            .iter()
            .map(|index| self.resolve_one(*index))
            .collect();
        Ok(self.response(json!({ "items": items })))
    }

    fn resolve_one(&self, index: u32) -> Value {
        let SymbolLookup::Available(map) = &self.symbols else {
            let SymbolLookup::Unavailable { reason } = &self.symbols else {
                unreachable!()
            };
            return json!({
                "index": index,
                "status": "mapping_unavailable",
                "file": null,
                "line": null,
                "column": null,
                "function": null,
                "reason": reason,
            });
        };
        let Some(frame) = map.get(&index) else {
            return json!({
                "index": index,
                "status": "unknown_index",
                "file": null,
                "line": null,
                "column": null,
                "function": null,
            });
        };
        if frame.file.is_empty() || frame.line == 0 {
            return json!({
                "index": index,
                "status": "no_debug_location",
                "file": null,
                "line": null,
                "column": null,
                "function": frame.function,
            });
        }
        json!({
            "index": index,
            "status": "resolved",
            "file": frame.file,
            "line": frame.line,
            "column": null,
            "function": frame.function,
        })
    }

    fn response(&self, data: Value) -> Value {
        json!({
            "run_id": self.campaign.run_id,
            "observed_at_unix_ms": now_ms(),
            "data": data,
        })
    }
}

impl ProposalRecord {
    fn summary(&self) -> Result<Value, String> {
        let snap = {
            let data = self.read_data()?;
            (
                data.status,
                data.proposal.as_ref().map(|proposal| {
                    (
                        proposal.goal.handler_ids.clone(),
                        proposal.goal.reason.clone(),
                    )
                }),
                data.artifacts
                    .as_ref()
                    .map(|item| item.generator_py.clone()),
                data.stats.mutations.changed_candidates,
                data.stats.mutations.attempts,
                data.stats.executions.completed,
                data.stats.contribution.corpus_cases.len(),
                data.stats.contribution.feedback_novelty_indices.len(),
            )
        };
        let (
            status,
            goal,
            generator_py,
            mutations,
            mutation_attempts,
            executions,
            corpus_cases,
            novelty_indices,
        ) = snap;
        Ok(json!({
            "proposal_id": self.identity.id,
            "origin": self.identity.origin,
            "status": status,
            "analysis_request_id": self.identity.analysis_request_id,
            "session": self.identity.session,
            "snapshot": self.identity.snapshot,
            "goal": goal.map(|(handler_ids, reason)| json!({
                "handler_ids": handler_ids,
                "reason": reason,
            })),
            "generator_py": generator_py,
            "mutations": mutations,
            "mutation_attempts": mutation_attempts,
            "executions": executions,
            "corpus_cases": corpus_cases,
            "novelty_indices": novelty_indices,
        }))
    }

    fn detail(&self, offset: usize, limit: usize, summary_only: bool) -> Result<Value, String> {
        let snap = {
            let data = self.read_data()?;
            let indices: Vec<u32> = data
                .stats
                .contribution
                .feedback_novelty_indices
                .iter()
                .copied()
                .collect();
            (
                data.status,
                data.proposal
                    .as_ref()
                    .map(|proposal| {
                        if summary_only {
                            Ok(json!({
                                "handler_ids": proposal.goal.handler_ids,
                                "reason": proposal.goal.reason,
                            }))
                        } else {
                            serde_json::to_value(proposal)
                        }
                    })
                    .transpose()
                    .map_err(|error| format!("serializing proposal: {error}"))?,
                data.synthesis_request_id.clone(),
                data.artifacts.clone(),
                data.stats.clone(),
                indices,
            )
        };
        let (status, proposal_value, synthesis_request_id, artifacts, stats, indices) = snap;
        let (page, next_offset) = page_slice(&indices, PageRequest { offset, limit });
        let mut result = json!({
            "proposal_id": self.identity.id,
            "origin": self.identity.origin,
            "status": status,
            "analysis_request_id": self.identity.analysis_request_id,
            "session": self.identity.session,
            "snapshot": self.identity.snapshot,
            "created_at_unix_ms": self.identity.created_at_unix_ms,
            "synthesis_request_id": synthesis_request_id,
            "artifacts": artifacts.as_ref().map(|item| json!({
                "generator_py": item.generator_py,
                "params_json": item.params_json,
            })),
            "stats": {
                "mutations": {
                    "attempts": stats.mutations.attempts,
                    "changed_candidates": stats.mutations.changed_candidates,
                    "skipped_or_unchanged": stats.mutations.skipped_or_unchanged,
                },
                "executions": {
                    "started": stats.executions.started,
                    "completed": stats.executions.completed,
                    "valid": stats.executions.valid,
                    "invalid": stats.executions.invalid,
                    "infrastructure_errors": stats.executions.infrastructure_errors,
                    "target_outcomes": stats.executions.target_outcomes,
                    "generator_statuses": stats.executions.generator_statuses,
                    "generator_exits": stats.executions.generator_exits,
                    "last_error": stats.executions.last_error.as_ref().map(|error| json!({
                        "at_unix_ms": error.at_unix_ms,
                        "category": error.category,
                        "message": error.message,
                    })),
                },
                "contribution": {
                    "corpus_cases": stats.contribution.corpus_cases.len(),
                    "novelty_indices": stats.contribution.feedback_novelty_indices.len(),
                },
            },
            "feedback_novelty_indices": {
                "total": indices.len(),
                "offset": offset,
                "limit": limit,
                "items": page,
                "next_offset": next_offset,
            },
        });
        result[if summary_only { "goal" } else { "proposal" }] =
            proposal_value.unwrap_or(Value::Null);
        Ok(result)
    }
}

pub mod http {
    use std::sync::Arc;

    use rmcp::schemars::JsonSchema;
    use rmcp::{
        handler::server::{tool::ToolRouter, wrapper::Parameters},
        tool, tool_handler, tool_router,
        transport::streamable_http_server::{
            session::local::LocalSessionManager, StreamableHttpServerConfig, StreamableHttpService,
        },
        ServerHandler,
    };
    use serde::{Deserialize, Serialize};
    use tokio::sync::oneshot;

    use super::{IntrospectionStore, ProposalId, ProposalOrigin, ProposalStatus};
    use crate::coverage::proposal::{Proposal, ProposalGoal};
    use std::collections::BTreeMap;

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct Response<T> {
        run_id: String,
        observed_at_unix_ms: u64,
        data: T,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct Page<T> {
        total: usize,
        offset: usize,
        limit: usize,
        items: Vec<T>,
        next_offset: Option<usize>,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct ProposalSummary {
        proposal_id: String,
        origin: ProposalOrigin,
        status: ProposalStatus,
        analysis_request_id: Option<String>,
        session: String,
        snapshot: Option<String>,
        goal: Option<ProposalGoal>,
        generator_py: Option<String>,
        mutations: u64,
        mutation_attempts: u64,
        executions: u64,
        corpus_cases: usize,
        novelty_indices: usize,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct Artifacts {
        generator_py: String,
        params_json: String,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct MutationCounts {
        attempts: u64,
        changed_candidates: u64,
        skipped_or_unchanged: u64,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct ExecutionErrorInfo {
        at_unix_ms: u64,
        category: String,
        message: String,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct ExecutionCounts {
        started: u64,
        completed: u64,
        valid: u64,
        invalid: u64,
        infrastructure_errors: u64,
        target_outcomes: BTreeMap<String, u64>,
        generator_statuses: BTreeMap<String, u64>,
        generator_exits: BTreeMap<String, u64>,
        last_error: Option<ExecutionErrorInfo>,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct ContributionCounts {
        corpus_cases: usize,
        novelty_indices: usize,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct ProposalStats {
        mutations: MutationCounts,
        executions: ExecutionCounts,
        contribution: ContributionCounts,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct ProposalDetail {
        proposal_id: String,
        origin: ProposalOrigin,
        status: ProposalStatus,
        analysis_request_id: Option<String>,
        session: String,
        snapshot: Option<String>,
        created_at_unix_ms: u64,
        synthesis_request_id: Option<String>,
        artifacts: Option<Artifacts>,
        stats: ProposalStats,
        feedback_novelty_indices: Page<u32>,
        #[serde(flatten)]
        body: ProposalDetailBody,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    #[serde(untagged)]
    enum ProposalDetailBody {
        Summary {
            #[serde(deserialize_with = "required_option")]
            goal: Option<ProposalGoal>,
        },
        Full {
            #[serde(deserialize_with = "required_option")]
            proposal: Option<Proposal>,
        },
    }

    fn required_option<'de, D, T>(deserializer: D) -> Result<Option<T>, D::Error>
    where
        D: serde::Deserializer<'de>,
        T: Deserialize<'de>,
    {
        Option::<T>::deserialize(deserializer)
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    #[serde(rename_all = "snake_case")]
    enum ResolutionStatus {
        Resolved,
        UnknownIndex,
        NoDebugLocation,
        MappingUnavailable,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct ResolvedIndex {
        index: u32,
        status: ResolutionStatus,
        file: Option<String>,
        line: Option<u32>,
        column: Option<u32>,
        function: Option<String>,
        #[serde(default, skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
    }

    #[derive(Serialize, Deserialize, JsonSchema)]
    struct ResolvedIndices {
        items: Vec<ResolvedIndex>,
    }

    fn typed<T: serde::de::DeserializeOwned>(value: serde_json::Value) -> Result<T, String> {
        serde_json::from_value(value)
            .map_err(|error| format!("invalid introspection response: {error}"))
    }

    #[derive(Clone)]
    pub struct IntrospectionServer {
        store: Arc<IntrospectionStore>,
        tool_router: ToolRouter<Self>,
    }

    #[derive(Debug, Deserialize, JsonSchema)]
    struct ListProposalsRequest {
        statuses: Option<Vec<ProposalStatus>>,
        offset: Option<usize>,
        limit: Option<usize>,
    }

    #[derive(Debug, Deserialize, JsonSchema)]
    struct GetProposalRequest {
        proposal_id: String,
        index_offset: Option<usize>,
        index_limit: Option<usize>,
        #[serde(default)]
        summary_only: bool,
    }

    #[derive(Debug, Deserialize, JsonSchema)]
    struct ResolveBitmapIndicesRequest {
        indices: Vec<u32>,
    }

    #[tool_router]
    impl IntrospectionServer {
        pub fn new(store: Arc<IntrospectionStore>) -> Self {
            Self {
                store,
                tool_router: Self::tool_router(),
            }
        }

        #[tool(
            description = "List registered proposals in registration order. Counts are live: changed_candidates is the number of mutated candidates, executions are completed executor returns, and corpus_cases is the deduplicated count of corpus additions. This is not a frozen coverage snapshot. Read-only."
        )]
        fn list_proposals(
            &self,
            Parameters(request): Parameters<ListProposalsRequest>,
        ) -> Result<rmcp::Json<Response<Page<ProposalSummary>>>, String> {
            let value = self.store.list_proposals(
                request.statuses.as_deref(),
                request.offset,
                request.limit,
            )?;
            Ok(rmcp::Json(typed(value)?))
        }

        #[tool(
            description = "Read a proposal's artifact paths, live stats, and frozen body. Set summary_only to return its goal in place of the body. feedback_novelty_indices is paged in ascending order. Novelty includes first hits and hit-count bucket increases, so an index is not always a new source line. Read-only."
        )]
        fn get_proposal(
            &self,
            Parameters(request): Parameters<GetProposalRequest>,
        ) -> Result<rmcp::Json<Response<ProposalDetail>>, String> {
            let value = self.store.get_proposal_view(
                &ProposalId(request.proposal_id),
                request.index_offset,
                request.index_limit,
                request.summary_only,
            )?;
            Ok(rmcp::Json(typed(value)?))
        }

        #[tool(
            description = "Resolve PCGUARD bitmap indexes loaded once at campaign start. column is always null. Status is resolved, unknown_index, no_debug_location, or mapping_unavailable. Read-only and does not reread the map file."
        )]
        fn resolve_bitmap_indices(
            &self,
            Parameters(request): Parameters<ResolveBitmapIndicesRequest>,
        ) -> Result<rmcp::Json<Response<ResolvedIndices>>, String> {
            let value = self.store.resolve_indices(&request.indices)?;
            Ok(rmcp::Json(typed(value)?))
        }
    }

    #[tool_handler]
    impl ServerHandler for IntrospectionServer {}

    pub struct IntrospectionHttp {
        url: String,
        shutdown: Option<oneshot::Sender<()>>,
        thread: Option<std::thread::JoinHandle<()>>,
    }

    impl IntrospectionHttp {
        pub fn start(
            store: Arc<IntrospectionStore>,
            listener: std::net::TcpListener,
        ) -> Result<Self, String> {
            listener
                .set_nonblocking(true)
                .map_err(|error| format!("setting introspection listener nonblocking: {error}"))?;
            let (bound_tx, bound_rx) = oneshot::channel();
            let (shutdown_tx, shutdown_rx) = oneshot::channel();
            let thread = std::thread::Builder::new()
                .name("campaign-introspection".into())
                .spawn(move || {
                    let runtime = match tokio::runtime::Builder::new_multi_thread()
                        .enable_all()
                        .build()
                    {
                        Ok(runtime) => runtime,
                        Err(error) => {
                            let _ = bound_tx.send(Err(error.to_string()));
                            return;
                        }
                    };
                    runtime.block_on(async move {
                        let listener = match tokio::net::TcpListener::from_std(listener) {
                            Ok(listener) => listener,
                            Err(error) => {
                                let _ = bound_tx.send(Err(error.to_string()));
                                return;
                            }
                        };
                        let port = match listener.local_addr() {
                            Ok(addr) => addr.port(),
                            Err(error) => {
                                let _ = bound_tx.send(Err(error.to_string()));
                                return;
                            }
                        };
                        let service = StreamableHttpService::new(
                            move || Ok(IntrospectionServer::new(Arc::clone(&store))),
                            LocalSessionManager::default().into(),
                            StreamableHttpServerConfig::default(),
                        );
                        let router = axum::Router::new().nest_service("/mcp", service);
                        let _ = bound_tx.send(Ok(port));
                        let _ = axum::serve(listener, router)
                            .with_graceful_shutdown(async move {
                                let _ = shutdown_rx.await;
                            })
                            .await;
                    });
                })
                .map_err(|error| error.to_string())?;
            let port = bound_rx
                .blocking_recv()
                .map_err(|_| "introspection server dropped its bind result".to_string())??;
            Ok(Self {
                url: format!("http://127.0.0.1:{port}/mcp"),
                shutdown: Some(shutdown_tx),
                thread: Some(thread),
            })
        }

        pub fn url(&self) -> &str {
            &self.url
        }

        pub fn shutdown(mut self) {
            if let Some(shutdown) = self.shutdown.take() {
                let _ = shutdown.send(());
            }
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }

    impl Drop for IntrospectionHttp {
        fn drop(&mut self) {
            if let Some(shutdown) = self.shutdown.take() {
                let _ = shutdown.send(());
            }
            if let Some(thread) = self.thread.take() {
                let _ = thread.join();
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::coverage::proposal::{Proposal, ProposalGoal, ProposalStep};
    use std::{cell::RefCell, fs, os::fd::AsRawFd, path::Path, rc::Rc, sync::Barrier};

    fn prebound_listener() -> std::net::TcpListener {
        let listener = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
        listener.set_nonblocking(true).unwrap();
        let flags = unsafe { libc::fcntl(listener.as_raw_fd(), libc::F_GETFD) };
        assert!(flags >= 0 && flags & libc::FD_CLOEXEC != 0);
        listener
    }

    fn identity(id: &str) -> ProposalIdentity {
        ProposalIdentity {
            id: ProposalId::coverage(id),
            origin: ProposalOrigin::Coverage,
            analysis_request_id: Some(id.to_string()),
            session: "s".into(),
            snapshot: Some("000001".into()),
            created_at_unix_ms: 1,
        }
    }

    fn proposal(id: &str) -> Arc<Proposal> {
        Arc::new(Proposal {
            origin: "coverage".into(),
            session: "s".into(),
            build: id.into(),
            snapshot: "000001".into(),
            goal: ProposalGoal {
                handler_ids: vec!["handler".into()],
                reason: "reason".into(),
            },
            parties: BTreeMap::new(),
            party_aliases: BTreeMap::new(),
            channels: BTreeMap::new(),
            trace: vec![ProposalStep {
                name: "step".into(),
                step: "step".into(),
                action: "send".into(),
                condition: String::new(),
                message: None,
                channel: "ch".into(),
                sender: None,
                receiver: None,
                party: None,
                reason: "reason".into(),
                constraints: vec![],
                processing: None,
                mutable_fields: vec![],
            }],
            dependencies: vec![],
        })
    }

    fn report(valid: bool) -> CaseReport {
        CaseReport {
            exit_kind: "ok".into(),
            target_status: "exited:0".into(),
            generator_status: "ok".into(),
            generator_exit: "exited".into(),
            generator_code: Some(0),
            generator_error: None,
            cleanup_signal: None,
            edges: 1,
            coverage: "pcguard".into(),
            elapsed_ms: 1,
            note: String::new(),
            valid,
            startup_timeout_streak: 0,
            result_timeout_streak: 0,
        }
    }

    #[test]
    fn status_mailbox_and_late_result() {
        let mut writer = RegistryWriter::open(
            CampaignIdentity {
                run_id: "run".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        let record = writer.register(identity("a")).unwrap();
        record.accept_proposal(proposal("a")).unwrap();
        record.synthesis_started("a".into()).unwrap();
        record
            .publish_generator(GeneratorArtifacts {
                generator_py: PathBuf::from("/tmp/a/generator.py"),
                params_json: PathBuf::from("/tmp/a/params.json"),
            })
            .unwrap();
        assert!(record.set_status(ProposalStatus::Failed).is_err());

        let replaced = writer.register(identity("b")).unwrap();
        replaced.accept_proposal(proposal("b")).unwrap();
        replaced.set_status(ProposalStatus::Skipped).unwrap();
        assert!(replaced.accept_proposal(proposal("b")).is_err());

        let autonomous = writer
            .register(ProposalIdentity {
                id: ProposalId::synthesis("auto"),
                origin: ProposalOrigin::AutonomousSynthesis,
                analysis_request_id: None,
                session: "s".into(),
                snapshot: None,
                created_at_unix_ms: 2,
            })
            .unwrap();
        assert_eq!(
            autonomous.read_data().unwrap().status,
            ProposalStatus::Synthesizing
        );
    }

    #[test]
    fn corpus_dedup_and_novelty_buckets() {
        let mut writer = RegistryWriter::open(
            CampaignIdentity {
                run_id: "run".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        let record = writer.register(identity("a")).unwrap();
        record.record_mutation(false);
        record.record_mutation(true);
        record.corpus_added(7, &[0, 4, 4, 9]);
        record.corpus_added(7, &[11]);
        record.corpus_added(8, &[4, 12]);
        let data = record.read_data().unwrap();
        assert_eq!(data.stats.mutations.attempts, 2);
        assert_eq!(data.stats.mutations.changed_candidates, 1);
        assert_eq!(data.stats.contribution.corpus_cases.len(), 2);
        assert_eq!(
            data.stats
                .contribution
                .feedback_novelty_indices
                .iter()
                .copied()
                .collect::<Vec<_>>(),
            vec![4, 9, 12]
        );
    }

    #[test]
    fn execution_failure_clears_previous_error_on_start() {
        let mut writer = RegistryWriter::open(
            CampaignIdentity {
                run_id: "run".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        let record = writer.register(identity("a")).unwrap();
        record.execution_started();
        record.execution_failed("executor", "boom");
        record.execution_started();
        assert!(record
            .read_data()
            .unwrap()
            .stats
            .executions
            .last_error
            .is_none());
        record.execution_finished(&report(true));
        let data = record.read_data().unwrap();
        assert_eq!(data.stats.executions.completed, 1);
        assert_eq!(data.stats.executions.valid, 1);
    }

    #[test]
    fn generator_path_conflict_is_reported() {
        let mut writer = RegistryWriter::open(
            CampaignIdentity {
                run_id: "run".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        writer.register(identity("a")).unwrap();
        writer.register(identity("b")).unwrap();
        let path = Path::new("/tmp/generator.py");
        writer
            .bind_generator(path, ProposalId::coverage("a"))
            .unwrap();
        assert!(writer
            .bind_generator(path, ProposalId::coverage("b"))
            .is_err());
    }

    #[test]
    fn concurrent_update_and_query_stay_consistent() {
        let writer = RegistryWriter::open(
            CampaignIdentity {
                run_id: "run".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        let store = writer.store();
        let handle = IntrospectionHandle {
            writer: Rc::new(RefCell::new(writer)),
        };
        handle
            .with_mut(|writer| writer.register(identity("a")))
            .unwrap();
        let record = handle
            .with(|writer| writer.get(&ProposalId::coverage("a")))
            .unwrap();
        let barrier = Arc::new(Barrier::new(2));
        let right = {
            let store = Arc::clone(&store);
            let barrier = Arc::clone(&barrier);
            std::thread::spawn(move || {
                barrier.wait();
                for _ in 0..100 {
                    let _ = store.list_proposals(None, None, Some(10)).unwrap();
                }
            })
        };
        barrier.wait();
        for id in 0..100 {
            record.corpus_added(id, &[id as usize + 1]);
        }
        right.join().unwrap();
        let detail = store
            .get_proposal(&ProposalId::coverage("a"), None, Some(1000))
            .unwrap();
        assert_eq!(detail["data"]["stats"]["contribution"]["corpus_cases"], 100);
    }

    #[test]
    fn missing_map_does_not_block_proposal_queries() {
        let mut writer = RegistryWriter::open(
            CampaignIdentity {
                run_id: "run".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        writer.register(identity("a")).unwrap();
        let store = writer.store();
        let listed = store.list_proposals(None, Some(5), Some(10)).unwrap();
        assert_eq!(listed["data"]["items"].as_array().unwrap().len(), 0);
        let resolved = store.resolve_indices(&[1]).unwrap();
        assert_eq!(
            resolved["data"]["items"][0]["status"],
            "mapping_unavailable"
        );
        assert!(store.resolve_indices(&[]).is_err());
        assert!(store
            .get_proposal(&ProposalId::coverage("missing"), None, None)
            .is_err());
    }

    #[test]
    fn index_map_is_read_once_and_pages_locations() {
        let dir = std::env::temp_dir().join(format!("mcn-index-map-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let index = dir.join("index.z");
        let locations = dir.join("locations.z");
        let mut map = crate::coverage::symbol_map::SymbolMap::default();
        map.index.insert(
            1,
            crate::coverage::symbol_map::Frame {
                function: "parse".into(),
                file: "src/a.c".into(),
                line: 12,
            },
        );
        map.index.insert(
            2,
            crate::coverage::symbol_map::Frame {
                function: "inlined".into(),
                file: String::new(),
                line: 0,
            },
        );
        map.write(&index, &locations).unwrap();
        let handle = IntrospectionHandle::open(
            CampaignIdentity {
                run_id: "mapped".into(),
                started_at_unix_ms: 1,
            },
            Some(&index),
        );
        map.index.insert(
            1,
            crate::coverage::symbol_map::Frame {
                function: "changed".into(),
                file: "src/b.c".into(),
                line: 99,
            },
        );
        map.write(&index, &locations).unwrap();
        let store = handle.store();
        let resolved = store.resolve_indices(&[1, 2, 3]).unwrap();
        let items = resolved["data"]["items"].as_array().unwrap();
        assert_eq!(items[0]["status"], "resolved");
        assert_eq!(items[0]["function"], "parse");
        assert_eq!(items[0]["file"], "src/a.c");
        assert_eq!(items[0]["line"], 12);
        assert!(items[0]["column"].is_null());
        assert_eq!(items[1]["status"], "no_debug_location");
        assert!(items[1]["column"].is_null());
        assert_eq!(items[2]["status"], "unknown_index");
        assert!(items[2]["column"].is_null());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn timeout_late_result_and_cancel_keep_terminal_status() {
        let handle = IntrospectionHandle::open(
            CampaignIdentity {
                run_id: "run".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        handle.register_coverage("late", "s", "000001").unwrap();
        handle.mark_failed("late").unwrap();
        assert!(handle.mark_failed("late").is_err());
        assert!(handle
            .accept(&ProposalId::coverage("late"), proposal("late"))
            .is_err());
        handle.register_coverage("open", "s", "000002").unwrap();
        handle.register_autonomous("auto", "s").unwrap();
        handle
            .publish(
                "auto",
                GeneratorArtifacts {
                    generator_py: PathBuf::from("/tmp/auto/generator.py"),
                    params_json: PathBuf::from("/tmp/auto/params.schema.json"),
                },
                Some(proposal("auto")),
            )
            .unwrap();
        handle.cancel_incomplete();
        let store = handle.store();
        let open = store
            .get_proposal(&ProposalId::coverage("open"), None, None)
            .unwrap();
        assert_eq!(open["data"]["status"], "cancelled");
        let late = store
            .get_proposal(&ProposalId::coverage("late"), None, None)
            .unwrap();
        assert_eq!(late["data"]["status"], "failed");
        let auto = store
            .get_proposal(&ProposalId::synthesis("auto"), None, None)
            .unwrap();
        assert_eq!(auto["data"]["status"], "synthesized");
        assert_eq!(auto["data"]["synthesis_request_id"], "auto");
        assert_eq!(auto["data"]["proposal"]["origin"], "coverage");
    }

    #[test]
    fn closed_loop_counts_baseline_mutations_and_ignores_filtered_results() {
        let dir = std::env::temp_dir().join(format!("mcn-intro-loop-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(dir.join("requests").join("req").join("published")).unwrap();
        let generator = dir
            .join("requests")
            .join("req")
            .join("published")
            .join("generator.py");
        fs::write(&generator, b"def generate(ctx, trace):\n    return b''\n").unwrap();
        let schema = br#"{"type":"object","title":"schema-only"}"#;
        let params = br#"{"login":{"user":"alice","retry":1}}"#;
        fs::write(generator.with_file_name("params.schema.json"), schema).unwrap();
        fs::write(
            generator
                .parent()
                .unwrap()
                .parent()
                .unwrap()
                .join("params.json"),
            params,
        )
        .unwrap();
        let generator = fs::canonicalize(&generator).unwrap();
        let params_json = crate::engine::synthesis::initial_params_path(&generator).unwrap();
        assert_eq!(fs::read(&params_json).unwrap(), params);
        assert_ne!(
            fs::read(generator.with_file_name("params.schema.json")).unwrap(),
            params
        );
        let handle = IntrospectionHandle::open(
            CampaignIdentity {
                run_id: "loop".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        handle.register_coverage("req", "s", "000001").unwrap();
        handle
            .accept(&ProposalId::coverage("req"), proposal("req"))
            .unwrap();
        handle.synthesis_started("req").unwrap();
        handle
            .publish(
                "req",
                GeneratorArtifacts {
                    generator_py: generator.clone(),
                    params_json: params_json.clone(),
                },
                None,
            )
            .unwrap();
        let first = libafl::feedbacks::MapNoveltiesMetadata::new(vec![1, 4, 4, 0]);
        note_evaluated_corpus(
            &handle,
            Some("req"),
            &generator,
            Some(10),
            Some(&first.list),
        )
        .unwrap();
        note_mutation(&handle, Some("req"), &generator, false).unwrap();
        note_mutation(&handle, Some("req"), &generator, true).unwrap();
        let bucket = libafl::feedbacks::MapNoveltiesMetadata::new(vec![4, 9]);
        note_evaluated_corpus(
            &handle,
            Some("req"),
            &generator,
            Some(11),
            Some(&bucket.list),
        )
        .unwrap();
        note_mutation(&handle, Some("req"), &generator, true).unwrap();
        note_evaluated_corpus(&handle, Some("req"), &generator, None, Some(&[99])).unwrap();
        note_evaluated_corpus(
            &handle,
            None,
            Path::new("/tmp/unbound-seed.py"),
            Some(12),
            Some(&[3]),
        )
        .unwrap();
        note_evaluated_corpus(&handle, Some("req"), &generator, Some(13), None).unwrap();
        let record = handle.owner(&generator).unwrap();
        record.execution_started();
        record.execution_finished(&report(true));
        let store = handle.store();
        let detail = store
            .get_proposal(&ProposalId::coverage("req"), None, Some(2))
            .unwrap();
        let summary = store
            .get_proposal_view(&ProposalId::coverage("req"), None, Some(2), true)
            .unwrap();
        let mut expected = detail["data"].clone();
        let proposal = expected
            .as_object_mut()
            .unwrap()
            .remove("proposal")
            .unwrap();
        expected["goal"] = proposal["goal"].clone();
        assert_eq!(summary["data"], expected);
        let reported = detail["data"]["artifacts"]["params_json"].as_str().unwrap();
        assert_eq!(fs::read(reported).unwrap().as_slice(), params.as_slice());
        assert!(reported.ends_with("params.json"), "{reported}");
        assert!(!reported.contains("params.schema.json"), "{reported}");
        assert_eq!(detail["data"]["status"], "synthesized");
        assert_eq!(detail["data"]["stats"]["contribution"]["corpus_cases"], 3);
        assert_eq!(detail["data"]["stats"]["mutations"]["attempts"], 3);
        assert_eq!(
            detail["data"]["stats"]["mutations"]["changed_candidates"],
            2
        );
        assert_eq!(
            detail["data"]["stats"]["mutations"]["skipped_or_unchanged"],
            1
        );
        assert_eq!(detail["data"]["stats"]["executions"]["completed"], 1);
        assert_eq!(detail["data"]["feedback_novelty_indices"]["total"], 3);
        assert_eq!(
            detail["data"]["feedback_novelty_indices"]["items"]
                .as_array()
                .unwrap()
                .len(),
            2
        );
        assert_eq!(detail["data"]["feedback_novelty_indices"]["next_offset"], 2);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn trace_origin_path_conflict_does_not_attribute_stats() {
        let handle = IntrospectionHandle::open(
            CampaignIdentity {
                run_id: "run".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        handle.register_coverage("a", "s", "000001").unwrap();
        handle.register_coverage("b", "s", "000001").unwrap();
        let path = Path::new("/tmp/mcn-intro-conflict.py");
        handle
            .with_mut(|writer| writer.bind_generator(path, ProposalId::coverage("a")))
            .unwrap();
        let Err(error) = resolve_owner(&handle, Some("b"), path) else {
            panic!("expected a generator path conflict");
        };
        assert!(error.contains("conflicts"), "{error}");
        assert!(note_mutation(&handle, Some("b"), path, true).is_err());
        let record = handle
            .with(|writer| writer.get(&ProposalId::coverage("a")))
            .unwrap();
        let data = record.read_data().unwrap();
        assert_eq!(data.stats.mutations.attempts, 0);
        assert_eq!(data.stats.contribution.corpus_cases.len(), 0);
    }

    #[test]
    fn http_mcp_lists_tools_and_isolates_campaigns() {
        let left = IntrospectionHandle::open(
            CampaignIdentity {
                run_id: "campaign-a".into(),
                started_at_unix_ms: 1,
            },
            None,
        );
        let right = IntrospectionHandle::open(
            CampaignIdentity {
                run_id: "campaign-b".into(),
                started_at_unix_ms: 2,
            },
            None,
        );
        left.register_coverage("alpha", "s", "000001").unwrap();
        left.accept(&ProposalId::coverage("alpha"), proposal("build1"))
            .unwrap();
        right.register_coverage("beta", "s", "000001").unwrap();
        let left_listener = prebound_listener();
        let right_listener = prebound_listener();
        let left_port = left_listener.local_addr().unwrap().port();
        let right_port = right_listener.local_addr().unwrap().port();
        let left_http = super::http::IntrospectionHttp::start(left.store(), left_listener).unwrap();
        let right_http =
            super::http::IntrospectionHttp::start(right.store(), right_listener).unwrap();
        assert_eq!(mcp_addr(left_http.url()), format!("127.0.0.1:{left_port}"));
        assert_eq!(
            mcp_addr(right_http.url()),
            format!("127.0.0.1:{right_port}")
        );
        let left_addr = mcp_addr(left_http.url());
        let right_addr = mcp_addr(right_http.url());
        let left_session = mcp_session(&left_addr);
        let right_session = mcp_session(&right_addr);
        let listed = mcp_exchange(
            &left_addr,
            Some(&left_session),
            r#"{"jsonrpc":"2.0","id":2,"method":"tools/list"}"#,
        );
        assert!(listed.contains("list_proposals"), "{listed}");
        assert!(listed.contains("get_proposal"), "{listed}");
        assert!(listed.contains("resolve_bitmap_indices"), "{listed}");
        assert_eq!(listed.matches("outputSchema").count(), 3, "{listed}");
        let left_rows = mcp_exchange(
            &left_addr,
            Some(&left_session),
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_proposals","arguments":{}}}"#,
        );
        assert!(left_rows.contains("campaign-a"), "{left_rows}");
        assert!(left_rows.contains("structuredContent"), "{left_rows}");
        assert!(left_rows.contains("\\\"proposal_id\\\""), "{left_rows}");
        let left_result = mcp_json(&left_rows);
        assert_eq!(
            left_result["result"]["structuredContent"]["data"]["items"][0]["proposal_id"],
            "coverage:alpha"
        );
        let text = left_result["result"]["content"][0]["text"].as_str().unwrap();
        assert_eq!(
            serde_json::from_str::<Value>(text).unwrap(),
            left_result["result"]["structuredContent"]
        );
        for summary_only in [true, false] {
            let request = json!({"jsonrpc":"2.0", "id":10, "method":"tools/call", "params":{
                "name":"get_proposal", "arguments":{"proposal_id":"coverage:alpha", "summary_only":summary_only}
            }}).to_string();
            let response = mcp_json(&mcp_exchange(&left_addr, Some(&left_session), &request));
            let data = &response["result"]["structuredContent"]["data"];
            if summary_only {
                assert_eq!(data["goal"]["handler_ids"][0], "handler");
                assert!(data.get("proposal").is_none());
            } else {
                assert_eq!(data["proposal"]["goal"]["handler_ids"][0], "handler");
                assert!(data.get("goal").is_none());
            }
        }
        let request = r#"{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"resolve_bitmap_indices","arguments":{"indices":[1]}}}"#;
        let response = mcp_json(&mcp_exchange(&left_addr, Some(&left_session), request));
        assert_eq!(
            response["result"]["structuredContent"]["data"]["items"][0]["status"],
            "mapping_unavailable"
        );
        assert!(left_rows.contains("coverage:alpha"), "{left_rows}");
        assert!(!left_rows.contains("coverage:beta"), "{left_rows}");
        let right_rows = mcp_exchange(
            &right_addr,
            Some(&right_session),
            r#"{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"list_proposals","arguments":{}}}"#,
        );
        assert!(right_rows.contains("campaign-b"), "{right_rows}");
        assert!(right_rows.contains("coverage:beta"), "{right_rows}");
        assert!(!right_rows.contains("coverage:alpha"), "{right_rows}");
        left_http.shutdown();
        assert!(std::net::TcpStream::connect(&left_addr).is_err());
        let still = mcp_exchange(
            &right_addr,
            Some(&right_session),
            r#"{"jsonrpc":"2.0","id":4,"method":"tools/call","params":{"name":"get_proposal","arguments":{"proposal_id":"coverage:beta"}}}"#,
        );
        assert!(still.contains("coverage:beta"), "{still}");
        assert!(still.contains("structuredContent"), "{still}");
        assert!(still.contains("\\\"proposal\\\""), "{still}");
        right_http.shutdown();
    }

    fn mcp_addr(url: &str) -> String {
        url.trim_start_matches("http://")
            .trim_end_matches("/mcp")
            .to_string()
    }

    fn mcp_json(response: &str) -> Value {
        let body = response.split_once("\r\n\r\n").expect("HTTP response body").1;
        let json = body.lines().find_map(|line| line.strip_prefix("data: ")).unwrap_or(body);
        serde_json::from_str(json.trim()).expect("MCP JSON response")
    }

    fn mcp_exchange(addr: &str, session: Option<&str>, body: &str) -> String {
        use std::io::{Read, Write};
        use std::net::TcpStream;
        use std::time::Duration;
        let mut stream =
            TcpStream::connect(addr).unwrap_or_else(|error| panic!("connect {addr}: {error}"));
        stream
            .set_read_timeout(Some(Duration::from_secs(2)))
            .unwrap();
        let mut head = format!(
            "POST /mcp HTTP/1.1\r\nHost: {addr}\r\nContent-Type: application/json\r\nAccept: application/json, text/event-stream\r\nContent-Length: {}\r\nConnection: close\r\n",
            body.len()
        );
        if let Some(session) = session {
            head.push_str(&format!("Mcp-Session-Id: {session}\r\n"));
        }
        head.push_str("\r\n");
        stream.write_all(head.as_bytes()).unwrap();
        stream.write_all(body.as_bytes()).unwrap();
        let mut buf = Vec::new();
        let mut tmp = [0u8; 4096];
        loop {
            match stream.read(&mut tmp) {
                Ok(0) => break,
                Ok(count) => buf.extend_from_slice(&tmp[..count]),
                Err(error)
                    if error.kind() == std::io::ErrorKind::WouldBlock
                        || error.kind() == std::io::ErrorKind::TimedOut =>
                {
                    break;
                }
                Err(error) => panic!("{error}"),
            }
        }
        String::from_utf8_lossy(&buf).into_owned()
    }

    fn mcp_session(addr: &str) -> String {
        let response = mcp_exchange(
            addr,
            None,
            r#"{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-03-26","capabilities":{},"clientInfo":{"name":"introspection-test","version":"0"}}}"#,
        );
        let session = response
            .lines()
            .find_map(|line| {
                let (name, value) = line.split_once(':')?;
                name.eq_ignore_ascii_case("mcp-session-id")
                    .then(|| value.trim().to_string())
            })
            .unwrap_or_else(|| panic!("missing session in {response}"));
        let _ = mcp_exchange(
            addr,
            Some(&session),
            r#"{"jsonrpc":"2.0","method":"notifications/initialized"}"#,
        );
        session
    }
}
