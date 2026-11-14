"""Copy OpenSSL 4.0.2 sources and prune the CLI to s_server and built-in help.

The pruned tree keeps only ``openssl.c``, ``s_server.c`` and a support file so
LLVM/SVF graph extraction sees the TLS server entry points without the rest of
the command-line tool. The generated ``s_server_prune.patch`` is meant to be
applied with ``patch -p1`` to a pristine 4.0.2 tree.
"""

import argparse
import difflib
from pathlib import Path
import shutil


ROOT = Path(__file__).resolve().parents[1]
VERSION = "4.0.2"
SOURCE = ROOT / ".deps/openssl" / f"openssl-{VERSION}"
COPY = ROOT / ".deps/openssl" / f"openssl-{VERSION}-s-server-only"

SUPPORT_PREAMBLE = """/*
 * Copyright 2001-2026 The OpenSSL Project Authors. All Rights Reserved.
 *
 * Licensed under the Apache License 2.0 (the "License").  You may not use
 * this file except in compliance with the License.  You can obtain a copy
 * in the file LICENSE in the source distribution or at
 * https://www.openssl.org/source/license.html
 */

#include "apps.h"
#include <openssl/ocsp.h>

#include <openssl/ssl.h>

"""


def cut(text, start_marker, end_marker, replacement):
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[:start] + replacement + text[end:]


def prune_build_info(text):
    return cut(
        text,
        "# Source for the 'openssl' program",
        "IF[{- !$disabled{apps} -}]",
        "# Retain s_server and the CLI's built-in help only.\n"
        "$OPENSSLSRC=openssl.c s_server.c s_server_support.c\n\n",
    )


def prune_progs(text):
    return cut(
        text,
        "    my %md_disabler = (",
        '    print "    {0, NULL, NULL, NULL, NULL}',
        "",
    )


def prune_openssl_c(text):
    # 4.0.2 resolves digest/cipher names through EVP_MD_fetch/EVP_CIPHER_fetch
    # and dispatches to dgst_main/enc_main. Neither command is linked after the
    # prune, so drop the fallback instead of keeping dead references.
    return cut(
        text,
        "    if (fp == NULL) {\n        EVP_MD *md = NULL;",
        "    if (fp != NULL) {",
        "",
    )


def support_file(source):
    client = (source / "apps/s_client.c").read_text()
    constants = "\n".join(
        line for line in client.splitlines()
        if line.startswith("const unsigned char tls13_aes")
    )
    ocsp = (source / "apps/ocsp.c").read_text()
    function_start = ocsp.index("OCSP_RESPONSE *process_responder(")
    function_end = ocsp.index("\n}\n", function_start) + 3
    helper = (
        SUPPORT_PREAMBLE
        + constants
        + "\n\n"
        + "#if !defined(OPENSSL_NO_OCSP) && !defined(OPENSSL_NO_SOCK)\n"
        + ocsp[function_start:function_end]
        + "#endif\n"
    )
    return helper


def prune_source(source, destination, patch_path=None):
    source = source.resolve(strict=True)
    destination = destination.resolve()
    if source == destination or source in destination.parents or destination in source.parents:
        raise ValueError("Source and destination must be separate, non-nested directories")
    if not destination.exists():
        shutil.copytree(source, destination, symlinks=True)

    patch = []
    transforms = {
        "apps/build.info": prune_build_info,
        "apps/progs.pl": prune_progs,
        "apps/openssl.c": prune_openssl_c,
    }
    for name, transform in transforms.items():
        original = (source / name).read_text()
        modified = transform(original)
        (destination / name).write_text(modified)
        patch.extend(
            difflib.unified_diff(
                original.splitlines(True),
                modified.splitlines(True),
                fromfile=f"a/{name}",
                tofile=f"b/{name}",
            )
        )

    helper = support_file(source)
    (destination / "apps/s_server_support.c").write_text(helper)
    patch.extend(
        difflib.unified_diff(
            [],
            helper.splitlines(True),
            fromfile="/dev/null",
            tofile="b/apps/s_server_support.c",
        )
    )

    patch_path = patch_path or destination / "s_server_prune.patch"
    patch_path.parent.mkdir(parents=True, exist_ok=True)
    patch_path.write_text("".join(patch))
    return destination, patch_path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=SOURCE,
                        help=f"Original OpenSSL {VERSION} source directory")
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
