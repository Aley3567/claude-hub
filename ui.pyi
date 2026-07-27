# Stub file for ui module resolution
import sys as _sys
from pathlib import Path as _Path

# Redirect imports to the correct location
if str(_Path(__file__).parent.parent) not in _sys.path:
    _sys.path.insert(0, str(_Path(__file__).parent.parent))
