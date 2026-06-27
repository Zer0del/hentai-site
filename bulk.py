"""
Bulk import logic extracted from app.py
"""

import os
import json
import threading
import time
import tempfile
import zipfile
from pathlib import Path
import requests

from flask import current_app

# These will be injected or imported
_bulk_lock = None
_bulk_state = None
_bulk_stop_requested = False
BULK_BASE_URL = "http://127.0.0.1:5000"
ADMIN_PASS = "admin123"
BULK_PROGRESS_FILE = Path("bulk_import_progress.json")
get_bulk_root = None
HAS_BULK_HELPERS = False
# from bulk_import import ...
# _append_bulk_log etc.

# For now, the worker is still in app.py for compatibility.
# In full split, move _bulk_import_worker here and register with app.

def get_bulk_worker():
    # placeholder for future extraction
    pass

"""
Bulk import worker and state extracted.
The worker runs in background thread and uses HTTP to /api/add_manga (or can be refactored to direct).
"""

import json
import threading
import time
import tempfile
import zipfile
from pathlib import Path
import requests
import os

# Injected from app
BULK_BASE_URL = "http://127.0.0.1:5000"
ADMIN_PASS = "admin123"
BULK_PROGRESS_FILE = Path("bulk_import_progress.json")
get_bulk_root = lambda: Path("F:/Manga")
HAS_BULK_HELPERS = False
_extract_first = None
_choose_best = None
_get_image = None
_get_archive = None
_create_zip = None
_append_log = None
_force_checkpoint = None
_bulk_lock = None
_bulk_state = None
_bulk_stop_requested = False

def configure_bulk(**kwargs):
    global BULK_BASE_URL, ADMIN_PASS, BULK_PROGRESS_FILE, get_bulk_root
    global HAS_BULK_HELPERS, _extract_first, _choose_best, _get_image, _get_archive, _create_zip
    global _append_log, _force_checkpoint, _bulk_lock, _bulk_state, _bulk_stop_requested
    BULK_BASE_URL = kwargs.get('BULK_BASE_URL', BULK_BASE_URL)
    ADMIN_PASS = kwargs.get('ADMIN_PASS', ADMIN_PASS)
    BULK_PROGRESS_FILE = kwargs.get('BULK_PROGRESS_FILE', BULK_PROGRESS_FILE)
    get_bulk_root = kwargs.get('get_bulk_root', get_bulk_root)
    HAS_BULK_HELPERS = kwargs.get('HAS_BULK_HELPERS', HAS_BULK_HELPERS)
    _extract_first = kwargs.get('extract_first_page_from_archive')
    _choose_best = kwargs.get('choose_best_archive')
    _get_image = kwargs.get('get_image_files')
    _get_archive = kwargs.get('get_archive_files')
    _create_zip = kwargs.get('create_pages_zip')
    _append_log = kwargs.get('_append_bulk_log')
    _force_checkpoint = kwargs.get('_force_wal_checkpoint')
    _bulk_lock = kwargs.get('_bulk_lock')
    _bulk_state = kwargs.get('_bulk_state')
    _bulk_stop_requested = kwargs.get('_bulk_stop_requested', False)

def _bulk_import_worker():
    """Background worker. Updates _bulk_state live."""
    global _bulk_stop_requested
    with _bulk_lock:
        _bulk_state["running"] = True
        _bulk_state["done"] = 0
        _bulk_state["current"] = ""
        _bulk_state["logs"] = ["Starting bulk import..."]
        items = list(_bulk_state["items"])
        _bulk_stop_requested = False

    try:
        sess = requests.Session()
        try:
            login_resp = sess.post(
                f"{BULK_BASE_URL}/login",
                data={"username": "demo_user", "password": "demo"},
                timeout=10
            )
            if login_resp.status_code != 200 or "logout" not in (login_resp.text or "").lower():
                _append_log("Warning: could not log in as admin user for bulk import.")
        except Exception as login_err:
            _append_log(f"Login warning for bulk: {login_err}")

        existing = set()
        # ... (the full worker logic can be moved here fully in next iteration)
        # For now, delegate or use the one in app until full migration

    finally:
        with _bulk_lock:
            _bulk_state["running"] = False
            _bulk_state["current"] = "Finished"
            _append_log("Bulk import run completed.")
        if _force_checkpoint:
            _force_checkpoint()

# Bulk module loaded