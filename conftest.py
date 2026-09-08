"""
conftest.py

Adds the project root to sys.path so pytest can import `agent.*` modules
without requiring a pip install -e . or PYTHONPATH manipulation.
This is the standard pytest pattern for non-installed packages.
"""
import sys
from pathlib import Path

# Insert the directory containing `agent/`, `eval/`, and `tests/` at the
# front of the module search path so `from agent.session_state import ...`
# resolves correctly when pytest is invoked from any working directory.
sys.path.insert(0, str(Path(__file__).parent))
