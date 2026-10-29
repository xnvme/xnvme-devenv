import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

import guest_reuse
from cijoe.core.command import Cijoe
from cijoe.core.resources import Config


LOCK = """
[system-imaging.images.nosi-base-x86_64]
oras.url = "oras://ghcr.io/safl/nosi/base@sha256:aa"
[lock.nosi]
host = "{host}"
[lock.kernel]
release = "7.3.0-xnvme2"
[[lock.deb]]
side = "host"
url = "https://x/{host}.deb"
[[lock.deb]]
side = "guest"
url = "https://x/{guest}.deb"
"""


def tree(root: Path):
    """A checkout with the guest's inputs, and files that are not ones"""

    (root / "lock.toml").write_text(LOCK.format(host="h1", guest="g1"))
    for name in guest_reuse.INPUTS:
        path = root / name
        if name == "guest":
            (path / "tests").mkdir(parents=True)
            (path / "tests" / "check").write_text("check\n")
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(f"{name}\n")
    (root / "rigs").mkdir()
    (root / "rigs" / "a.toml").write_text("rig\n")

    return root


def test_inputs_digest_follows_the_inputs_alone(tmp_path):
    root = tree(tmp_path)
    digest = guest_reuse.inputs_digest(root)
    assert digest == guest_reuse.inputs_digest(root)

    (root / "rigs" / "a.toml").write_text("another rig\n")
    assert guest_reuse.inputs_digest(root) == digest, "a rig is not an input"

    (root / "guest" / "tests" / "check").write_text("another check\n")
    assert guest_reuse.inputs_digest(root) != digest

    (root / "guest" / "tests" / "check").write_text("check\n")
    (root / "guest" / "tests" / "check~").write_text("an editor's backup\n")
    assert guest_reuse.inputs_digest(root) == digest, "a backup is not an input"

    (root / "lock.toml").write_text(LOCK.format(host="h2", guest="g1"))
    assert guest_reuse.inputs_digest(root) == digest, "the host's pins are not"

    (root / "lock.toml").write_text(LOCK.format(host="h1", guest="g2"))
    assert guest_reuse.inputs_digest(root) != digest


def test_the_makefile_and_the_config_name_the_same_files():
    makefile = (guest_reuse.ROOT / "Makefile").read_text()
    images = tomllib.loads(
        (guest_reuse.ROOT / "cijoe" / "configs" / "images.toml").read_text()
    )
    for var, key in (("DERIVE_REUSED", "reused_path"), ("DERIVE_INPUTS", "inputs_path")):
        line = next(ln for ln in makefile.splitlines() if ln.startswith(f"{var} = "))
        made = line.split(" = ", 1)[1].replace("$(HOME)", "{{ local.env.HOME }}")
        assert made == images["derive"][key], var


def test_inputs_are_files_of_the_checkout():
    for name in guest_reuse.INPUTS:
        assert (guest_reuse.ROOT / name).exists(), name


@pytest.fixture
def cijoe(tmp_path):
    config = Config(Path("images.toml"))
    disk = tmp_path / "disk"
    config.options = {
        "derive": {
            "image": "ghcr.io/x/guest",
            "inputs_path": str(disk / "guest.inputs"),
            "reused_path": str(disk / "guest.reused"),
        },
        "system-imaging": {
            "images": {
                "xnvme-devenv-guest-x86_64": {"disk": {"path": str(disk / "g.qcow2")}}
            }
        },
    }
    cijoe = Cijoe(config, tmp_path / "output", False)
    cijoe.set_output_ident("test")
    return cijoe


def paths(cijoe):
    derive = cijoe.config.options["derive"]
    return Path(derive["inputs_path"]), Path(derive["reused_path"])


def test_without_a_published_guest_it_derives(cijoe, monkeypatch):
    def missing(url):
        raise RuntimeError("manifest unknown")

    monkeypatch.setattr(guest_reuse.oras, "resolve_ref", missing)
    inputs, reused = paths(cijoe)
    reused.parent.mkdir(parents=True)
    reused.write_text("left by an earlier run\n")

    assert guest_reuse.main(None, cijoe) == 0
    assert inputs.read_text().strip() == guest_reuse.inputs_digest()
    assert not reused.exists()


def test_a_published_guest_is_staged_and_recorded(cijoe, monkeypatch):
    staged = []
    monkeypatch.setattr(
        guest_reuse.oras, "resolve_ref", lambda url: SimpleNamespace(digest="sha256:ab")
    )
    monkeypatch.setattr(
        guest_reuse,
        "diskimage_from_oras",
        lambda cijoe, image: staged.append(image) or 0,
    )
    inputs, reused = paths(cijoe)

    assert guest_reuse.main(None, cijoe) == 0
    tag = f"inputs-{guest_reuse.inputs_digest()}"
    assert staged[0]["oras"]["url"] == f"oras://ghcr.io/x/guest:{tag}"
    assert staged[0]["disk"]["path"].endswith("g.qcow2")
    assert reused.read_text() == f"{tag}\nsha256:ab\n"


def test_a_guest_that_fails_to_stage_is_derived(cijoe, monkeypatch):
    monkeypatch.setattr(
        guest_reuse.oras, "resolve_ref", lambda url: SimpleNamespace(digest="sha256:ab")
    )
    monkeypatch.setattr(guest_reuse, "diskimage_from_oras", lambda cijoe, image: 5)
    _, reused = paths(cijoe)

    assert guest_reuse.main(None, cijoe) == 0
    assert not reused.exists()
