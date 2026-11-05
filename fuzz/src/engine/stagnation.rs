//! Coordinate bitmap stagnation, source coverage, and the next synthesis request.

use libafl::Error;
use libafl_bolts::impl_serdeany;
use serde::{Deserialize, Serialize};

pub const DEFAULT_BITMAP_STAGNATION_LIMIT: u64 = 256;

#[derive(Clone, Copy, Debug, Default, PartialEq, Eq, Serialize, Deserialize)]
enum PendingStage {
    #[default]
    None,
    SourceCoverage,
    Synthesis,
}

/// Fuzzer-thread state. Only ordinary mutated cases increase the no-growth streak.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct BitmapProgress {
    seen: Vec<bool>,
    limit: u64,
    pub consecutive_no_coverage: u64,
    mutation_pending: bool,
    pending: PendingStage,
    #[serde(default)]
    synthesis_inflight: bool,
}

impl Default for BitmapProgress {
    fn default() -> Self {
        Self::new(DEFAULT_BITMAP_STAGNATION_LIMIT).expect("the default limit is positive")
    }
}

impl_serdeany!(BitmapProgress);

impl BitmapProgress {
    pub fn new(limit: u64) -> Result<Self, Error> {
        if limit == 0 {
            return Err(Error::illegal_argument(
                "campaign.bitmap_stagnation_limit must be greater than 0".to_string(),
            ));
        }
        Ok(Self {
            seen: Vec::new(),
            limit,
            consecutive_no_coverage: 0,
            mutation_pending: false,
            pending: PendingStage::None,
            synthesis_inflight: false,
        })
    }

    pub fn set_mutation_pending(&mut self, pending: bool) {
        self.mutation_pending = pending;
    }

    /// Keep observing coverage while synthesis runs, without accumulating a trigger.
    /// Resume with a fresh streak once the fuzzer thread handles the outcome.
    pub fn set_synthesis_inflight(&mut self, inflight: bool) {
        if inflight || self.synthesis_inflight != inflight {
            self.pending = PendingStage::None;
            self.consecutive_no_coverage = 0;
        }
        self.synthesis_inflight = inflight;
    }

    /// Observe the raw map without changing it or the normal hitcount feedback.
    /// Index 0 is a runtime marker. Hitcount changes on a known slot are not growth.
    /// Seeds and synthesized inputs extend the baseline but do not increase the streak.
    pub fn observe(&mut self, map: &[u8], valid: bool) {
        let mutated = std::mem::take(&mut self.mutation_pending);
        if !valid {
            return;
        }
        self.seen.resize(self.seen.len().max(map.len()), false);
        let mut grew = false;
        for (index, value) in map.iter().enumerate().skip(1) {
            if *value != 0 && !self.seen[index] {
                self.seen[index] = true;
                grew = true;
            }
        }
        if grew {
            if self.consecutive_no_coverage > 0 {
                log::info!(
                    "bitmap coverage stagnant reset after new bitmap slot; previous streak {}",
                    self.consecutive_no_coverage
                );
            }
            self.consecutive_no_coverage = 0;
        } else if mutated && !self.synthesis_inflight && self.pending == PendingStage::None {
            self.consecutive_no_coverage = self.consecutive_no_coverage.saturating_add(1);
            log::info!(
                "bitmap coverage stagnant: {}/{} consecutive mutated cases",
                self.consecutive_no_coverage,
                self.limit
            );
            if self.consecutive_no_coverage >= self.limit {
                self.pending = PendingStage::SourceCoverage;
                log::info!(
                    "bitmap coverage stagnant for {} mutated cases; source coverage is due after mutation",
                    self.consecutive_no_coverage
                );
            }
        }
    }

    pub fn source_coverage_due(&self) -> bool {
        !self.synthesis_inflight && self.pending == PendingStage::SourceCoverage
    }

    /// A skipped or failed source-coverage batch must also release the next stage.
    pub fn finish_source_coverage(&mut self) {
        if self.source_coverage_due() {
            self.pending = PendingStage::Synthesis;
            self.consecutive_no_coverage = 0;
        }
    }

    pub fn synthesis_due(&self) -> bool {
        !self.synthesis_inflight && self.pending == PendingStage::Synthesis
    }

