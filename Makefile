# Assemble and qualify a release from lock.toml. derive and qualify run inside
# the host container, as CI runs them: it has QEMU, cijoe and the servers.
lock = python3 -c 'import tomllib,sys; print(tomllib.load(open("lock.toml","rb"))["nosi"][sys.argv[1]])'

DOCKER ?= docker
HOST_IMAGE ?= xnvme-images-host:local
CIJOE_OUTPUT ?= cijoe-output
export XI_GUEST_BASE := $(shell $(lock) guest)

.PHONY: host guest-debs derive qualify clean

host:
	rm -rf host/debs
	python3 tools/fetch-debs.py lock.toml host host/debs
	$(DOCKER) build -t $(HOST_IMAGE) --build-arg BASE=$$($(lock) host) host

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
	rm -rf host/debs cijoe/$(CIJOE_OUTPUT)-*
