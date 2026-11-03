//! Campaign configuration. Paths in the TOML file are relative to that file.

use std::{
    fs,
    path::{Path, PathBuf},
};

use libafl::Error;
use serde::Deserialize;

fn default_host() -> String {
    "127.0.0.1".to_string()
}

fn default_iterations() -> u64 {
    10
}

fn default_map_size() -> usize {
    65_536
}

fn default_config_output() -> String {
    "fftp.conf".to_string()
}

fn default_args() -> Vec<String> {
    vec!["@CONFIG@".to_string()]
}

fn default_cert() -> String {
    "certs/my.crt".to_string()
}

fn default_key() -> String {
    "certs/my.pem".to_string()
}

fn default_file_limit() -> u64 {
    67_108_864
}

fn default_drain_quiet_ms() -> u64 {
    5
}

fn default_drain_timeout_ms() -> u64 {
    100
}

/// How long each phase of one case may run.
#[derive(Clone, Debug, Deserialize)]
pub struct Timeouts {
    /// Budget for the first TCP connect, in milliseconds.
    pub connect_ms: u64,
    /// Budget for one blocking socket read or write, in milliseconds.
    pub io_ms: u64,
    /// Budget for the whole case, in milliseconds.
    pub case_ms: u64,
    /// How long to wait for the target to exit after the generator closes.
    pub exit_grace_ms: u64,
}

impl Default for Timeouts {
    fn default() -> Self {
        Self {
            connect_ms: 2_000,
            io_ms: 1_000,
            case_ms: 10_000,
            exit_grace_ms: 5_000,
        }
    }
}

/// One forkserver target. LightFTP keeps the historical defaults.
#[derive(Clone, Debug, Deserialize)]
pub struct TargetConfig {
    /// PCGUARD forkserver ELF.
    pub binary: PathBuf,
    /// Directory restored from `fixture` before every case.
    pub workdir: PathBuf,
    /// Snapshot copied over `workdir`.
    pub fixture: PathBuf,
    /// Config template. Placeholders are replaced when it is written.
    pub config_template: PathBuf,
    /// File name written under `workdir`. LightFTP uses `fftp.conf`.
    #[serde(default = "default_config_output")]
    pub config_output: String,
    /// Arguments after the program. `@CONFIG@` is the rendered config path.
    #[serde(default = "default_args")]
    pub args: Vec<String>,
    /// Certificate path relative to `fixture`.
    #[serde(default = "default_cert")]
    pub cert: String,
    /// Private key path relative to `fixture`.
    #[serde(default = "default_key")]
    pub key: String,
    /// CA path relative to `fixture`. Omitted keeps the LightFTP CA lookup.
    #[serde(default)]
    pub ca: Option<String>,
    #[serde(default = "default_host")]
    pub host: String,
    /// Control port. `0` picks a free port once for the campaign.
    pub port: u16,
    #[serde(default)]
    pub extra_env: Vec<(String, String)>,
    /// Shared PCGUARD map capacity in bytes. The handshake rejects a smaller map.
    #[serde(default = "default_map_size")]
    pub map_size: usize,
    #[serde(default)]
    pub debug_child: bool,
}

/// Values substituted into arguments, environment values, and the config template.
#[derive(Clone, Debug)]
pub struct TargetPlaceholders {
    /// Campaign control port.
    pub port: u16,
    /// Directory the target uses as its root.
    pub root: PathBuf,
    /// Rendered config file.
    pub config: PathBuf,
    /// Certificate file.
    pub cert: PathBuf,
    /// Private key file.
    pub key: PathBuf,
    /// CA bundle.
    pub ca: PathBuf,
}

impl TargetPlaceholders {
    /// Replace `@PORT@`, `@ROOT@`, `@CONFIG@`, `@CERT@`, `@KEY@`, and `@CA@`.
    pub fn apply(&self, text: &str) -> String {
        text.replace("@PORT@", &self.port.to_string())
            .replace("@ROOT@", &self.root.display().to_string())
            .replace("@CONFIG@", &self.config.display().to_string())
            .replace("@CERT@", &self.cert.display().to_string())
            .replace("@KEY@", &self.key.display().to_string())
            .replace("@CA@", &self.ca.display().to_string())
    }
}

/// Corpus, seeds, and the Python interpreter.
#[derive(Clone, Debug, Deserialize)]
pub struct CampaignPaths {
    pub seeds: PathBuf,
    pub corpus: PathBuf,
    pub crashes: PathBuf,
    pub python: PathBuf,
    pub pythonpath: PathBuf,
    /// CPU pin for the forkserver. Omitted means the process is not pinned.
    /// Coverage does not depend on this.
    #[serde(default)]
    pub core: Option<usize>,
    #[serde(default = "default_iterations")]
    pub iterations: u64,
}

