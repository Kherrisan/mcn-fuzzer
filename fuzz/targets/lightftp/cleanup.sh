#!/bin/sh
set -eu
campaign_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
rm -rf "$MCN_FUZZ_WORKDIR"/* "$MCN_FUZZ_WORKDIR"/.[!.]* "$MCN_FUZZ_WORKDIR"/..?* 2>/dev/null || true
cp -a "$campaign_dir/fixture/." "$MCN_FUZZ_WORKDIR/"
sed \
  -e "s|@PORT@|$MCN_FUZZ_PORT|g" \
  -e "s|@ROOT@|$MCN_FUZZ_WORKDIR|g" \
  -e 's|@CA@|certs/my.pem|g' \
  -e 's|@CERT@|certs/my.crt|g' \
  -e 's|@KEY@|certs/my.key|g' \
  "$campaign_dir/fftp.conf" > "$MCN_FUZZ_WORKDIR/fftp.conf"
