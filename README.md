# xnvme-devenv

[![ci](https://github.com/xnvme/xnvme-devenv/actions/workflows/ci.yml/badge.svg)](https://github.com/xnvme/xnvme-devenv/actions/workflows/ci.yml)
[![version](https://img.shields.io/github/v/tag/xnvme/xnvme-devenv?label=version&sort=semver)](https://github.com/xnvme/xnvme-devenv/tags)
[![host](https://img.shields.io/badge/ghcr.io-host-blue?logo=docker)](https://github.com/orgs/xnvme/packages/container/package/xnvme-devenv%2Fhost)
[![guest](https://img.shields.io/badge/ghcr.io-guest--ubuntu--2610-blue?logo=docker)](https://github.com/orgs/xnvme/packages/container/package/xnvme-devenv%2Fguest-ubuntu-2610)
[![license](https://img.shields.io/badge/license-BSD--3--Clause-blue.svg)](LICENSE)
[![REUSE](https://img.shields.io/badge/REUSE-compliant-green.svg)](REUSE.toml)

Development and test environments for xNVMe and the projects built on it,
used the same way locally and in CI, released in matched pairs: a guest image
and the host container that serves its emulated devices, under one tag.

| Image | What |
| ----- | ---- |
| guest | nosi's `ubuntu-2610-rocm` booting our kernel, 7.3 with the io_uring dma-buf series, and the guest drivers built for it by DKMS: `ionic-ernic`, the amdgpu GC 12.1 fixes and upcie's modules, plus the rocjitsu firmware stubs and options |
| host  | nosi's `ubuntu-2604-docker`, which has stock QEMU and cijoe, with the vfio-user servers: rocm-ernic, rocjitsu, vfu_kvssd |

How xNVMe uses these environments, in CI and to reproduce a CI job on your
own machine, is in xNVMe's documentation:
[Continuous Integration](https://xnvme.io/contributing/ci/index.html).

The two are released together because what breaks silently crosses between
them: the rocm-ernic server and its guest driver, rocjitsu and its firmware,
our kernel and the modules built for it. xNVMe pins one tag for both.

Nothing is built here from source. The pieces are debs released by their
producers, listed by URL and sha256 in one lock file: the `xnvme-ci/*` tags
of the safl forks of rocm-ernic, rocm-systems and linux, and the
releases of upcie and vfio-user-kvssd. `lock.toml` is a cijoe config, so the
workflows read the pins from it directly. A release boots the guest under the host container and
qualifies it, a GEMM on the emulated GPU and NIC to VRAM DMA among the
checks, before either image is published.

The guest is for CI only and is never flashed onto a machine, so it may carry
emulation settings that nosi must not. Machines that cannot reboot into a
new kernel install the producers' DKMS debs on their own kernel instead.

## Rigs

A rig is what a guest boots with: its PCIe topology and the devices on it.
QEMU emulates some devices, and the host's servers serve others over
vfio-user. Each rig is a cijoe config under `rigs/`, shipped in the host
container at `/opt/xnvme-devenv/rigs`. The `rig_*` scripts in `cijoe/scripts`
start a guest with a rig, plug and unplug its devices, and kill it with its
servers. `cijoe/scripts/rig.py` describes the format. The scripts install as
the cijoe package `xnvme_devenv`, with the devbind and iommu they pass devices
through with, so a consumer adds them to its cijoe with
`pipx inject <venv> /opt/xnvme-devenv/cijoe` in the host container, or from a
checkout on any other host, and uses them as `xnvme_devenv.rig_guest_start` and
so on. The guest is the consumer's own, Linux, FreeBSD or Windows, as its
config describes it; the rig adds its topology and devices. Once the guest is up,
cijoe's `core.facts_gather` records what the guest has as facts, its NVMe
controllers, GPUs and NICs among them, under the guest's target name; the
consumer's steps read them as `{{ target.nvme }}`, `{{ target.gpu }}` and
`{{ target.nic }}`, and a later run with `--facts <output>/facts.toml`. A
consumer finds the device it wants by what the rig fixes, an NVMe controller's
serial or a function's BDF, and pairs it with the device's labels from the rig,
so its device list does not depend on how the guest numbers its devices.

A rig is named for the machine it stands in for and where its devices come
from, emulated or passed through. `cluster-node-emulated` has an emulated GPU,
an emulated RDMA NIC and an NVMe drive behind one PCIe switch, and a release
is qualified on it. `storage-node-emulated` has the NVMe controllers xNVMe's
QEMU matrix tests, one per command set and format, each at a fixed BDF.
`compute-node-emulated` has the GPU and one NVMe controller behind a switch,
and `make qualify-compute` qualifies it.
`rdma-node-emulated` has the RDMA NIC alone, its far end an NVMe/RDMA target.
`storage-node-passthru` passes the machine's own NVMe drive through with
vfio-pci; `PASSTHRU=<bdf>` names the drive, or a config of the machine's
gives its BDF, and, to have it checked, its serial; the host container needs
`--privileged --ulimit memlock=-1`. CI has no drive to pass through, so
`make qualify-passthru PASSTHRU=<bdf>` qualifies the rig on a machine that has
one.
`make qualify-cluster RIG=...` takes rig names or paths to rig files. Each is
one more config, and later ones win, so a file of your own can change one
detail of a rig without copying it.

Each NVMe transport has a report of its own: `make qualify-pcie` checks the
storage rig's controllers over PCIe, with the guest's vIOMMU and without;
`make qualify-tcp` exports one of them through the guest kernel's NVMe/TCP
target and reads it back, as xNVMe's tcp-linux tests do, and checks what
xNVMe's tcp-spdk tests need of the guest; and `make qualify-rdma` connects to
the RDMA rig's target.

## Making a release

`make host ci WORKDIR=<dir>` builds the host container, then derives and
qualifies the guest inside it, each a cijoe workflow under `cijoe/workflows`
with its own report; `make host` needs cijoe installed (`pipx install
./cijoe --include-deps`). It is the same line `.github/workflows/ci.yml` runs
on every push, so a CI failure reproduces locally with it. `<dir>` needs some
45G free; `make logs WORKDIR=<dir>` prints the ends of the guests' consoles
and the servers' logs afterwards.

A release is a commit on main that changes `VERSION`, with the subject
`ver: bump to vX.Y.Z`. When CI on main is green and no tag `vX.Y.Z` exists
yet, the same run publishes the host as
`ghcr.io/xnvme/xnvme-devenv/host:vX.Y.Z` and the guest, with the lock it was
made from, as `ghcr.io/xnvme/xnvme-devenv/guest-ubuntu-2610:vX.Y.Z`, and
creates the tag with a GitHub release that names both and the guest's layer
digest to pin, with the lock, the guest image's sha256 and the cijoe reports
of the run attached. Nobody pushes a tag by hand.

## Licensing

BSD-3-Clause (`LICENSE`), except where a file says otherwise:
`guest/tests/rdma_p2p.hip` is GPL-2.0-or-later, following rocm-ernic's tests.
