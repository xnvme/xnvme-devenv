# Assemble and qualify a release from lock.toml. Every step is a cijoe
# workflow taking lock.toml as config; derive and qualify run inside the host
# container, which has QEMU, cijoe and the servers. 'make host ci' is the whole
# of what CI runs, so a failure there reproduces with the same line.

DOCKER ?= docker
HOST_IMAGE ?= xnvme-devenv-host:local
CIJOE_OUTPUT ?= cijoe-output
# Images, guests and server sockets: some 45G, on a disk that has it
WORKDIR ?= $(CURDIR)/.work
# The container writes as root; reading what it left may need sudo
SUDO ?=
CIJOE_CONFIGS = --config ../lock.toml --config configs/images.toml
# The rig qualify boots: rig names, from rigs/, or paths to rig files, each one
# more config, later ones taking precedence, so a file can change one detail
RIG ?= cluster-node-emulated
RIG_CONFIGS = $(foreach r,$(RIG),--config $(if $(findstring /,$(r)),$(abspath $(r)),../rigs/$(r).toml))

.PHONY: host ci logs derive qualify qualify-storage test clean

host:
	cd cijoe && DOCKER="$(DOCKER)" HOST_IMAGE="$(HOST_IMAGE)" cijoe workflows/host.yaml --monitor \
		--config ../lock.toml --output $(CIJOE_OUTPUT)-host

ci:
	mkdir -p $(WORKDIR)
	$(DOCKER) run --rm --privileged -v $(WORKDIR):$(WORKDIR) -v $(CURDIR):/src -w /src \
		-e HOME=$(WORKDIR)/home $(HOST_IMAGE) bash -euxo pipefail -c '\
			mkdir -p "$$HOME/run"; \
			export PATH=$$HOME/.local/bin:$$PATH; \
			pipx install ./cijoe --include-deps --force >/dev/null; \
			make derive qualify qualify-storage RIG="$(RIG)"'

# The ends of the guests' serial consoles and the servers' logs, after a ci run
logs:
	@for f in $(WORKDIR)/home/guests/xnvme-devenv-*/serial.output \
		$(WORKDIR)/home/run/*.log; do \
		echo "::group::$$f (tail)"; \
		$(SUDO) tail -c 20000 "$$f" 2>/dev/null || echo "missing"; \
		echo "::endgroup::"; \
	done

derive:
	cd cijoe && cijoe workflows/derive.yaml --monitor $(CIJOE_CONFIGS) --output $(CIJOE_OUTPUT)-derive

# The unit tests of the cijoe scripts; they need cijoe and pytest, not the host
PYTEST ?= pytest
test:
	cd cijoe && $(PYTEST) -q tests --config configs/images.toml --output $(CIJOE_OUTPUT)-test

qualify:
	cd cijoe && cijoe workflows/qualify.yaml --monitor $(CIJOE_CONFIGS) $(RIG_CONFIGS) --output $(CIJOE_OUTPUT)-qualify

# The storage-node-emulated rig boots with its controllers where consumers expect
qualify-storage:
	cd cijoe && cijoe workflows/qualify-storage.yaml --monitor $(CIJOE_CONFIGS) --config ../rigs/storage-node-emulated.toml --output $(CIJOE_OUTPUT)-qualify-storage

clean:
	rm -rf host/debs cijoe/$(CIJOE_OUTPUT)-* .work
