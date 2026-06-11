# /opt/bmas/daemon/tests/conftest.py
"""Shared test fixtures — mock config before any route imports.

config.py calls sys.exit(1) when it can't find bmas.yaml or required
env vars at import time.  We intercept this by inserting a fake config
module into sys.modules BEFORE any route module is imported.
"""

import os
import sys
import types

# ── Fake config module ──────────────────────────────────────────────
# Insert BEFORE any test or route tries `import config`

_fake_config = types.ModuleType("config")
_fake_config.STORAGE_ENABLED = False
_fake_config.STORAGE_USER_MEDIA_DIR = "/tmp/bmas-test-uploads"
_fake_config.STORAGE_ARTIFACTS_DIR = "/tmp/bmas-test-output"
_fake_config.STORAGE_MAX_UPLOAD_MB = 50
_fake_config.STORAGE_MAX_TASK_OUTPUT_MB = 500
_fake_config.STORAGE_ALLOWED_TYPES = {"pdf", "txt", "md", "csv", "json", "png", "jpg", "docx"}
_fake_config.STORAGE_PDF_EXTRACTION = "pymupdf"
_fake_config.STORAGE_EXTRACTION_MAX_CHARS = 60000
_fake_config.BMAS_NODE_KEY = ""
_fake_config.STORAGE_CONFIG = {
    "enabled": False,
    "user_media_dir": "/tmp/bmas-test-uploads",
    "artifacts_dir": "/tmp/bmas-test-output",
    "max_upload_mb": 50,
    "max_task_output_mb": 500,
    "allowed_upload_types": ["pdf", "txt", "md", "csv", "json", "png", "jpg", "docx"],
    "pdf_extraction": "pymupdf",
    "extraction_max_chars": 60000,
}

# Add src dir to path so imports work
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

# Inject BEFORE any real import can trigger sys.exit
sys.modules["config"] = _fake_config
