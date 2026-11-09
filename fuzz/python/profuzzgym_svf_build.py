"""Build ProFuzzGym's pinned subjects as bitcode in existing dependency images."""

import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import socket
import subprocess
import tarfile
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
GYM = ROOT / ".deps/profuzzgym/profuzzbench"
TOOLS = ROOT / ".deps/profuzzgym-svf-tools"
VENV = ROOT / ".venv"
LLVM = Path.home() / "SVF/llvm-21.1.0.obj"


def setup_tools():
    """Make analysis helpers available to the dependency containers."""
    TOOLS.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(__file__).with_name("profuzzgym_svf_container.py"), TOOLS / "container_build.py")
    for name in ["wllvm", "wllvm++", "extract-bc"]:
        wrapper = TOOLS / name
        wrapper.write_text(f'#!/bin/sh\nexec {VENV / "bin" / name} "$@"\n')
        wrapper.chmod(0o755)
    file_tool = shutil.which("file")
    if file_tool is None:
        raise RuntimeError("The host 'file' utility is required by wllvm")
    shutil.copy2(file_tool, TOOLS / "file.bin")
    shutil.copy2("/usr/share/misc/magic.mgc", TOOLS / "magic.mgc")
    libraries = TOOLS / "file-libs"
    libraries.mkdir(exist_ok=True)
    dependencies = subprocess.check_output(["ldd", file_tool], text=True)
    for path in re.findall(r"=> (/\S+)", dependencies):
        if not Path(path).name.startswith("libc.so"):
            shutil.copy2(path, libraries / Path(path).name)
    wrapper = TOOLS / "file"
    wrapper.write_text('#!/bin/sh\nexec env LD_LIBRARY_PATH=/analysis-tools/file-libs '
                       '/analysis-tools/file.bin -m /analysis-tools/magic.mgc "$@"\n')
    wrapper.chmod(0o755)


def docker_host_path(path):
    """Translate paths when the Docker client itself runs in a bind-mounted container."""
    ids = subprocess.check_output(["docker", "ps", "-q"], text=True).split()
    if ids:
        containers = json.loads(subprocess.check_output(["docker", "inspect", *ids], text=True))
        for container in containers:
            if container["Config"].get("Hostname") != socket.gethostname():
                continue
            mounts = sorted(container["Mounts"], key=lambda item: len(item["Destination"]), reverse=True)
            for mount in mounts:
                destination = Path(mount["Destination"])
                if mount["Type"] == "bind" and (path == destination or destination in path.parents):
                    return Path(mount["Source"]) / path.relative_to(destination)
    return path


def execute(command, log, **kwargs):
    subprocess.run([str(item) for item in command], stdout=log,
                   stderr=subprocess.STDOUT, check=True, **kwargs)


def provision(entry, destination, log):
    if destination.exists():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    if entry["kind"] == "git":
        destination.mkdir()
        execute(["git", "-C", destination, "init", "-q"], log)
        execute(["git", "-C", destination, "remote", "add", "origin", entry["url"]], log)
        execute(["git", "-C", destination, "fetch", "--depth=1", "origin",
                 entry["resolved_commit"]], log)
        execute(["git", "-C", destination, "checkout", "--detach", "FETCH_HEAD"], log)
    else:
        archive = destination.parent / "source.tar.gz"
        urls = [entry["url"]]
        if "sourceforge.net" in entry["url"]:
            urls.append(entry["url"].replace("downloads.sourceforge.net/project/",
                        "sourceforge.net/projects/").replace("/bftpd/bftpd-", "/files/bftpd/bftpd-", 1) + "/download")
        error = None
        for url in urls:
            try:
                request = urllib.request.Request(url, headers={"User-Agent": "mcn-fuzzer"})
                with urllib.request.urlopen(request, timeout=120) as response, archive.open("wb") as stream:
                    shutil.copyfileobj(response, stream)
                with archive.open("rb") as stream:
                    digest = hashlib.file_digest(stream, "sha256").hexdigest()
                if digest != entry["sha256"]:
                    raise ValueError(f"Archive SHA256 mismatch: {digest}")
                error = None
                break
            except Exception as exc:
                error = exc
        if error:
            raise error
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            with tarfile.open(archive) as package:
                skipped = []
                def source_filter(member, path):
                    if member.issym() and Path(member.linkname).is_absolute():
                        skipped.append({"path": member.name, "target": member.linkname})
                        return None
                    return tarfile.data_filter(member, path)
                package.extractall(temporary, filter=source_filter)
                if skipped:
                    (destination.parent / "omitted-external-media-links.json").write_text(json.dumps(skipped, indent=2) + "\n")
            children = list(Path(temporary).iterdir())
            source = children[0] if len(children) == 1 and children[0].is_dir() else Path(temporary)
            shutil.copytree(source, destination, symlinks=True)
            for path in destination.rglob("*"):
                if not path.is_symlink():
                    path.chmod(path.stat().st_mode | 0o200)