/// Opt-in loopback capture. Omitted means capture stays off.
#[derive(Clone, Debug, Deserialize)]
pub struct CaptureConfig {
    /// When false, the executor skips the pcap handle, capture thread, and preflight.
    #[serde(default)]
    pub enabled: bool,
    /// Maximum bytes kept from one case, including the PCAP global header.
    #[serde(default = "default_file_limit")]
    pub file_limit: u64,
    /// Quiet period after the last packet before End completes, in milliseconds.
    #[serde(default = "default_drain_quiet_ms")]
    pub drain_quiet_ms: u64,
    /// Maximum End drain, in milliseconds.
    #[serde(default = "default_drain_timeout_ms")]
    pub drain_timeout_ms: u64,
}

impl Default for CaptureConfig {
    fn default() -> Self {
        Self {
            enabled: false,
            file_limit: default_file_limit(),
            drain_quiet_ms: default_drain_quiet_ms(),
            drain_timeout_ms: default_drain_timeout_ms(),
        }
    }
}

/// One campaign file.
#[derive(Clone, Debug, Deserialize)]
pub struct Campaign {
    pub target: TargetConfig,
    pub timeouts: Timeouts,
    pub campaign: CampaignPaths,
    /// PCAP capture. Missing from the file keeps the historical no-capture path.
    #[serde(default)]
    pub capture: CaptureConfig,
    /// Directory that contained the TOML file. Not read from the file.
    #[serde(skip)]
    pub base: PathBuf,
}

impl Campaign {
    /// Load a TOML campaign and resolve its paths.
    pub fn load(path: &Path) -> Result<Self, Error> {
        let text = fs::read_to_string(path)
            .map_err(|error| Error::unknown(format!("reading {}: {error}", path.display())))?;
        let mut campaign: Campaign = toml::from_str(&text)
            .map_err(|error| Error::serialize(format!("parsing {}: {error}", path.display())))?;
        reject_reserved_target_env(&campaign.target.extra_env)?;
        let base = path
            .parent()
            .unwrap_or_else(|| Path::new("."))
            .to_path_buf();
        campaign.target.binary = resolve(&base, &campaign.target.binary);
        campaign.target.workdir = resolve(&base, &campaign.target.workdir);
        campaign.target.fixture = resolve(&base, &campaign.target.fixture);
        campaign.target.config_template = resolve(&base, &campaign.target.config_template);
        campaign.campaign.seeds = resolve(&base, &campaign.campaign.seeds);
        campaign.campaign.corpus = resolve(&base, &campaign.campaign.corpus);
        campaign.campaign.crashes = resolve(&base, &campaign.campaign.crashes);
        campaign.campaign.python = resolve(&base, &campaign.campaign.python);
        campaign.campaign.pythonpath = resolve(&base, &campaign.campaign.pythonpath);
        campaign.base = base;
        Ok(campaign)
    }

    /// Generated server configuration path.
    pub fn server_config(&self) -> PathBuf {
        self.target.workdir.join(&self.target.config_output)
    }

    /// Certificate copied with the fixture.
    pub fn cert_path(&self) -> PathBuf {
        self.target.fixture.join(&self.target.cert)
    }

    /// Private key copied with the fixture.
    pub fn key_path(&self) -> PathBuf {
        self.target.fixture.join(&self.target.key)
    }

    /// CA bundle. An omitted path keeps the previous LightFTP lookup.
    pub fn ca_path(&self) -> PathBuf {
        if let Some(ca) = &self.target.ca {
            return self.target.fixture.join(ca);
        }
        let system = PathBuf::from("/etc/ssl/certs/ca-certificates.crt");
        if system.is_file() {
            system
        } else {
            self.target.fixture.join("certs").join("my.pem")
        }
    }

    /// Placeholder values for this campaign's fixed port.
    pub fn placeholders(&self, port: u16) -> TargetPlaceholders {
        TargetPlaceholders {
            port,
            root: self.target.workdir.clone(),
            config: self.server_config(),
            cert: self.cert_path(),
            key: self.key_path(),
            ca: self.ca_path(),
        }
    }
}

/// Keys the fuzzer owns. `extra_env` must not set them.
///
/// `AFL_LLVM_ONLY_FSRV` and `AFL_GCC_ONLY_FSRV` disable PCGUARD counters when
/// they are present, including when the value is `0`. `__AFL_SHM_ID`,
/// `__AFL_SHM_ID_SIZE`, and `AFL_MAP_SIZE` select the shared coverage map.
pub fn reject_reserved_target_env(extra_env: &[(String, String)]) -> Result<(), Error> {
    for (key, value) in extra_env {
        if key == "AFL_LLVM_ONLY_FSRV" || key == "AFL_GCC_ONLY_FSRV" {
            return Err(Error::illegal_argument(format!(
                "target.extra_env must not set {key} (got {value:?}). \
                 The variable disables PCGUARD counters whenever it is present, including when it is 0 or 1. \
                 Remove the key."
            )));
        }
        if key == "__AFL_SHM_ID" || key == "__AFL_SHM_ID_SIZE" || key == "AFL_MAP_SIZE" {
            return Err(Error::illegal_argument(format!(
                "target.extra_env must not set {key} (got {value:?}). \
                 The fuzzer allocates the shared coverage map and sets this itself. \
                 Size the allocation with target.map_size; a campaign value would replace the real map."
            )));
        }
    }
    Ok(())
}

