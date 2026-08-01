"""Make the target importable to its own tests.

The target is treated as an arbitrary codebase, not as part of the orchestrator
package (D3) — so it is not installed. Its tests put it on the path themselves,
exactly as they would in the repository it came from.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
