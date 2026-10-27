import subprocess
import sys

# In a fresh interpreter outside the tree, so 'import rig' resolves from the
# installed package and not from the checkout conftest.py puts on the path
LOAD = """
from cijoe.core.resources import get_resources
scripts = get_resources()["scripts"]
for name in ("rig_guest_start", "rig_guest_kill", "rig_device_plug", "rig_devices",
             "rig_device_unplug", "rocm_ernic_peer_check"):
    script = scripts[f"xnvme_devenv.{name}"]
    script.load()  # its return value reports an error even when it loads
    assert script.func, name
"""


def test_scripts_load_from_the_installed_package(tmp_path):
    subprocess.run([sys.executable, "-I", "-c", LOAD], cwd=tmp_path, check=True)