fn resolve(base: &Path, path: &Path) -> PathBuf {
    if path.is_absolute() {
        path.to_path_buf()
    } else {
        base.join(path)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sample(extra: &str, core_line: &str) -> String {
        format!(
            r#"
[target]
binary = "b"
workdir = "w"
fixture = "f"
config_template = "c"
port = 0
{extra}
[timeouts]
connect_ms = 1
io_ms = 1
case_ms = 1
exit_grace_ms = 1
[campaign]
seeds = "s"
corpus = "co"
crashes = "cr"
python = "python"
pythonpath = "pp"
{core_line}
"#
        )
    }

    #[test]
    fn omitted_core_does_not_pin() {
        let campaign: Campaign = toml::from_str(&sample("", "")).unwrap();
        assert_eq!(campaign.campaign.core, None);
        let pinned: Campaign = toml::from_str(&sample("", "core = 0")).unwrap();
        assert_eq!(pinned.campaign.core, Some(0));
    }

    #[test]
    fn omitted_launch_fields_keep_lightftp_defaults() {
        let campaign: Campaign = toml::from_str(&sample("", "")).unwrap();
        assert_eq!(campaign.target.config_output, "fftp.conf");
        assert_eq!(campaign.target.args, vec!["@CONFIG@".to_string()]);
        assert_eq!(campaign.target.cert, "certs/my.crt");
        assert_eq!(campaign.target.key, "certs/my.pem");
        assert_eq!(campaign.target.ca, None);
        assert!(!campaign.capture.enabled);
        assert_eq!(campaign.capture.file_limit, 67_108_864);
        assert_eq!(campaign.capture.drain_quiet_ms, 5);
        assert_eq!(campaign.capture.drain_timeout_ms, 100);
        let rendered = campaign.placeholders(2200).apply(&campaign.target.args[0]);
        assert!(rendered.ends_with("fftp.conf"), "{rendered}");
        let conf = campaign
            .placeholders(2200)
            .apply("port=@PORT@ cert=@CERT@ key=@KEY@ ca=@CA@ root=@ROOT@ cfg=@CONFIG@");
        assert!(conf.contains("port=2200"), "{conf}");
        assert!(conf.contains("certs/my.crt"), "{conf}");
        assert!(conf.contains("certs/my.pem"), "{conf}");
        assert!(conf.contains("fftp.conf"), "{conf}");
    }

    #[test]
    fn reserved_env_values_are_rejected() {
        for (key, value) in [
            ("AFL_LLVM_ONLY_FSRV", "0"),
            ("AFL_LLVM_ONLY_FSRV", "1"),
            ("AFL_GCC_ONLY_FSRV", "1"),
            ("__AFL_SHM_ID", "99"),
            ("__AFL_SHM_ID_SIZE", "65536"),
            ("AFL_MAP_SIZE", "65536"),
        ] {
            let text = sample(&format!("extra_env = [[\"{key}\", \"{value}\"]]\n"), "");
            let error = reject_reserved_target_env(
                &toml::from_str::<Campaign>(&text).unwrap().target.extra_env,
            )
            .expect_err(key);
            let message = format!("{error:?}");
            assert!(message.contains(key), "{message}");
        }
    }

    #[test]
    fn load_rejects_forkserver_only_set_to_zero() {
        let dir = std::env::temp_dir().join(format!("mcn-cfg-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).unwrap();
        let path = dir.join("campaign.toml");
        fs::write(
            &path,
            sample("extra_env = [[\"AFL_LLVM_ONLY_FSRV\", \"0\"]]\n", ""),
        )
        .unwrap();
        let error = Campaign::load(&path).expect_err("reserved env");
        let message = format!("{error:?}");
        assert!(message.contains("AFL_LLVM_ONLY_FSRV"), "{message}");
        assert!(message.contains('0'), "{message}");
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn openssl_campaign_points_openssl_conf_at_rendered_config() {
        let campaign =
            Campaign::load(Path::new("targets/openssl/campaign.toml")).expect("openssl campaign");
        let placeholders = campaign.placeholders(4433);
        let value = campaign
            .target
            .extra_env
            .iter()
            .find(|(key, _)| key == "OPENSSL_CONF")
            .map(|(_, value)| placeholders.apply(value));
        let expected = placeholders.config.display().to_string();
        assert_eq!(value.as_deref(), Some(expected.as_str()));
        assert!(expected.ends_with("openssl.cnf"));
        assert!(campaign.target.args.iter().any(|arg| arg == "-naccept"));
    }

    #[test]
    fn ordinary_extra_env_is_kept() {
        let text = sample("extra_env = [[\"MCN_TEST_CRASH\", \"client\"]]\n", "");
        let campaign: Campaign = toml::from_str(&text).unwrap();
        assert!(reject_reserved_target_env(&campaign.target.extra_env).is_ok());
        assert_eq!(
            campaign.target.extra_env,
            vec![("MCN_TEST_CRASH".to_string(), "client".to_string())]
        );
    }
}
