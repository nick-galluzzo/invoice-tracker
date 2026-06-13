import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

# Agents are instantiated at import time and require this env var.
# Tests only exercise pure Python logic (determine_line_item_vat), not the agents.
os.environ.setdefault("GOOGLE_API_KEY", "test-dummy-key")