def build(spec_path, versions, jobs):
    spec = json.loads(spec_path.read_text())
    name = spec["name"]
    out = ROOT / "targets" / name / "profuzzgym-svf"
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "build.json"
    if manifest_path.exists():
        old = json.loads(manifest_path.read_text())
        if old.get("status") == "complete" and (out / "target.bc").is_file():
            print(f"Reusing complete IR: {name}", flush=True)
            return old
    started = time.monotonic()
    entry = versions[spec.get("version_key", name)]
    images = [spec["image"], f"pfbgym-cov-sgfuzz-{name}", f"pfbgym-cov-libaflstar-{name}"]
    image = next((candidate for candidate in images if subprocess.run(
        ["docker", "image", "inspect", candidate], stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL).returncode == 0), None)
    plan = {"name": name, "source": entry, "spec_path": str(spec_path.relative_to(GYM)),
            "spec": spec, "image": image, "make_jobs": jobs,
            "host_uid": os.getuid(), "host_gid": os.getgid(),
            "host_source": str(out / "source"), "status": "building",
            "profuzzgym_commit": subprocess.check_output(
                ["git", "-C", str(GYM.parent), "rev-parse", "HEAD"], text=True).strip(),
            "analysis_adjustments": ["AFL/ASAN/gcov instrumentation removed",
                "Compiler replaced by wllvm with LLVM 21.1.0", "Linked system libraries remain external"]}
    try:
        if image is None:
            raise RuntimeError("No existing dependency image is available")
        plan["image_id"] = subprocess.check_output(
            ["docker", "image", "inspect", image, "--format", "{{.Id}}"], text=True).strip()
        print(f"Building {name} {entry['version']} in {image}", flush=True)
        with (out / "provision.log").open("a") as log:
            provision(entry, out / "source", log)
        (out / "plan.json").write_text(json.dumps(plan, indent=2) + "\n")
        command = ["docker", "run", "--rm", "--pull=never", "--network=none",
                   "--name", f"mcn-svf-build-{name}", "--user", "0:0",
                   "-v", f"{docker_host_path(out)}:/out", "-v", f"{docker_host_path(out / 'source')}:/opt/pfb/src/{entry['dest']}",
                   "-v", f"{docker_host_path(GYM)}:/gym:ro", "-v", f"{docker_host_path(TOOLS)}:/analysis-tools:ro",
                   "-v", f"{docker_host_path(LLVM)}:/analysis-llvm:ro", "-v", f"{docker_host_path(VENV)}:{VENV}:ro",
                   "--entrypoint", "python3", image, "/analysis-tools/container_build.py"]
        with (out / "build.log").open("a") as log:
            execute(command, log)
        with (out / "target.bc").open("rb") as stream:
            plan["bitcode_sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
        plan["status"] = "complete"
    except Exception as error:
        plan.update(status="failed", error=str(error))
    plan["elapsed_seconds"] = round(time.monotonic() - started, 3)
    manifest_path.write_text(json.dumps(plan, indent=2) + "\n")
    print(f"{plan['status']}: {name} ({plan['elapsed_seconds']}s) {plan.get('error', '')}", flush=True)
    return plan


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--targets", nargs="*")
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--jobs", type=int, default=8)
    args = parser.parse_args()
    setup_tools()
    versions = json.loads((GYM / "subjects/versions.json").read_text())["sources"]
    specs = sorted((GYM / "subjects").glob("*/*/subject.json"))
    if args.targets:
        specs = [path for path in specs if json.loads(path.read_text())["name"] in args.targets]
    results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        for result in pool.map(lambda path: build(path, versions, args.jobs), specs):
            results.append({key: result.get(key) for key in ["name", "status", "error", "elapsed_seconds"]})
    inventory = []
    for path in sorted((ROOT / "targets").glob("*/profuzzgym-svf/build.json")):
        record = json.loads(path.read_text())
        inventory.append({key: record.get(key) for key in ["name", "status", "error", "elapsed_seconds"]})
    (ROOT / "targets/profuzzgym-svf-builds.json").write_text(json.dumps(inventory, indent=2) + "\n")
    if any(result["status"] != "complete" for result in results):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
