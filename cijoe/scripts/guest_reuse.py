#!/usr/bin/env python3
"""
Reuse a published guest made from the same inputs
=================================================

A guest is made from the guest's part of the lock and from what derive runs,
and deriving it is most of what a release takes. This hashes those inputs and, when the registry
has a guest tagged 'inputs-<hash>', stages it as the qcow2 qualify boots, and
records which it staged, so 'make derive' does not derive it again. The hash
is written either way, for the release to tag the guest it publishes with. A
guest that cannot be looked up or fetched is derived, as a new one would be.

Retargetable: false
-------------------
"""

import hashlib
import json
import logging as log
import tomllib
from argparse import ArgumentParser
from pathlib import Path

from cijoe.system_imaging.scripts.diskimage_from_oras import diskimage_from_oras
from withcache import oras

ROOT = Path(__file__).resolve().parents[2]
INPUTS = [
    "guest",
    "cijoe/tasks/derive.yaml",
    "cijoe/scripts/fetch_debs.py",
    "cijoe/scripts/guest_derive.py",
    "cijoe/scripts/guest_export.py",
    "cijoe/scripts/root_unlock.py",
]
SKIPPED = (".pyc", "~", ".swp")


def add_args(parser: ArgumentParser):
    pass


def guest_lock(lock: dict):
    """The part of the lock the guest is made from: the nosi base, the kernel and
    the guest's debs, not the host's"""

    return {
        "base": lock["system-imaging"]["images"]["nosi-base-x86_64"]["oras"]["url"],
        "kernel": lock["lock"]["kernel"],
        "debs": [d for d in lock["lock"]["deb"] if d["side"] == "guest"],
    }


def inputs_digest(root: Path = ROOT):
    """The sha256 of what the guest is made from: the guest's part of the lock,
    and the files derive runs and installs, by path and content. The derive
    guest's own size and the host's tools change no byte of the image."""

    files = []
    for name in INPUTS:
        path = root / name
        if path.is_dir():
            files += sorted(
                p
                for p in path.rglob("*")
                if p.is_file()
                and "__pycache__" not in p.parts
                and not p.name.endswith(SKIPPED)
            )
        else:
            files.append(path)

    lock = tomllib.loads((root / "lock.toml").read_text())
    digest = hashlib.sha256(json.dumps(guest_lock(lock), sort_keys=True).encode())
    for path in files:
        digest.update(str(path.relative_to(root)).encode() + b"\0")
        digest.update(path.read_bytes() + b"\0")

    return digest.hexdigest()


def main(args, cijoe):
    """Stage the published guest made from these inputs, when there is one"""

    digest = inputs_digest()
    inputs = Path(cijoe.getconf("derive.inputs_path"))
    inputs.parent.mkdir(parents=True, exist_ok=True)
    inputs.write_text(f"{digest}\n")
    reused = Path(cijoe.getconf("derive.reused_path"))
    reused.unlink(missing_ok=True)

    url = f"oras://{cijoe.getconf('derive.image')}:inputs-{digest}"
    try:
        layer = oras.resolve_ref(url).digest
    except Exception as exc:
        log.info(f"no guest made from these inputs, {url}: {exc}")
        return 0

    disk = cijoe.getconf("system-imaging.images.xnvme-devenv-guest-x86_64.disk")
    try:
        err = diskimage_from_oras(cijoe, {"oras": {"url": url}, "disk": disk})
    except Exception as exc:
        err = exc
    if err:
        log.info(f"could not stage {url} ({err}); deriving instead")
        return 0

    reused.write_text(f"inputs-{digest}\n{layer}\n")
    log.info(f"staged {url}, layer {layer}")
    return 0
