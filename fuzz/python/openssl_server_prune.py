"""Copy OpenSSL 3.5.9 sources and prune the CLI to s_server and built-in help."""

import argparse
import difflib
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / ".deps/openssl/openssl-3.5.9"
COPY = ROOT / ".deps/openssl/openssl-3.5.9-s-server-only"


def prune_source(source, destination, patch_path=None):
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must be separate, non-nested directories")
    if not destination.exists():
        shutil.copytree(source, destination, symlinks=True)
    patch = []
    for name in ("apps/build.info", "apps/progs.pl", "apps/openssl.c"):
        original = (source / name).read_text()
        modified = original
        if name.endswith("build.info"):
            start = modified.index("# Source for the 'openssl' program")
            end = modified.index("IF[{- !$disabled{apps} -}]", start)
            modified = (modified[:start]
                        + "# Retain s_server and the CLI's built-in help only.\n"
                        + "$OPENSSLSRC=openssl.c s_server.c s_server_support.c\n\n"
                        + modified[end:])
        elif name.endswith("progs.pl"):
            start = modified.index("    my %md_disabler = (")
            end = modified.index('    print "    {0, NULL, NULL, NULL, NULL}', start)
            modified = modified[:start] + modified[end:]
        else:
            start = modified.index("    if (fp == NULL) {\n        if (EVP_get_digestbyname")
            end = modified.index("    if (fp != NULL) {", start)
            modified = modified[:start] + modified[end:]
        (destination / name).write_text(modified)
        patch.extend(difflib.unified_diff(original.splitlines(True),
                                         modified.splitlines(True),
                                         fromfile=f"a/{name}", tofile=f"b/{name}"))
    # Retain s_server's shared definitions verbatim, without linking either
    # the s_client or OCSP CLI implementation.
    ocsp = (source / "apps/ocsp.c").read_text()
    function_start = ocsp.index("OCSP_RESPONSE *process_responder(")
    function_end = ocsp.index("\n}\n", function_start) + 3
    client = (source / "apps/s_client.c").read_text()
    constants = "\n".join(line for line in client.splitlines()
                          if line.startswith("const unsigned char tls13_aes"))
    helper = (ocsp[:ocsp.index("#include")]
              + '#include "apps.h"\n#include <openssl/ocsp.h>\n\n'
              + '#include <openssl/ssl.h>\n\n' + constants + "\n\n"
              + "#if !defined(OPENSSL_NO_OCSP) && !defined(OPENSSL_NO_SOCK)\n"
              + ocsp[function_start:function_end] + "#endif\n")
    (destination / "apps/s_server_support.c").write_text(helper)
    patch.extend(difflib.unified_diff([], helper.splitlines(True),
                                     fromfile="/dev/null", tofile="b/apps/s_server_support.c"))
    patch_path = patch_path or destination / "s_server_prune.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("".join(patch))
    return destination, patch_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE,
                        help="Original, unmodified OpenSSL source directory")
    parser.add_argument("--output", type=Path, default=COPY,
                        help="Destination directory for the pruned source copy")
    parser.add_argument("--patch", type=Path,
                        help="Patch file path (default: OUTPUT/s_server_prune.patch)")
    args = parser.parse_args()
    destination, patch_path = prune_source(args.source, args.output, args.patch)
    print(f"Pruned sources: {destination}")
    print(f"Patch: {patch_path}")


if __name__ == "__main__":
    main()
