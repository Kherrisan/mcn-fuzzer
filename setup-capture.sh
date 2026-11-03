#!/usr/bin/env bash
# Check capture dependencies and build a normal-user Release binary.
# Does not install files, change capabilities, or write outside the repo.
set -euo pipefail

die() { printf 'error: %s\n' "$*" >&2; exit 1; }

[[ "$(uname -s)" == Linux ]] || die 'Linux is required.'
[[ -r /proc/self/status ]] || die 'Linux /proc is required.'
command -v pkg-config >/dev/null 2>&1 || die 'pkg-config is required.'
command -v cargo >/dev/null 2>&1 || die 'cargo is required.'
pkg-config --exists libpcap || die 'libpcap is not installed (pkg-config cannot find it).'
printf 'libpcap %s\n' "$(pkg-config --modversion libpcap)"

project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
(
  cd "$project_dir/fuzz"
  cargo build --offline --release --features stats
)
printf 'built %s\n' "$project_dir/fuzz/target/release/mcn-fuzz"
printf 'start it with sudo so namespaces and libpcap can be opened, for example:\n'
printf '  sudo %s run --campaign targets/lightftp/campaign.toml\n' "$project_dir/fuzz/target/release/mcn-fuzz"
