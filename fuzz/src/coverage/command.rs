//! `mcn-fuzz coverage`: replay saved generator inputs and write coverage over time.
//!
//! The command does not start the fuzz loop, the synthesis agent, or the
//! coverage-analysis agent. It calls the same batch replay the coverage stage uses.

use std::{
    fs::{self, File, OpenOptions},
    io::Write,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

use libafl::{inputs::Input, Error};

use super::{
    build_fingerprint, load_checkpoint, replay_pending, resume_or_refuse,
    BatchReport, BatchTimings, CaseLocation, CoverageSummary, OutputLock,
    PendingCase, Resume, SourceCoverageProgress,
};
use crate::{app::config::Campaign, engine::input::GeneratorInput};

/// Replay `corpus` through the campaign coverage binary into an empty `output`.
///
/// `step` is the number of inputs per exported batch. The campaign's periodic
/// coverage switch is ignored: this command always requests source coverage.
pub fn run(
    campaign: &Campaign,
    output: &Path,
    corpus: Option<&Path>,
    step: u64,
) -> Result<(), Error> {
    let step = require_step(step)?;
    let output = absolute_dir(output)?;
    reject_non_empty(&output)?;
    fs::create_dir_all(&output)?;
    let corpus = corpus.unwrap_or(campaign.campaign.corpus.as_path());
    let selected = select_inputs(corpus)?;
    if selected.is_empty() {
        return Err(Error::illegal_argument(format!(
            "coverage corpus {} has no generator inputs",
            corpus.display()
        )));
    }
    let config = &campaign.coverage;
    if config.binary.as_os_str().is_empty() {
        return Err(Error::illegal_argument(
            "campaign coverage.binary is required for coverage replay".to_string(),
        ));
    }
    let fingerprint = build_fingerprint(&config.binary, &config.llvm_profdata, &config.llvm_cov)?;
    let root = output.join(&fingerprint);
    let _lock = OutputLock::acquire(&output)?;
    match resume_or_refuse(&root)? {
        Resume::Refuse { reason } => {
            return Err(Error::illegal_state(reason));
        }
        Resume::Ready => {}
    }
    let mut progress =
        load_checkpoint(&root).unwrap_or_else(|| SourceCoverageProgress::new(&fingerprint));
    if progress.fingerprint != fingerprint {
        progress = SourceCoverageProgress::new(&fingerprint);
    }
    let mut csv = Csv::create(&output.join("cov_over_time.csv"))?;
    let mut cumulative = 0_u64;
    for batch in selected.chunks(step as usize) {
        let pending = pending_cases(batch)?;
        let report = replay_pending(
            campaign,
            config,
            &root,
            &fingerprint,
            None,
            None,
            &pending,
            &progress,
            BatchTimings::default(),
        )?;
        let finished = cumulative.saturating_add(report.succeeded);
        cumulative = match record_published_batch(&mut csv, &report, batch, cumulative) {
            Ok(cumulative) => cumulative,
            Err(error) => {
                return Err(Error::unknown(format!(
                    "coverage replay stopped after {finished} inputs: {error}"
                )));
            }
        };
        progress = report.progress;
    }
    Ok(())
}

/// Clap parser. Zero is rejected before the command runs.
pub fn parse_step(raw: &str) -> Result<u64, String> {
    let step: u64 = raw
        .parse()
        .map_err(|_| format!("coverage --step must be greater than 0, got {raw}"))?;
    if step == 0 {
        return Err("coverage --step must be greater than 0".to_string());
    }
    Ok(step)
}

/// `step` must be at least 1. The CLI default is 1.
pub fn require_step(step: u64) -> Result<u64, Error> {
    if step == 0 {
        return Err(Error::illegal_argument(
            "coverage --step must be greater than 0".to_string(),
        ));
    }
    Ok(step)
}

/// LLVM profile paths are resolved from the coverage child's working directory,
/// so the output directory has to be absolute before replay starts.
fn absolute_dir(path: &Path) -> Result<PathBuf, Error> {
    if path.exists() {
        return fs::canonicalize(path).map_err(|error| {
            Error::unknown(format!("coverage output {}: {error}", path.display()))
        });
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    let parent = fs::canonicalize(parent).map_err(|error| {
        Error::unknown(format!(
            "coverage output parent {}: {error}",
            parent.display()
        ))
    })?;
    let name = path.file_name().ok_or_else(|| {
        Error::illegal_argument(format!(
            "coverage output {} has no directory name",
            path.display()
        ))
    })?;
    Ok(parent.join(name))
}

/// Refuse an output that already contains anything, including hidden names.
pub fn reject_non_empty(output: &Path) -> Result<(), Error> {
    if !output.exists() {
        return Ok(());
    }
    if !output.is_dir() {
        return Err(Error::illegal_argument(format!(
            "coverage output {} is not a directory",
            output.display()
        )));
    }
    let mut entries = fs::read_dir(output)?;
    if entries.next().is_some() {
        return Err(Error::illegal_argument(format!(
            "coverage output {} is not empty",
            output.display()
        )));
    }
    Ok(())
}

#[derive(Debug)]
struct SavedInput {
    name: String,
    modified_unix: u64,
    input: GeneratorInput,
}

/// Regular, non-hidden corpus files, excluding `.metadata` and `.pcap` sidecars.
/// Every file is parsed before the caller starts replay. Order is mtime, then name.
fn select_inputs(corpus: &Path) -> Result<Vec<SavedInput>, Error> {
    if !corpus.is_dir() {
        return Err(Error::illegal_argument(format!(
            "coverage corpus {} is not a directory",
            corpus.display()
        )));
    }
    let mut found = Vec::new();
    for entry in fs::read_dir(corpus)? {
        let entry = entry?;
        let name = entry.file_name();
        let name = name.to_string_lossy();
        if name.starts_with('.') || name.ends_with(".metadata") || name.ends_with(".pcap") {
            continue;
        }
        let file_type = entry.file_type()?;
        if !file_type.is_file() {
            continue;
        }
        let path = entry.path();
        let modified = entry
            .metadata()?
            .modified()
            .unwrap_or(SystemTime::UNIX_EPOCH);
        found.push((name.into_owned(), modified, path));
    }
    found.sort_by(|left, right| left.1.cmp(&right.1).then_with(|| left.0.cmp(&right.0)));
    let mut errors = Vec::new();
    let mut selected = Vec::with_capacity(found.len());
    for (name, modified, path) in found {
        match GeneratorInput::from_file(&path) {
            Ok(input) => selected.push(SavedInput {
                name,
                modified_unix: unix_secs(modified),
                input,
            }),
            Err(error) => errors.push(format!("{}: {error}", path.display())),
        }
    }
    if !errors.is_empty() {
        return Err(Error::serialize(format!(
            "malformed generator inputs were not replayed: {}",
            errors.join("; ")
        )));
    }
    Ok(selected)
}

#[cfg(test)]
pub(super) fn replay_directory_once(
    campaign: &Campaign,
    root: &Path,
    corpus: &Path,
) -> Result<BatchReport, Error> {
    let selected = select_inputs(corpus)?;
    let pending = pending_cases(&selected)?;
    let fingerprint = build_fingerprint(
        &campaign.coverage.binary,
        &campaign.coverage.llvm_profdata,
        &campaign.coverage.llvm_cov,
    )?;
    let progress = SourceCoverageProgress::new(&fingerprint);
    replay_pending(
        campaign,
        &campaign.coverage,
        &root.join(&fingerprint),
        &fingerprint,
        None,
        None,
        &pending,
        &progress,
        BatchTimings::default(),
    )
}

fn pending_cases(batch: &[SavedInput]) -> Result<Vec<PendingCase>, Error> {
    let mut pending = Vec::with_capacity(batch.len());
    for (index, saved) in batch.iter().enumerate() {
        let id = format!("corpus:{index}");
        pending.push(PendingCase {
            location: CaseLocation {
                id: id.clone(),
                corpus_id: index,
                filename: Some(saved.name.clone()),
            },
            id,
            input: saved.input.clone(),
        });
    }
    Ok(pending)
}

/// Covered percentage in the range 0–100. A zero total is 0, not an error.
pub fn coverage_percent(covered: u64, total: u64) -> f64 {
    if total == 0 {
        0.0
    } else {
        (covered as f64) * 100.0 / (total as f64)
    }
}

fn unix_secs(time: SystemTime) -> u64 {
    time.duration_since(UNIX_EPOCH)
        .map(|value| value.as_secs())
        .unwrap_or(0)
}

struct Csv {
    file: File,
}

impl Csv {
    fn create(path: &Path) -> Result<Self, Error> {
        let mut file = OpenOptions::new()
            .create(true)
            .truncate(true)
            .write(true)
            .open(path)?;
        file.write_all(b"Time,l_per,l_abs,b_per,b_abs,l_total,b_total,inputs\n")?;
        file.flush()?;
        Ok(Self { file })
    }

    fn write_row(
        &mut self,
        summary: &CoverageSummary,
        time: u64,
        inputs: u64,
    ) -> Result<(), Error> {
        let line = format!(
            "{time},{:.4},{},{:.4},{},{},{},{inputs}\n",
            coverage_percent(summary.lines, summary.lines_total),
            summary.lines,
            coverage_percent(summary.branches, summary.branches_total),
            summary.branches,
            summary.lines_total,
            summary.branches_total,
        );
        self.file.write_all(line.as_bytes())?;
        self.file.flush()?;
        Ok(())
    }
}

/// Record one published batch. A failure after a non-empty prefix still writes
/// the row, then returns the failure so the command exits non-zero.
fn record_published_batch(
    csv: &mut Csv,
    report: &BatchReport,
    batch: &[SavedInput],
    cumulative: u64,
) -> Result<u64, Error> {
    let mut cumulative = cumulative;
    if report.succeeded > 0 {
        let index = (report.succeeded as usize).saturating_sub(1);
        let time = batch.get(index).map(|item| item.modified_unix).unwrap_or(0);
        cumulative = cumulative.saturating_add(report.succeeded);
        csv.write_row(&report.summary, time, cumulative)?;
    }
    if let Some(failure) = &report.failure {
        return Err(Error::unknown(failure.clone()));
    }
    Ok(cumulative)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread::sleep;
    use std::time::Duration;

    fn touch(path: &Path, body: &str) {
        fs::write(path, body).unwrap();
    }

    #[test]
    fn step_zero_is_rejected_and_one_is_kept() {
        assert!(require_step(0).is_err());
        assert_eq!(require_step(1).unwrap(), 1);
        assert_eq!(require_step(4).unwrap(), 4);
    }

    #[test]
    fn non_empty_output_is_rejected_before_replay() {
        let dir = std::env::temp_dir().join(format!("mcn-cov-out-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        assert!(reject_non_empty(&dir).is_ok());
        fs::create_dir_all(&dir).unwrap();
        assert!(reject_non_empty(&dir).is_ok());
        touch(&dir.join(".hidden"), "x");
        assert!(reject_non_empty(&dir).is_err());
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn selection_filters_orders_and_resolves_generator_paths() {
        let root = std::env::temp_dir().join(format!("mcn-cov-select-{}", std::process::id()));
        let dir = root.join("corpus");
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&dir).unwrap();
        let script = root.join("gen.py");
        touch(&script, "def generate(ctx, params):\n    return None\n");
        touch(
            &dir.join(".hidden.json"),
            "{\"generator\":\"gen.py\",\"params\":{}}\n",
        );
        touch(&dir.join("skip.metadata"), "{}");
        touch(&dir.join("skip.pcap"), "pcap");
        fs::create_dir(dir.join("nested")).unwrap();
        let later = dir.join("b.json");
        let earlier = dir.join("a.json");
        touch(
            &earlier,
            &format!(
                "{{\"generator\":\"{}\",\"params\":{{\"n\":1}}}}\n",
                script.display()
            ),
        );
        sleep(Duration::from_millis(20));
        touch(
            &later,
            "{\"generator\":\"gen.py\",\"params\":{\"n\":2}}\n",
        );
        let same = dir.join("c.json");
        touch(
            &same,
            "{\"generator\":\"gen.py\",\"params\":{\"n\":2}}\n",
        );
        let selected = select_inputs(&dir).unwrap();
        let names: Vec<_> = selected.iter().map(|item| item.name.clone()).collect();
        assert_eq!(names, vec!["a.json", "b.json", "c.json"]);
        assert!(selected[0].modified_unix <= selected[1].modified_unix);
        assert!(selected[0].input.generator.ends_with("gen.py"));
        assert!(selected[1].input.generator.ends_with("gen.py"));
        let bad = dir.join("bad.json");
        touch(&bad, "not-json");
        let error = select_inputs(&dir).unwrap_err();
        let message = format!("{error}");
        assert!(message.contains("bad.json"), "{message}");
        assert!(message.contains("malformed"), "{message}");
        let _ = fs::remove_dir_all(&root);
    }

    #[test]
    fn percentages_are_zero_when_the_total_is_zero() {
        assert_eq!(coverage_percent(0, 0), 0.0);
        assert_eq!(coverage_percent(1, 4), 25.0);
        assert_eq!(coverage_percent(0, 8), 0.0);
    }

    #[test]
    fn csv_records_exact_and_partial_batches_with_cumulative_inputs() {
        let dir = std::env::temp_dir().join(format!("mcn-cov-csv-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("cov_over_time.csv");
        let mut csv = Csv::create(&path).unwrap();
        let batch = vec![
            SavedInput {
                name: "a".into(),
                modified_unix: 10,
                input: dummy_input(),
            },
            SavedInput {
                name: "b".into(),
                modified_unix: 20,
                input: dummy_input(),
            },
            SavedInput {
                name: "c".into(),
                modified_unix: 30,
                input: dummy_input(),
            },
        ];
        let summary = CoverageSummary {
            lines: 1,
            lines_total: 4,
            branches: 0,
            branches_total: 0,
            ..CoverageSummary::default()
        };
        let full = BatchReport {
            progress: SourceCoverageProgress::default(),
            summary,
            previous: CoverageSummary::default(),
            succeeded: 2,
            failure: None,
        };
        let cumulative = record_published_batch(&mut csv, &full, &batch[..2], 0).unwrap();
        assert_eq!(cumulative, 2);
        let partial = BatchReport {
            succeeded: 1,
            summary: CoverageSummary {
                lines: 2,
                lines_total: 4,
                ..summary
            },
            failure: Some("second input failed".to_string()),
            ..full.clone()
        };
        let error =
            record_published_batch(&mut csv, &partial, &batch[2..], cumulative).unwrap_err();
        assert!(format!("{error}").contains("second input failed"));
        let text = fs::read_to_string(&path).unwrap();
        let lines: Vec<_> = text.lines().collect();
        assert_eq!(
            lines[0],
            "Time,l_per,l_abs,b_per,b_abs,l_total,b_total,inputs"
        );
        assert_eq!(lines[1], "20,25.0000,1,0.0000,0,4,0,2");
        assert_eq!(lines[2], "30,50.0000,2,0.0000,0,4,0,3");
        let _ = fs::remove_dir_all(&dir);
    }

    fn dummy_input() -> GeneratorInput {
        GeneratorInput::new(std::path::PathBuf::from("gen.py"), Default::default())
    }
}
