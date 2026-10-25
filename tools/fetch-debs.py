#!/usr/bin/env python3
"""
Download the debs lock.toml pins for one side, host or guest, and check each
against its sha256; a mismatch is fatal, and nothing unchecked is left behind.

    fetch-debs.py <lock.toml> <host|guest> <output dir>
"""
import hashlib
import sys
import tomllib
import urllib.parse
import urllib.request
from pathlib import Path


def main(lock, side, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    debs = [d for d in tomllib.loads(Path(lock).read_text())["deb"] if d["side"] == side]
    if not debs:
        sys.exit(f"fetch-debs: no {side} debs in {lock}")
    for deb in debs:
        name = urllib.parse.unquote(deb["url"].rsplit("/", 1)[1])
        dst = out / name
        with urllib.request.urlopen(deb["url"], timeout=300) as resp:
            data = resp.read()
        digest = hashlib.sha256(data).hexdigest()
        if digest != deb["sha256"]:
            sys.exit(f"fetch-debs: {name}: sha256 {digest}, lock.toml pins {deb['sha256']}")
        dst.write_bytes(data)
        print(dst)


if __name__ == "__main__":
    if len(sys.argv) != 4 or sys.argv[2] not in ("host", "guest"):
        sys.exit(__doc__)
    main(*sys.argv[1:])
