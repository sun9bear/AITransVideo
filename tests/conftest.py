from pathlib import Path
import sys


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_PATH = PROJECT_ROOT / "src"
GATEWAY_PATH = PROJECT_ROOT / "gateway"

if str(SRC_PATH) not in sys.path:
    sys.path.insert(0, str(SRC_PATH))
# Gateway is imported in production with `gateway/` as the working
# directory, so its modules use sibling imports (`from config import
# settings`, etc.). Tests need the same path entry to keep those
# imports resolvable when we exercise gateway modules directly.
if str(GATEWAY_PATH) not in sys.path:
    sys.path.insert(0, str(GATEWAY_PATH))
