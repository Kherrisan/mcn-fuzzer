//! Acceptance scenarios, each executed in a separate supervised namespace.

mod common;
mod lightftp;
mod openssl;

use crate::app::config::Campaign;
use libafl::Error;

/// Scenarios that each run in a fresh supervise/PID+NET+MNT namespace.
///
/// LightFTP opens three sessions. OpenSSL keeps its single session in one job.
pub fn accept_scenarios(campaign: &Campaign) -> &'static [&'static str] {
    if campaign.target.args.iter().any(|arg| arg == "s_server") {
        &["openssl"]
    } else {
        &["main", "hang", "crash"]
    }
}

/// Run one acceptance scenario. The process is already inside its namespace.
pub fn accept_job(campaign: &Campaign, scenario: &str) -> Result<(), Error> {
    let scenarios = accept_scenarios(campaign);
    if !scenarios.iter().any(|item| *item == scenario) {
        return Err(Error::unknown(format!(
            "accept job {scenario} does not belong to this campaign ({})",
            scenarios.join(", ")
        )));
    }
    note_accept_namespace(scenario);
    match scenario {
        "openssl" => openssl::accept_openssl(campaign),
        "main" => lightftp::accept_lightftp_main(campaign),
        "hang" => lightftp::accept_lightftp_hang(campaign),
        "crash" => lightftp::accept_lightftp_crash(campaign),
        _ => Err(Error::unknown(format!(
            "accept job {scenario} is not implemented"
        ))),
    }
}

fn note_accept_namespace(scenario: &str) {
    println!(
        "accept job {scenario}: pid={} pidns={} netns={} mntns={}",
        std::process::id(),
        namespace_name("pid"),
        namespace_name("net"),
        namespace_name("mnt")
    );
}

fn namespace_name(kind: &str) -> String {
    std::fs::read_link(format!("/proc/self/ns/{kind}"))
        .map(|path| path.display().to_string())
        .unwrap_or_else(|error| format!("unreadable:{error}"))
}

#[cfg(test)]
mod tests {
    use std::path::Path;

    use super::{accept_job, accept_scenarios};
    use crate::app::config::Campaign;

    #[test]
    fn accept_jobs_route_lightftp_separately_and_openssl_as_one() {
        let lightftp = Campaign::load(Path::new("evaluation/fixtures/lightftp/campaign.toml")).unwrap();
        assert_eq!(accept_scenarios(&lightftp), ["main", "hang", "crash"]);
        let openssl = Campaign::load(Path::new("targets/openssl/campaign.toml")).unwrap();
        assert_eq!(accept_scenarios(&openssl), ["openssl"]);
        let wrong = accept_job(&lightftp, "openssl").unwrap_err();
        assert!(
            format!("{wrong:?}").contains("does not belong"),
            "{wrong:?}"
        );
        let wrong = accept_job(&openssl, "hang").unwrap_err();
        assert!(
            format!("{wrong:?}").contains("does not belong"),
            "{wrong:?}"
        );
        let wrong = accept_job(&lightftp, "missing").unwrap_err();
        assert!(
            format!("{wrong:?}").contains("does not belong"),
            "{wrong:?}"
        );
    }
}
