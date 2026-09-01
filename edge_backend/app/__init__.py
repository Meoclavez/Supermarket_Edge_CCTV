"""Edge AI CCTV Surveillance System Backend Package."""
import sys
from pathlib import Path

_EDGE_BACKEND_DIR = str(Path(__file__).resolve().parent.parent)
if _EDGE_BACKEND_DIR not in sys.path:
    sys.path.insert(0, _EDGE_BACKEND_DIR)

__version__ = "1.0.0"
