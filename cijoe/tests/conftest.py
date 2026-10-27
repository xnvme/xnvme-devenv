import sys
from pathlib import Path

# The scripts import rig.py as cijoe runs them, from their own directory
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
