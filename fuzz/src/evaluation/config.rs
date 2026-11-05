//! Evaluation suite files. Paths inside the suite are relative to the suite file.

use std::{
    collections::BTreeSet,
    fs,
    path::{Path, PathBuf},
};

use serde::Deserialize;

use crate::app::config::{PartiesConfig, PartySelector};

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Suite {
    pub campaign: PathBuf,
    #[serde(default = "default_ready")]
    pub ready_ms: u64,
    #[serde(default = "default_exit")]
    pub sut_exit_ms: u64,
    #[serde(default = "default_term")]
    pub term_grace_ms: u64,
    pub binding: SuiteBinding,
    #[serde(default)]
    pub prefix: Vec<String>,
    #[serde(default)]
    pub suffix: Vec<String>,
    #[serde(default)]
    pub cases: Vec<SuiteCase>,
    #[serde(skip)]
    pub path: PathBuf,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SuiteBinding {
    pub sut: Vec<String>,
    pub generator: Vec<String>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(deny_unknown_fields)]
pub struct SuiteCase {
    pub name: String,
    #[serde(default)]
    pub steps: Vec<String>,
}

fn default_ready() -> u64 {
    2_000
}

fn default_exit() -> u64 {
    1_000
}

fn default_term() -> u64 {
    200
}

impl Suite {
    pub fn load(path: &Path) -> Result<Self, String> {
        let text = fs::read_to_string(path)
            .map_err(|error| format!("reading suite {}: {error}", path.display()))?;
        let mut suite: Suite = toml::from_str(&text)
            .map_err(|error| format!("parsing suite {}: {error}", path.display()))?;
        let base = path.parent().unwrap_or_else(|| Path::new("."));
        if !suite.campaign.is_absolute() {
            suite.campaign = base.join(&suite.campaign);
        }
        suite.path = path.to_path_buf();
        if suite.cases.is_empty() {
            return Err("suite has no cases".to_string());
        }
        let mut names = BTreeSet::new();
        for case in &suite.cases {
            if case.name.is_empty() || case.name.contains('/') || case.name.contains('\\') {
                return Err(format!(
                    "case name {:?} is not a single path segment",
                    case.name
                ));
            }
            if !names.insert(case.name.clone()) {
                return Err(format!("case {} is listed more than once", case.name));
            }
        }
        if suite.binding.sut.is_empty() || suite.binding.generator.is_empty() {
            return Err("suite binding needs non-empty sut and generator lists".to_string());
        }
        Ok(suite)
    }

    pub fn parties(&self) -> PartiesConfig {
        PartiesConfig {
            sut: PartySelector::Names(self.binding.sut.clone()),
            generator: PartySelector::Names(self.binding.generator.clone()),
        }
    }

    /// Prefix, then the case steps, then the suffix. Order is the suite order.
    pub fn sequence(&self, case: &SuiteCase) -> Vec<String> {
        let mut names = self.prefix.clone();
        names.extend(case.steps.iter().cloned());
        names.extend(self.suffix.iter().cloned());
        names
    }

    pub fn select<'a>(&'a self, requested: &[String]) -> Result<Vec<&'a SuiteCase>, String> {
        if requested.is_empty() {
            return Ok(self.cases.iter().collect());
        }
        let mut chosen = Vec::new();
        for name in requested {
            let case = self
                .cases
                .iter()
                .find(|case| case.name == *name)
                .ok_or_else(|| format!("suite has no case {name}"))?;
            chosen.push(case);
        }
        Ok(chosen)
    }
}
