# LightFTP test model

This name-based protocol fixture contains the 24 steps exercised by the five
cases in `evaluation/lightftp.toml`, including their message fields, channels,
and dependencies. It is an incomplete test model without publication source
snapshots. It replaces tests' dependency on ignored campaign-local protocol
output. Tests still exercise the actual built LightFTP server and preserve
campaign-file immutability, trace ordering, artifact validation, and replay
assertions. The campaign fixture resolves its paths to the existing target
build and Python environment.
