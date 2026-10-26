// SPDX-License-Identifier: BSD-3-Clause
//
// io_uring I/O between a block device and a dma-buf, as the guest kernel's
// io_uring dma-buf series allows: a udmabuf over a memfd is registered as a
// fixed buffer for the device, written to the device with WRITE_FIXED, and
// read back into the zeroed buffer with READ_FIXED. The memfd is the CPU's
// view of the same pages, so the bytes are checked through it.
//
// A stock kernel refuses the registration, and the test fails saying so.
//
//   cc -o iouring_dmabuf iouring_dmabuf.c -luring
//   ./iouring_dmabuf /dev/nvme0n1
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <liburing.h>
#include <linux/udmabuf.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/ioctl.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

#define SZ (1024 * 1024)

// From the series' include/uapi/linux/io_uring.h, which the guest's headers
// predate
#define DEVENV_RSRC_UPDATE_EXTENDED (1U << 1)
#define DEVENV_REGBUF_TYPE_DMABUF 2

struct devenv_rsrc_update2 {
	uint32_t offset;
	uint32_t flags;
	uint64_t data;
	uint64_t tags;
	uint32_t nr;
	uint32_t resv2;
};

struct devenv_regbuf_desc {
	uint32_t type;
	uint32_t flags;
	uint64_t size;
	uint64_t uaddr;
	int32_t dmabuf_fd;
	int32_t target_fd;
	uint64_t resv[6];
};

static int fail(const char *what, int err)
{
	fprintf(stderr, "iouring_dmabuf: %s: %s\n", what, strerror(err));
	return 1;
}

// Submits one fixed-buffer op and waits for it, reissuing on the spurious
// -EAGAIN the series documents
static int io(struct io_uring *ring, int op, int fd)
{
	for (;;) {
		struct io_uring_sqe *sqe = io_uring_get_sqe(ring);
		struct io_uring_cqe *cqe;
		int res;

		if (op == IORING_OP_WRITE_FIXED)
			io_uring_prep_write_fixed(sqe, fd, NULL, SZ, 0, 0);
		else
			io_uring_prep_read_fixed(sqe, fd, NULL, SZ, 0, 0);
		io_uring_submit(ring);
		if (io_uring_wait_cqe(ring, &cqe))
			return -EIO;
		res = cqe->res;
		io_uring_cqe_seen(ring, cqe);
		if (res != -EAGAIN)
			return res;
	}
}

int main(int argc, char **argv)
{
	struct io_uring ring;
	uint8_t *cpu;
	int dev, memfd, udev, dmabuf, err;

	if (argc != 2) {
		fprintf(stderr, "usage: %s <block device>\n", argv[0]);
		return 2;
	}
	dev = open(argv[1], O_RDWR | O_DIRECT);
	if (dev < 0)
		return fail(argv[1], errno);

	memfd = memfd_create("iouring_dmabuf", MFD_ALLOW_SEALING);
	if (memfd < 0 || ftruncate(memfd, SZ) || fcntl(memfd, F_ADD_SEALS, F_SEAL_SHRINK))
		return fail("memfd", errno);
	cpu = mmap(NULL, SZ, PROT_READ | PROT_WRITE, MAP_SHARED, memfd, 0);
	if (cpu == MAP_FAILED)
		return fail("mmap", errno);
	udev = open("/dev/udmabuf", O_RDWR);
	struct udmabuf_create create = {.memfd = memfd, .flags = UDMABUF_FLAGS_CLOEXEC, .size = SZ};
	dmabuf = udev < 0 ? -1 : ioctl(udev, UDMABUF_CREATE, &create);
	if (dmabuf < 0)
		return fail("udmabuf", errno);

	err = io_uring_queue_init(8, &ring, 0);
	if (err)
		return fail("io_uring_queue_init", -err);
	err = io_uring_register_buffers_sparse(&ring, 1);
	if (err)
		return fail("io_uring_register_buffers_sparse", -err);
	// size and uaddr stay zero: the buffer spans the dma-buf, addressed from 0
	struct devenv_regbuf_desc desc = {
		.type = DEVENV_REGBUF_TYPE_DMABUF, .dmabuf_fd = dmabuf, .target_fd = dev};
	struct devenv_rsrc_update2 up = {
		.flags = DEVENV_RSRC_UPDATE_EXTENDED, .data = (uintptr_t)&desc, .nr = 1};
	err = syscall(__NR_io_uring_register, ring.ring_fd, IORING_REGISTER_BUFFERS_UPDATE, &up,
		      sizeof(up));
	if (err != 1)
		return fail("registering the dma-buf (does the kernel carry the series?)",
			    err < 0 ? errno : EINVAL);

	for (size_t i = 0; i < SZ; i++)
		cpu[i] = (uint8_t)(i * 7 + 3 + i / 4096);
	err = io(&ring, IORING_OP_WRITE_FIXED, dev);
	if (err != SZ)
		return fail("WRITE_FIXED", err < 0 ? -err : EIO);
	memset(cpu, 0, SZ);
	err = io(&ring, IORING_OP_READ_FIXED, dev);
	if (err != SZ)
		return fail("READ_FIXED", err < 0 ? -err : EIO);
	for (size_t i = 0; i < SZ; i++) {
		if (cpu[i] != (uint8_t)(i * 7 + 3 + i / 4096)) {
			fprintf(stderr, "iouring_dmabuf: byte %zu differs after the read\n", i);
			return 1;
		}
	}
	printf("iouring_dmabuf: %d bytes through a dma-buf to %s and back: PASS\n", SZ, argv[1]);
	return 0;
}