    pub fn take_synthesis_request(&mut self) -> bool {
        if !self.synthesis_due() {
            return false;
        }
        self.pending = PendingStage::None;
        self.consecutive_no_coverage = 0;
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn mutated(progress: &mut BitmapProgress, map: &[u8]) {
        progress.set_mutation_pending(true);
        progress.observe(map, true);
    }

    #[test]
    fn consecutive_valid_mutations_trigger_coverage_before_synthesis() {
        let mut progress = BitmapProgress::new(3).unwrap();
        progress.observe(&[1, 1, 0], true);
        assert_eq!(progress.consecutive_no_coverage, 0);
        mutated(&mut progress, &[1, 1, 0]);
        mutated(&mut progress, &[1, 1, 0]);
        assert!(!progress.source_coverage_due());
        assert!(!progress.synthesis_due());
        mutated(&mut progress, &[1, 1, 0]);
        assert!(progress.source_coverage_due());
        assert!(!progress.take_synthesis_request());
        progress.finish_source_coverage();
        assert!(progress.synthesis_due());
        assert!(progress.take_synthesis_request());
        assert!(!progress.take_synthesis_request());
        assert_eq!(progress.consecutive_no_coverage, 0);
        mutated(&mut progress, &[1, 1, 0]);
        assert_eq!(progress.consecutive_no_coverage, 1);
    }

    #[test]
    fn new_slot_resets_streak_but_hitcounts_and_runtime_marker_do_not() {
        let mut progress = BitmapProgress::new(3).unwrap();
        progress.observe(&[0, 1, 0], true);
        mutated(&mut progress, &[1, 2, 0]);
        mutated(&mut progress, &[2, 4, 0]);
        assert_eq!(progress.consecutive_no_coverage, 2);
        mutated(&mut progress, &[1, 1, 1]);
        assert_eq!(progress.consecutive_no_coverage, 0);
        assert!(!progress.source_coverage_due());
    }

    #[test]
    fn seeds_synthesized_inputs_skips_and_invalid_cases_do_not_count() {
        let mut progress = BitmapProgress::new(2).unwrap();
        progress.observe(&[1, 1, 0], true);
        progress.observe(&[1, 1, 0], true);
        progress.set_mutation_pending(false);
        assert_eq!(progress.consecutive_no_coverage, 0);
        mutated(&mut progress, &[1, 1, 0]);
        progress.set_mutation_pending(true);
        progress.observe(&[1, 1, 1], false);
        assert_eq!(progress.consecutive_no_coverage, 1);
        // Invalid coverage was not added to the baseline.
        progress.observe(&[1, 1, 1], true);
        assert_eq!(progress.consecutive_no_coverage, 0);
        mutated(&mut progress, &[1, 1, 1]);
        assert_eq!(progress.consecutive_no_coverage, 1);
    }

    #[test]
    fn trigger_is_latched_until_the_end_of_the_mutational_stage() {
        let mut progress = BitmapProgress::new(1).unwrap();
        progress.observe(&[1, 1, 0], true);
        mutated(&mut progress, &[1, 1, 0]);
        mutated(&mut progress, &[1, 1, 1]);
        assert!(progress.source_coverage_due());
        progress.finish_source_coverage();
        // A latched trigger waits for the next stage boundary.
        for _ in 0..10 {
            mutated(&mut progress, &[1, 1, 1]);
        }
        assert!(progress.synthesis_due());
        assert!(!progress.source_coverage_due());
        assert_eq!(progress.consecutive_no_coverage, 0);
        assert!(progress.take_synthesis_request());
    }

    #[test]
    fn inflight_synthesis_keeps_bitmap_baseline_without_counting_or_queuing() {
        let mut progress = BitmapProgress::new(3).unwrap();
        progress.observe(&[1, 1, 0], true);
        progress.set_synthesis_inflight(true);
        mutated(&mut progress, &[1, 1, 1]);
        for _ in 0..10 {
            mutated(&mut progress, &[1, 1, 1]);
            assert_eq!(progress.consecutive_no_coverage, 0);
            assert!(!progress.source_coverage_due());
            progress.finish_source_coverage();
            assert!(!progress.synthesis_due());
            assert!(!progress.take_synthesis_request());
        }
        progress.set_synthesis_inflight(false);
        // The slot discovered while busy is already in the bitmap baseline.
        for expected in 1..=3 {
            mutated(&mut progress, &[1, 1, 1]);
            assert_eq!(progress.consecutive_no_coverage, expected);
            assert_eq!(progress.source_coverage_due(), expected == 3);
        }
    }

    #[test]
    fn starting_synthesis_discards_stale_triggers() {
        for coverage_finished in [false, true] {
            let mut progress = BitmapProgress::new(1).unwrap();
            progress.observe(&[1, 1], true);
            mutated(&mut progress, &[1, 1]);
            if coverage_finished {
                progress.finish_source_coverage();
            }
            progress.set_synthesis_inflight(true);
            progress.set_synthesis_inflight(false);
            assert_eq!(progress.consecutive_no_coverage, 0);
            assert!(!progress.source_coverage_due());
            assert!(!progress.synthesis_due());
        }
    }

    #[test]
    fn zero_limit_is_rejected() {
        assert!(BitmapProgress::new(0).is_err());
    }
}
