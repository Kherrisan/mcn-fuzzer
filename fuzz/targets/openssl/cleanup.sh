#!/bin/sh
set -eu
campaign_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
rm -rf "$MCN_FUZZ_WORKDIR/certs"
mkdir -p "$MCN_FUZZ_WORKDIR/certs"
cp "$campaign_dir/build/fixture/certs/server.crt" "$MCN_FUZZ_WORKDIR/certs/server.crt"
cp "$campaign_dir/build/fixture/certs/server.key" "$MCN_FUZZ_WORKDIR/certs/server.key"
