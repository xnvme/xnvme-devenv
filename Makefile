# Assemble and qualify a release from lock.toml. derive and qualify run inside
# the host container: it has QEMU, cijoe and the servers. 'make host ci' is
# the whole of what CI runs, so a failure there reproduces with the same line.
lock = python3 -c 'import tomllib,sys; print(tomllib.load(open("lock.toml","rb"))["nosi"][sys.argv[1]])'

DOCKER ?= docker
HOST_IMAGE ?= xnvme-images-host:local
CIJOE_OUTPUT ?= cijoe-output
# Images, guests and server sockets: some 45G, on a disk that has it
WORKDIR ?= $(CURDIR)/.work
# The container writes as root; reading what it left may need sudo
SUDO ?=
export XI_GUEST_BASE := $(shell $(lock) guest)

.PHONY: host ci logs guest-debs derive qualify clean

host:
	rm -rf host/debs
	python3 tools/fetch-debs.py lock.toml host host/debs
	$(DOCKER) build -t $(HOST_IMAGE) -f host/Containerfile --build-arg BASE=$$($(lock) host) host

ci:
	mkdir -p $(WORKDIR)
	$(DOCKER) run --rm --privileged -v $(WORKDIR):$(WORKDIR) -v $(CURDIR):/src -w /src \
		-e HOME=$(WORKDIR)/home $(HOST_IMAGE) bash -euxo pipefail -c '\
			mkdir -p "$$HOME/run"; \
			export PATH=$$HOME/.local/bin:$$PATH; \
			pipx install ./cijoe --include-deps --force >/dev/null; \
			make derive qualify'

# The ends of the guests' serial consoles and the servers' logs, after a ci run
logs:
	@for f in $(WORKDIR)/home/guests/xnvme-images-*/serial.output \
		$(WORKDIR)/home/run/vfio-user-*.log; do \
		echo "::group::$$f (tail)"; \
		$(SUDO) tail -c 20000 "$$f" 2>/dev/null || echo "missing"; \
		echo "::endgroup::"; \
	done

guest-debs:
	rm -rf /tmp/xnvme-images/guest-debs
	python3 tools/fetch-debs.py lock.toml guest /tmp/xnvme-images/guest-debs

derive: guest-debs
	cd cijoe && cijoe workflows/derive.yaml --monitor --config configs/images.toml \
		--output $(CIJOE_OUTPUT)-derive

qualify:
	cd cijoe && cijoe workflows/qualify.yaml --monitor --config configs/images.toml \
		--output $(CIJOE_OUTPUT)-qualify

clean:
	rm -rf host/debs cijoe/$(CIJOE_OUTPUT)-* .work
