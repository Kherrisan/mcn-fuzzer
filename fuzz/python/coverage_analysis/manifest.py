"""Four-field build manifest. Paths stay in the campaign TOML.

``source_sha256`` is :func:`source_digest` of the analysis source directory.
The build script and the fuzzer both recompute that digest from
``target.source_dir``. A mismatch rejects the build; source-derived caches
store the digest and miss when it changes.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def source_digest(source_dir):
    """SHA-256 of the regular files under ``source_dir``.

    Relative paths use ``/`` and are sorted by their UTF-8 bytes. Symlinks and
    directories are omitted, and directory symlinks are not followed. Each file
    updates the hash with ``uint64be(path_len) || path || uint64be(data_len) ||
    data``. An empty directory is the SHA-256 of no updates.
    """
    root = Path(source_dir)
    if not root.is_dir():
        raise SystemExit(f"source directory {root} is not a directory")
    files = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        kept = []
        for name in dirnames:
            if not (Path(dirpath) / name).is_symlink():
                kept.append(name)
        dirnames[:] = kept
        for name in filenames:
            path = Path(dirpath) / name
            if path.is_symlink() or not path.is_file():
                continue
            relative = path.relative_to(root).as_posix().encode()
            files.append((relative, path))
    digest = hashlib.sha256()
    for relative, path in sorted(files, key=lambda item: item[0]):
        data = path.read_bytes()
        digest.update(len(relative).to_bytes(8, "big"))
        digest.update(relative)
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def check_source(manifest_path, source_dir):
    """Return the source digest, or exit when it differs from the manifest."""
    try:
        manifest = load_manifest(manifest_path)
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise SystemExit(f"cannot read manifest: {error}") from error
    actual = source_digest(source_dir)
    expected = manifest["source_sha256"]
    if actual != expected:
        raise SystemExit(
            "source tree does not match manifest source_sha256\n"
            f"manifest {expected}\n"
            f"source   {actual}"
        )
    return actual


def graphs_digest(root, relative_paths):
    digest = hashlib.sha256()
    for relative in sorted(relative_paths):
        raw = relative.encode()
        digest.update(len(raw).to_bytes(8, "big"))
        digest.update(raw)
        file_digest = sha256_file(Path(root) / relative).encode()
        digest.update(len(file_digest).to_bytes(8, "big"))
        digest.update(file_digest)
    return digest.hexdigest()


def write_manifest(path, binary, source_dir, graph_root, graph_paths):
    payload = {
        "binary_sha256": sha256_file(binary),
        "source_sha256": source_digest(source_dir),
        "graphs_sha256": graphs_digest(graph_root, graph_paths),
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n")
    temporary.replace(path)
    return payload


def load_manifest(path):
    payload = json.loads(Path(path).read_text())
    expected = {"binary_sha256", "source_sha256", "graphs_sha256"}
    if set(payload) != expected:
        raise ValueError("manifest must contain binary, source, and graphs digests")
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-source", action="store_true")
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--manifest", type=Path)
    args = parser.parse_args(argv)
    if not args.check_source or args.source_dir is None or args.manifest is None:
        parser.error("--check-source, --source-dir, and --manifest are required")
    print(check_source(args.manifest, args.source_dir))


if __name__ == "__main__":
    main()
