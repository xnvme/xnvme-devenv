# xnvme-devenv

Development and test environments for xNVMe and the projects built on it,
used the same way locally and in CI, released in matched pairs: a guest image
and the host container that serves its emulated devices, under one tag.

| Image | What |
| ----- | ---- |
| guest | nosi's `ubuntu-2610-rocm` booting our kernel, 7.3 with the io_uring dma-buf series, and the guest drivers built for it by DKMS: `ionic-ernic`, the amdgpu GC 12.1 fixes and upcie's modules, plus the rocjitsu firmware stubs and options |
| host  | nosi's `ubuntu-2604-docker`, which has stock QEMU and cijoe, with the vfio-user servers: rocm-ernic, rocjitsu, vfu_kvssd |

The two are released together because what breaks silently crosses between
them: the rocm-ernic server and its guest driver, rocjitsu and its firmware,
our kernel and the modules built for it. xNVMe pins one tag for both.

Nothing is built here from source. The pieces are debs released by their
producers, listed by URL and sha256 in one lock file: the `xnvme-ci/*` tags
of the safl forks of rocm-ernic, rocm-systems and linux, and the
releases of upcie and vfio-user-kvssd. A release boots the guest under the host container and
qualifies it, a GEMM on the emulated GPU and NIC to VRAM DMA among the
checks, before either image is published.

The guest is for CI only and is never flashed onto a machine, so it may carry
emulation settings that nosi must not. Machines that cannot reboot into a
new kernel install the producers' DKMS debs on their own kernel instead.

## Making a release

`make host ci WORKDIR=<dir>` builds the host container, then derives and
qualifies the guest inside it, the same line `.github/workflows/ci.yml` runs
on every push, so a CI failure reproduces locally with it. `<dir>` needs some
45G free; `make logs WORKDIR=<dir>` prints the ends of the guests' consoles
and the servers' logs afterwards. A `v*` tag publishes the
host as `ghcr.io/xnvme/xnvme-devenv/host:<tag>` and the guest, with the
lock it was made from, as `ghcr.io/xnvme/xnvme-devenv/guest-ubuntu-2610:<tag>`.

## Licensing

BSD-3-Clause (`LICENSE`), except where a file says otherwise:
`guest/tests/rdma_p2p.hip` is GPL-2.0-or-later, following rocm-ernic's tests.
