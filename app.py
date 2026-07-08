#!/usr/bin/env python3
"""
Hentach - Hentai Manga Reader
Admin can add manga. High quality reader.
Uses Flask + SQLite + Tailwind CDN + vanilla JS
"""

# Friendly check for common missing dependencies (especially on first run / Windows)
try:
    import flask
    from werkzeug.utils import secure_filename
except ImportError as _dep_err:
    print("\n" + "="*60)
    print("ERROR: Required Python packages are not installed.")
    print("Missing:", _dep_err)
    print("\nPlease run the 'run.bat' file (it will install them automatically).")
    print("Or run manually in this folder:")
    print("    python -m pip install -r requirements.txt")
    print("="*60 + "\n")
    input("Press Enter to exit...")
    raise SystemExit(1)

import os
import re
import json
import sqlite3
import uuid
import zipfile
import shutil
import random
import time
import logging
import threading
import tempfile
from datetime import datetime
from pathlib import Path
from werkzeug.utils import secure_filename
from flask import (
    Flask, render_template, request, jsonify, redirect, url_for,
    send_from_directory, send_file, abort, session, g
)
try:
    from flask_caching import Cache
    HAS_CACHE = True
except ImportError:
    Cache = None
    HAS_CACHE = False

try:
    from flask_admin import Admin
    HAS_ADMIN = True
except ImportError:
    Admin = None
    HAS_ADMIN = False

try:
    from flask_compress import Compress
    HAS_COMPRESS = True
except ImportError:
    Compress = None
    HAS_COMPRESS = False

import helpers as h

# Also import specific ones for direct use
from helpers import (
    slugify, compute_rating, get_manga_by_slug, get_all_mangas,
    get_cover_url, get_page_urls, _create_thumbnail, save_uploaded_file,
    extract_zip_and_normalize, get_manga_dir, get_next_page_index, _finalize_cover,
    natural_sort_key,
    get_current_user, login_user, logout_user, update_user_history,
    get_user_tag_weights, get_recommendations,
    get_db, init_db, get_all_users,
    get_all_tags
)

# Basic logging — must be early, before any top-level code that might log
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Pillow is optional but strongly recommended for good-looking small previews
try:
    from PIL import Image
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# Config
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads", "manga")
DB_PATH = os.path.join(DATA_DIR, "manga.db")

# Legacy bulk system fully removed. Mass folder import (mass_import) + multi-ZIP in add form is the active way.

# Persistent secret key (stable across restarts for sessions)
SECRET_FILE = os.path.join(DATA_DIR, ".secret_key")
def _get_secret_key():
    if os.path.exists(SECRET_FILE):
        with open(SECRET_FILE, "r") as f:
            return f.read().strip()
    else:
        key = uuid.uuid4().hex
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(SECRET_FILE, "w") as f:
            f.write(key)
        return key

ADMIN_PASS = os.environ.get("HENTACH_ADMIN_PASS", os.environ.get("FAKKU_ADMIN_PASS", "admin123"))

# IP whitelist for site access during development.
# Only these IPs can access the site (including login).
# Set via env: ALLOWED_IPS="213.21.250.124,86.110.23.19,..."
# localhost included for local dev.
ALLOWED_IPS = set([ip.strip() for ip in os.environ.get(
    "ALLOWED_IPS", "127.0.0.1,::1,213.21.250.124,86.110.23.19"
).split(",") if ip.strip()])

ALLOWED_COVER = {"png", "jpg", "jpeg", "webp", "gif"}
ALLOWED_PAGE = {"png", "jpg", "jpeg", "webp", "gif"}
ALLOWED_ZIP = {"zip", "cbz"}

os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(UPLOAD_DIR, exist_ok=True)
AVATAR_DIR = os.path.join(BASE_DIR, "uploads", "avatars")
os.makedirs(AVATAR_DIR, exist_ok=True)
FORUM_ATTACH_DIR = os.path.join(BASE_DIR, "uploads", "forum")
os.makedirs(FORUM_ATTACH_DIR, exist_ok=True)

# Initialize helpers module
h.init_helpers(UPLOAD_DIR, DB_PATH, HAS_PIL, Image if HAS_PIL else None)

def create_app():
    """Application factory - makes the project easier to maintain, test and extend."""
    app = Flask(__name__)
    app.secret_key = _get_secret_key()
    app.config["MAX_CONTENT_LENGTH"] = 2 * 1024 * 1024 * 1024  # 2GB max upload - remove any size limits for large manga archives with many pages

    if HAS_CACHE:
        cache = Cache(app, config={'CACHE_TYPE': 'SimpleCache'})
    if HAS_COMPRESS:
        Compress(app)

    # Register modular blueprints (routes split out of this file)
    from blueprints.main import main_bp
    from blueprints.admin import admin_bp
    from blueprints.api import api_bp
    from blueprints.forum import forum_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(api_bp)
    app.register_blueprint(forum_bp)

    if HAS_ADMIN:
        admin = Admin(app, name='Hentach Admin', template_mode='bootstrap3')
        # Note: For full Flask-Admin, define ModelView for DB tables. Current custom admin remains for mass import etc.

    # IP whitelist check (dev mode) - block access from non-allowed IPs
    @app.before_request
    def restrict_to_whitelisted_ips():
        if not ALLOWED_IPS:
            return  # no restriction if empty
        # Get real client IP (behind nginx proxy)
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            client_ip = forwarded.split(",")[0].strip()
        else:
            client_ip = request.remote_addr or ""
        if client_ip not in ALLOWED_IPS:
            # Return 403 without details
            return "Access denied (IP not in whitelist).", 403

    # Bulk feature removed (per user request). Multiple ZIP uploads in admin form is the replacement for adding several mangas at once.
    # (old bulk code left for reference but not used in UI/routes)

    @app.context_processor
    def inject_current_user():
        # This will be overridden or use the one from helpers if available
        try:
            from app import get_current_user
            return dict(current_user=get_current_user())
        except:
            return dict(current_user=None)

    # Serve user-uploaded images (covers, pages, thumbs, avatars).
    # All templates and helpers generate /uploads/... URLs.
    # This was missing after the blueprint split -> all image functions appeared broken (404s).
    uploads_root = os.path.join(BASE_DIR, "uploads")
    @app.route("/uploads/<path:filename>")
    def serve_uploads(filename):
        response = send_from_directory(uploads_root, filename)
        # Strong cache for images to prevent re-downloads on back/forward and reloads (like nhentai)
        if filename.lower().endswith(('.webp', '.jpg', '.jpeg', '.png', '.gif')):
            response.headers['Cache-Control'] = 'public, max-age=2592000, immutable'
        else:
            response.cache_control.max_age = 86400 * 30
            response.cache_control.public = True
        return response

    @app.after_request
    def add_header(response):
        # Production headers for perf and security
        if 'static' in request.path or '/uploads/' in request.path:
            response.cache_control.max_age = 86400 * 7  # 1 week for static/uploads
            response.cache_control.public = True
        # Security basics
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'SAMEORIGIN'
        return response

    return app

# app created at the end after all definitions

# DB functions moved to helpers.py
# get_db, init_db, etc. are imported above from helpers.

def seed_demo_if_empty():
    """Create initial demo users if none exist. Demo manga seeding disabled to prevent placeholder reappearing after deletion."""
    conn = get_db()

    # Seed demo users if none (for initial setup / testing)
    user_count = conn.execute("SELECT COUNT(*) as c FROM users").fetchone()["c"]
    if user_count == 0:
        demo_users = [
            ("demo_user", "demo"),
            ("fan_lover", "123"),
            ("hentai_fan", "pass"),
        ]
        for uname, pw in demo_users:
            try:
                conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (uname, pw))
            except Exception:
                pass
        # Make demo users premium and admin for testing
        try:
            conn.execute("UPDATE users SET is_premium=1, is_admin=1 WHERE username IN ('demo_user', 'fan_lover', 'hentai_fan')")
        except Exception:
            pass
        conn.commit()
        logger.info("Seeded demo users: demo_user / demo , fan_lover / 123 , hentai_fan / pass")

    # NOTE: Demo manga seeding removed. If you want placeholder, add manually via admin.

    # Seed default forum categories and forums if none exist
    cat_count = conn.execute("SELECT COUNT(*) as c FROM forum_categories").fetchone()["c"]
    if cat_count == 0:
        try:
            # Categories
            c1 = conn.execute("INSERT INTO forum_categories (name, description, display_order) VALUES (?, ?, ?)",
                              ("General", "General discussion about anything", 1)).lastrowid
            c2 = conn.execute("INSERT INTO forum_categories (name, description, display_order) VALUES (?, ?, ?)",
                              ("Manga & Hentai", "Discussions about specific titles, recommendations, etc.", 2)).lastrowid

            # Forums/Boards
            conn.execute("INSERT INTO forum_forums (category_id, name, description, display_order) VALUES (?, ?, ?, ?)",
                         (c1, "Off-topic", "Random chatter, memes, life stuff", 1))
            conn.execute("INSERT INTO forum_forums (category_id, name, description, display_order) VALUES (?, ?, ?, ?)",
                         (c1, "Site Feedback", "Suggestions, bugs, feature requests for the site", 2))
            conn.execute("INSERT INTO forum_forums (category_id, name, description, display_order) VALUES (?, ?, ?, ?)",
                         (c2, "Manga Discussion", "Talk about specific mangas, reviews", 1))
            conn.execute("INSERT INTO forum_forums (category_id, name, description, display_order) VALUES (?, ?, ?, ?)",
                         (c2, "Recommendations", "What to read next?", 2))
            conn.commit()
            logger.info("Seeded default forum categories and boards")
        except Exception as e:
            logger.warning("Forum seed error: %s", e)

    conn.close()

def slugify(text):
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text or "manga-" + str(uuid.uuid4())[:8]

def compute_rating(row):
    if row["rating_count"] == 0:
        return 0.0, 0
    avg = round(row["rating_sum"] / row["rating_count"], 1)
    return avg, row["rating_count"]

# get_manga_by_slug and get_all_mangas are now in helpers.py (delegated)

# -------------------- Helpers (centralized) --------------------
def get_cover_url(row, thumb=False):
    """Return public URL for a manga's cover (handles local vs external).
    If thumb=True, prefers cover-thumb.webp if it exists (for cards/grids).
    For main cover, prefers cover-main.webp (server-resized with LANCZOS) to reduce browser aliasing.
    """
    if row is None:
        return ""
    if isinstance(row, sqlite3.Row):
        cover = row["cover"]
        slug = row["slug"]
    elif isinstance(row, dict):
        cover = row.get("cover", "")
        slug = row.get("slug", "")
    else:
        cover = getattr(row, "cover", "")
        slug = getattr(row, "slug", "")

    if isinstance(cover, str) and cover.startswith(("http://", "https://")):
        return cover

    if slug and cover:
        if thumb:
            thumb_name = "cover-thumb.webp"
            thumb_path = os.path.join(UPLOAD_DIR, slug, thumb_name)
            if os.path.exists(thumb_path):
                return f"/uploads/manga/{slug}/{thumb_name}"
        # Prefer optimized main cover (resized with LANCZOS on server) for detail page
        # to avoid browser downscaling rippling/aliasing on the large cover.
        main_name = "cover-main.webp"
        main_path = os.path.join(UPLOAD_DIR, slug, main_name)
        if os.path.exists(main_path):
            return f"/uploads/manga/{slug}/{main_name}"
        # Auto-generate optimized cover-main on first view if current cover is large.
        # This fixes rippling for previously imported mangas without re-upload.
        # One-time cost, then cached as cover-main.webp.
        orig_path = os.path.join(UPLOAD_DIR, slug, cover)
        if os.path.exists(orig_path):
            try:
                from PIL import Image
                with Image.open(orig_path) as im:
                    if im.width > 800 or im.height > 1000:
                        if _create_thumbnail(orig_path, main_path, max_width=800, quality=92, resample=Image.LANCZOS):
                            # ensure thumb too
                            thumb_p = os.path.join(UPLOAD_DIR, slug, "cover-thumb.webp")
                            if not os.path.exists(thumb_p):
                                _create_thumbnail(main_path, thumb_p, max_width=320, resample=Image.BILINEAR)
                            return f"/uploads/manga/{slug}/{main_name}"
            except Exception:
                pass
        return f"/uploads/manga/{slug}/{cover}"
    return cover or ""

def get_page_urls(slug, pages, thumb=False):
    """Convert internal page list (filenames or urls) to public URLs.
    If thumb=True, tries to use *-thumb.webp versions (for small previews/strip).
    Falls back to full size if thumb not present (good for old uploads).
    """
    if isinstance(pages, str):
        try:
            pages = json.loads(pages)
        except Exception:
            pages = []
    resolved = []
    for p in pages or []:
        if isinstance(p, str) and p.startswith(("http://", "https://")):
            resolved.append(p)
            continue

        if thumb:
            base, ext = os.path.splitext(p)
            thumb_name = f"{base}-thumb.webp"
            thumb_path = os.path.join(UPLOAD_DIR, slug, thumb_name)
            if os.path.exists(thumb_path):
                resolved.append(f"/uploads/manga/{slug}/{thumb_name}")
                continue

        resolved.append(f"/uploads/manga/{slug}/{p}")
    return resolved


def _create_thumbnail(src_path, thumb_path, max_width=None, max_height=None, quality=90, resample=None):
    """Resize/re-encode image to WebP using Pillow. Falls back silently if Pillow unavailable.
    Use resample=Image.BILINEAR for faster thumbs, LANCZOS for best quality.
    """
    if not HAS_PIL or not os.path.exists(src_path):
        return False

    try:
        from PIL import Image
        if resample is None:
            resample = Image.LANCZOS

        with Image.open(src_path) as im:
            # Handle transparency / mode for WebP
            if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
                im = im.convert("RGBA")
            else:
                im = im.convert("RGB")

            orig_w, orig_h = im.size
            if max_width and max_height:
                ratio = min(max_width / orig_w, max_height / orig_h)
            elif max_width:
                ratio = max_width / orig_w
            elif max_height:
                ratio = max_height / orig_h
            else:
                ratio = 1.0

            new_w = max(1, int(orig_w * ratio))
            new_h = max(1, int(orig_h * ratio))

            if ratio != 1.0:
                im = im.resize((new_w, new_h), resample)

            # Ensure .webp 
            if not thumb_path.lower().endswith(".webp"):
                thumb_path = os.path.splitext(thumb_path)[0] + ".webp"

            im.save(thumb_path, "WEBP", quality=quality, method=6, lossless=False)
        return True
    except Exception as e:
        logger.warning("Thumbnail generation failed for %s: %s", src_path, e)
        return False


def save_uploaded_file(file, dest_dir, prefix="file"):
    if not file or not file.filename:
        return None
    filename = secure_filename(file.filename)
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext not in ALLOWED_COVER | ALLOWED_PAGE:
        return None
    unique = f"{prefix}-{uuid.uuid4().hex[:8]}.{ext}"
    path = os.path.join(dest_dir, unique)
    file.save(path)
    return unique

def extract_zip_and_normalize(zip_file, dest_dir, prefix="page"):
    """Extract images from zip, rename sequentially 001.jpg etc. Return list of filenames."""
    pages = []
    temp_zip = os.path.join(dest_dir, "_temp.zip")
    zip_file.save(temp_zip)

    with zipfile.ZipFile(temp_zip, "r") as z:
        # Get image entries
        entries = []
        for name in z.namelist():
            if name.endswith("/"):
                continue
            ext = name.rsplit(".", 1)[-1].lower()
            if ext in ALLOWED_PAGE:
                entries.append(name)
        # natural sort so 1.jpg, 2.jpg, 10.jpg stay correct order
        entries.sort(key=lambda n: natural_sort_key(Path(n).name))

        idx = 1
        for entry in entries:
            ext = entry.rsplit(".", 1)[-1].lower()
            out_name = f"{idx:03d}.{ext}"
            out_path = os.path.join(dest_dir, out_name)
            with z.open(entry) as src, open(out_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            pages.append(out_name)

            base = os.path.splitext(out_name)[0]

            # Generate small thumbnail for strip / grids
            thumb_name = f"{base}-thumb.webp"
            thumb_path = os.path.join(dest_dir, thumb_name)
            _create_thumbnail(out_path, thumb_path, max_height=240, resample=Image.BILINEAR)

            # Re-encode full page as high-quality WebP for consistent viewer quality (skip if already)
            if not out_name.lower().endswith('.webp'):
                webp_name = f"{base}.webp"
                webp_path = os.path.join(dest_dir, webp_name)
                if _create_thumbnail(out_path, webp_path, quality=95):
                    pages[-1] = webp_name  # store the webp version as the full image
                    if out_name != webp_name and os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except:
                            pass

            idx += 1

    os.remove(temp_zip)
    return pages

def get_manga_dir(slug):
    d = os.path.join(UPLOAD_DIR, slug)
    os.makedirs(d, exist_ok=True)
    return d

def get_next_page_index(slug):
    """Returns the next numeric page index (e.g. 9 for 009.jpg)"""
    manga_dir = get_manga_dir(slug)
    nums = []
    for fname in os.listdir(manga_dir):
        if fname.lower().startswith("cover") or "-thumb" in fname.lower():
            continue
        base = os.path.splitext(fname)[0]
        if base.isdigit():
            nums.append(int(base))
    return max(nums) + 1 if nums else 1


def _finalize_cover(manga_dir, initial_cover_name):
    """
    Common logic for cover after upload/save:
    - Re-encode to WebP if needed (quality 95)
    - Create optimized cover-main.webp (800px LANCZOS) to prevent rippling on detail
    - Create cover-thumb.webp (BILINEAR 320px)
    Returns the final cover_name to store in DB.
    """
    cover_name = initial_cover_name
    cover_full_path = os.path.join(manga_dir, cover_name)

    # Re-encode to WebP if necessary
    if not cover_name.lower().endswith('.webp'):
        cover_base = os.path.splitext(cover_name)[0]
        cover_webp = f"{cover_base}.webp"
        cover_webp_path = os.path.join(manga_dir, cover_webp)
        if _create_thumbnail(cover_full_path, cover_webp_path, quality=95):
            try:
                os.remove(cover_full_path)
            except:
                pass
            cover_name = cover_webp
            cover_full_path = cover_webp_path

    # Optimized main cover for detail page (prevents browser aliasing/rippling)
    main_cover_path = os.path.join(manga_dir, "cover-main.webp")
    if _create_thumbnail(cover_full_path, main_cover_path, max_width=800, quality=92, resample=Image.LANCZOS):
        try:
            if cover_full_path != main_cover_path and os.path.exists(cover_full_path):
                os.remove(cover_full_path)
        except:
            pass
        cover_name = "cover-main.webp"
        cover_full_path = main_cover_path

    # Thumbnail for cards/grids
    _create_thumbnail(cover_full_path, os.path.join(manga_dir, "cover-thumb.webp"),
                      max_width=320, resample=Image.BILINEAR)

    return cover_name


# Legacy bulk helpers removed.


# Legacy bulk removed.

# -------------------- USER & RECOMMENDATIONS HELPERS --------------------

def get_current_user():
    user_id = session.get('user_id')
    if not user_id:
        return None
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        d = dict(row)
        d.setdefault('is_premium', 0)
        d.setdefault('avatar', '')
        d.setdefault('username_color', '#e11d48')
        d.setdefault('is_admin', 0)
        return d
    return None

def login_user(user_id):
    session['user_id'] = user_id
    session.permanent = True

def logout_user():
    session.pop('user_id', None)



def update_user_history(user_id, manga_id, last_page, completed=False):
    if not user_id:
        return
    conn = get_db()
    conn.execute("""
        INSERT INTO user_history (user_id, manga_id, last_page, completed, last_read_at)
        VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, manga_id) DO UPDATE SET
            last_page = MAX(last_page, ?),
            completed = MAX(completed, ?),
            last_read_at = CURRENT_TIMESTAMP
    """, (user_id, manga_id, last_page, int(completed), last_page, int(completed)))
    conn.commit()
    conn.close()


# Simple time-based cache for expensive on-the-fly computations
_cache = {}
_CACHE_TTL = 45  # seconds

def invalidate_user_tag_cache(user_id):
    """Clear tag weights (and related) cache for a user so profile/recommendations update immediately."""
    prefix = f"tag_weights:{user_id}"
    rec_prefix = f"recs:{user_id}"
    to_delete = []
    for k in list(_cache.keys()):
        if isinstance(k, str) and (k.startswith(prefix) or rec_prefix in k):
            to_delete.append(k)
    for k in to_delete:
        _cache.pop(k, None)

def _get_cached(key, func, *args, **kwargs):
    now = time.time()
    entry = _cache.get(key)
    if entry and now - entry["ts"] < _CACHE_TTL:
        return entry["val"]
    val = func(*args, **kwargs)
    _cache[key] = {"val": val, "ts": now}
    return val

def get_user_tag_weights(user_id):
    """Вычисляет веса тегов пользователя на основе оценок и избранного.
    Возвращает dict {tag: weight}. Cached briefly for speed.
    """
    if not user_id:
        return {}
    return _get_cached(f"tag_weights:{user_id}", _compute_user_tag_weights, user_id)

def _compute_user_tag_weights(user_id):
    conn = get_db()
    signals = conn.execute("""
        SELECT m.tags,
               COALESCE(r.score, 0) as score,
               (f.manga_id IS NOT NULL) as is_fav
        FROM mangas m
        LEFT JOIN user_ratings r ON r.manga_id = m.id AND r.user_id = ?
        LEFT JOIN user_favorites f ON f.manga_id = m.id AND f.user_id = ?
        WHERE r.score IS NOT NULL OR f.manga_id IS NOT NULL
    """, (user_id, user_id)).fetchall()

    tag_weights = {}
    for row in signals:
        tags = json.loads(row['tags'] or '[]')
        w = 0
        if row['is_fav']:
            w += 3
        if row['score'] > 0:
            # proportional to 10-point score (1 -> negative, 10 -> positive)
            w += round((row['score'] - 5) * 0.7)
        for t in tags:
            tag_weights[t] = tag_weights.get(t, 0) + w

    return tag_weights


def _compute_recommendations(user_id, limit=8):
    tag_weights = _compute_user_tag_weights(user_id)

    total_signal = sum(tag_weights.values())
    if total_signal < 5:
        # Cold start: популярные, исключая прочитанные/оцененные/избранные
        conn = get_db()
        read_ids = {r['manga_id'] for r in conn.execute(
            "SELECT manga_id FROM user_history WHERE user_id=?", (user_id,)
        ).fetchall()}
        rated_ids = {r['manga_id'] for r in conn.execute(
            "SELECT manga_id FROM user_ratings WHERE user_id=?", (user_id,)
        ).fetchall()}
        fav_ids = {r['manga_id'] for r in conn.execute(
            "SELECT manga_id FROM user_favorites WHERE user_id=?", (user_id,)
        ).fetchall()}

        recs = conn.execute("""
            SELECT *, 
                   (rating_sum * 1.0 / NULLIF(rating_count, 0)) as avg_rating
            FROM mangas
            ORDER BY avg_rating DESC, id DESC
        """).fetchall()

        result = []
        for r in recs:
            if r['id'] in read_ids or r['id'] in rated_ids or r['id'] in fav_ids:
                continue
            r = dict(r)
            r["cover"] = get_cover_url(r, thumb=True)
            result.append(r)
            if len(result) >= limit:
                break
        conn.close()
        # Final strict filter to guarantee no favorited/rated/read manga leaks (re-query fresh)
        try:
            c2 = get_db()
            interacted = set()
            for tbl in ("user_favorites", "user_ratings", "user_history"):
                for rr in c2.execute(f"SELECT manga_id FROM {tbl} WHERE user_id=?", (user_id,)).fetchall():
                    interacted.add(rr['manga_id'])
            c2.close()
            result = [x for x in result if x['id'] not in interacted]
        except Exception as e: print('recs filter err', e)
        return result[:limit]

    # Исключаем прочитанные
    conn = get_db()
    read_ids = {r['manga_id'] for r in conn.execute(
        "SELECT manga_id FROM user_history WHERE user_id=?", (user_id,)
    ).fetchall()}
    rated_ids = {r['manga_id'] for r in conn.execute(
        "SELECT manga_id FROM user_ratings WHERE user_id=?", (user_id,)
    ).fetchall()}
    fav_ids = {r['manga_id'] for r in conn.execute(
        "SELECT manga_id FROM user_favorites WHERE user_id=?", (user_id,)
    ).fetchall()}

    candidates = conn.execute("SELECT * FROM mangas ORDER BY created_at DESC LIMIT 500").fetchall()  # cap for perf on large libs
    scored = []
    for m in candidates:
        if m['id'] in read_ids or m['id'] in rated_ids or m['id'] in fav_ids:
            continue
        tags = json.loads(m['tags'] or '[]')
        score = sum(tag_weights.get(t, 0) for t in tags)

        # Бонус мангам, у которых много совпадающих "любимых" тегов
        matching_positive = sum(1 for t in tags if tag_weights.get(t, 0) > 0)
        if matching_positive >= 3:
            bonus = (matching_positive - 2) * 2  # +2 за каждый дополнительный совпадающий тег
            score += bonus

        if score > 0:
            scored.append((score, m))

    scored.sort(key=lambda x: -x[0])
    result = []
    for s, m in scored[:limit]:
        m = dict(m)
        m["cover"] = get_cover_url(m, thumb=True)
        result.append(m)
    conn.close()
    # Final strict filter to guarantee no favorited/rated/read manga leaks (re-query fresh)
    try:
        c2 = get_db()
        interacted = set()
        for tbl in ("user_favorites", "user_ratings", "user_history"):
            for rr in c2.execute(f"SELECT manga_id FROM {tbl} WHERE user_id=?", (user_id,)).fetchall():
                interacted.add(rr['manga_id'])
        c2.close()
        result = [x for x in result if x['id'] not in interacted]
    except Exception as e: print('recs filter err', e)
    return result[:limit]


def get_recommendations(user_id, limit=8):
    """Рекомендации на основе существующих данных (оценки + избранное + теги).
    Без хранения отдельных весов. Uses short cache.
    """
    if not user_id:
        return []

    def _compute_recs(uid, lim):
        return _compute_recommendations(uid, lim)

    return _get_cached(f"recs:{user_id}:{limit}", _compute_recs, user_id, limit)


# All routes moved to blueprints/ for modularity.
# app.py is now focused on core + factory.

# All remaining routes moved to blueprints. This section cleaned for modularity.

# -------------------- USER AUTH & PROFILE --------------------

# login route moved to blueprints/main.py or api

# @app.route("/login_as/<int:user_id>")
def login_as(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if user:
        login_user(user["id"])
    return redirect(url_for("index"))

# @app.route("/register", methods=["POST"])
def register():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not username or not password:
        return redirect(url_for("login"))
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        login_user(user["id"])
    except Exception as e:
        logger.warning("Register failed: %s", e)
    conn.close()
    return redirect(url_for("index"))

# @app.route("/logout")
def logout():
    logout_user()
    return redirect(url_for("index"))

# @app.route("/profile")
def profile():
    user = get_current_user()
    if not user:
        return redirect(url_for("login"))

    conn = get_db()
    # Favorites
    fav_rows = conn.execute("""
        SELECT m.* FROM mangas m
        JOIN user_favorites f ON m.id = f.manga_id
        WHERE f.user_id = ? ORDER BY f.added_at DESC
    """, (user['id'],)).fetchall()
    favorites = []
    for r in fav_rows:
        cover = get_cover_url(r, thumb=True)
        favorites.append({**dict(r), "cover": cover})

    # History
    hist_rows = conn.execute("""
        SELECT m.*, h.last_page, h.completed, h.last_read_at
        FROM user_history h
        JOIN mangas m ON m.id = h.manga_id
        WHERE h.user_id = ? ORDER BY h.last_read_at DESC LIMIT 20
    """, (user['id'],)).fetchall()
    history = []
    for r in hist_rows:
        cover = get_cover_url(r, thumb=True)
        history.append({**dict(r), "cover": cover})

    # My ratings
    rating_rows = conn.execute("""
        SELECT m.title, m.slug, ur.score FROM user_ratings ur
        JOIN mangas m ON m.id = ur.manga_id
        WHERE ur.user_id = ? ORDER BY ur.rated_at DESC
    """, (user['id'],)).fetchall()

    conn.close()

    tag_weights = get_user_tag_weights(user['id'])
    sorted_weights = sorted(tag_weights.items(), key=lambda x: -x[1])[:15]  # топ 15

    recommendations = []
    if user.get('is_premium'):
        recommendations = get_recommendations(user['id'], limit=6)

    return render_template("profile.html", user=user, favorites=favorites, history=history, my_ratings=rating_rows, recommendations=recommendations, tag_weights=sorted_weights)

# get_all_users is now only in helpers.py (imported above)

# Quick API to favorite
# @app.route("/api/favorite", methods=["POST"])
def api_favorite():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Войдите в систему"}), 403

    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("manga_id"))
    except Exception:
        return jsonify({"error": "bad id"}), 400

    try:
        conn = get_db()
        existing = conn.execute("SELECT 1 FROM user_favorites WHERE user_id=? AND manga_id=?", (user['id'], manga_id)).fetchone() is not None

        if existing:
            conn.execute("DELETE FROM user_favorites WHERE user_id=? AND manga_id=?", (user['id'], manga_id))
            action = "removed"
        else:
            conn.execute("INSERT INTO user_favorites (user_id, manga_id) VALUES (?, ?)", (user['id'], manga_id))
            action = "added"

        conn.commit()
        conn.close()
        _cache.clear()
        return jsonify({"ok": True, "action": action})
    except Exception as e:
        return jsonify({"error": "server error: " + str(e)}), 500

# Update premium settings: avatar and username color
# @app.route("/api/update_profile", methods=["POST"])
def api_update_profile():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Войдите в систему"}), 403
    if not user.get('is_premium'):
        return jsonify({"error": "Доступно только премиум пользователям"}), 403

    avatar_file = request.files.get("avatar")
    color = (request.form.get("username_color") or request.form.get("color") or user.get('username_color') or '#e11d48').strip()

    try:
        conn = get_db()
        updates = []
        params = []

        if avatar_file and avatar_file.filename:
            filename = secure_filename(avatar_file.filename)
            ext = filename.rsplit(".", 1)[-1].lower()
            if ext in ALLOWED_COVER:
                avatar_name = f"user{user['id']}-{uuid.uuid4().hex[:8]}.{ext}"
                avatar_path = os.path.join(AVATAR_DIR, avatar_name)
                avatar_file.save(avatar_path)
                updates.append("avatar = ?")
                params.append(avatar_name)

        if color:
            updates.append("username_color = ?")
            params.append(color)

        if updates:
            params.append(user['id'])
            conn.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", params)
            conn.commit()

        conn.close()
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": "server error: " + str(e)}), 500

# @app.route("/api/mark_read", methods=["POST"])
def api_mark_read():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Войдите в систему"}), 403

    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("manga_id"))
        read = bool(int(data.get("read", 1)))
    except Exception:
        return jsonify({"error": "bad input"}), 400

    conn = get_db()
    completed = 1 if read else 0
    conn.execute("""
        INSERT INTO user_history (user_id, manga_id, completed, last_read_at)
        VALUES (?, ?, ?, CURRENT_TIMESTAMP)
        ON CONFLICT(user_id, manga_id) DO UPDATE SET completed = ?, last_read_at = CURRENT_TIMESTAMP
    """, (user['id'], manga_id, completed, completed))
    conn.commit()
    conn.close()
    _cache.clear()
    return jsonify({"ok": True})

# @app.route("/api/progress", methods=["POST"])
def api_progress():
    user = get_current_user()
    if not user:
        return jsonify({"ok": False})

    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("manga_id"))
        last_page = int(data.get("last_page", 1))
        total = int(data.get("total", 1))
    except Exception:
        return jsonify({"ok": False})

    completed = last_page >= total
    update_user_history(user['id'], manga_id, last_page, completed)

    return jsonify({"ok": True, "completed": completed})

# Test premium grant (will be replaced by payment later)
# @app.route("/api/grant_premium", methods=["POST"])
def api_grant_premium():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Войдите в систему"}), 403
    conn = get_db()
    conn.execute("UPDATE users SET is_premium = 1 WHERE id = ?", (user['id'],))
    conn.commit()
    conn.close()
    _cache.clear()
    return jsonify({"ok": True})

# Test admin grant
# @app.route("/api/grant_admin", methods=["POST"])
def api_grant_admin():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Войдите в систему"}), 403
    conn = get_db()
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = ?", (user['id'],))
    conn.commit()
    conn.close()
    _cache.clear()
    return jsonify({"ok": True})

# Post comment (available to logged in users)
# @app.route("/api/comment", methods=["POST"])
def api_comment():
    user = get_current_user()
    if not user:
        return jsonify({"error": "Войдите в систему"}), 403

    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("manga_id"))
        content = (data.get("content") or "").strip()
    except Exception:
        return jsonify({"error": "bad input"}), 400

    if not content or len(content) > 2000:
        return jsonify({"error": "Комментарий пустой или слишком длинный"}), 400

    conn = get_db()
    conn.execute(
        "INSERT INTO comments (user_id, manga_id, content) VALUES (?, ?, ?)",
        (user['id'], manga_id, content)
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Delete comment (admin only)
# @app.route("/api/delete_comment", methods=["POST"])
def api_delete_comment():
    user = get_current_user()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Нет прав"}), 403

    data = request.get_json(silent=True) or request.form
    try:
        cid = int(data.get("id"))
    except Exception:
        return jsonify({"error": "bad id"}), 400

    conn = get_db()
    comment = conn.execute("SELECT user_id FROM comments WHERE id = ?", (cid,)).fetchone()
    if not comment:
        conn.close()
        return jsonify({"error": "Комментарий не найден"}), 404

    can_delete = user.get('is_admin') or (comment['user_id'] == user['id'])
    if not can_delete:
        conn.close()
        return jsonify({"error": "Нет прав на удаление"}), 403

    conn.execute("DELETE FROM comments WHERE id = ?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

# Download manga as ZIP (premium only)
# @app.route("/download/<slug>")
def download_manga(slug):
    user = get_current_user()
    if not user or not user.get('is_premium'):
        return "Доступно только премиум пользователям", 403

    row = get_manga_by_slug(slug)
    if not row:
        abort(404)

    pages = json.loads(row["pages"] or "[]")
    manga_dir = get_manga_dir(slug)

    import hashlib
    import zipfile

    # Simple disk cache for downloads (performance optimization)
    # Keyed by slug + hash of latest mtime of cover + pages
    cache_dir = os.path.join(BASE_DIR, "data", "downloads")
    os.makedirs(cache_dir, exist_ok=True)

    # Compute cache key from file mtimes
    mtimes = []
    cover = row["cover"]
    if not cover.startswith(("http://", "https://")):
        cp = os.path.join(manga_dir, cover)
        if os.path.exists(cp):
            mtimes.append(os.path.getmtime(cp))
    for p in pages:
        if not p.startswith(("http://", "https://")):
            pp = os.path.join(manga_dir, p)
            if os.path.exists(pp):
                mtimes.append(os.path.getmtime(pp))
    key = hashlib.md5(f"{slug}:{max(mtimes) if mtimes else 0}".encode()).hexdigest()
    cache_path = os.path.join(cache_dir, f"{slug}-{key}.zip")

    if os.path.exists(cache_path):
        safe_title = "".join(c for c in row["title"] if c.isalnum() or c in " -_").rstrip()
        return send_file(
            cache_path,
            as_attachment=True,
            download_name=f"{safe_title or slug}.zip",
            mimetype="application/zip"
        )

    # Build and cache
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    zip_path = tmp.name

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            if not cover.startswith(("http://", "https://")):
                cover_path = os.path.join(manga_dir, cover)
                if os.path.exists(cover_path):
                    zf.write(cover_path, arcname=f"cover{os.path.splitext(cover)[1]}")
            for p in pages:
                if not p.startswith(("http://", "https://")):
                    p_path = os.path.join(manga_dir, p)
                    if os.path.exists(p_path):
                        zf.write(p_path, arcname=p)

        # Move to cache
        import shutil
        shutil.move(zip_path, cache_path)
        zip_path = cache_path

        safe_title = "".join(c for c in row["title"] if c.isalnum() or c in " -_").rstrip()
        return send_file(
            cache_path,
            as_attachment=True,
            download_name=f"{safe_title or slug}.zip",
            mimetype="application/zip"
        )
    except Exception as e:
        logger.exception("Download failed")
        if os.path.exists(zip_path):
            try:
                os.unlink(zip_path)
            except:
                pass
        return "Ошибка при подготовке скачивания", 500

# -------------------- ADMIN ADD --------------------
# @app.route("/api/add_manga", methods=["POST"])
def api_add_manga():
    # Simple auth: pass in form or header
    password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
    if password != ADMIN_PASS:
        return jsonify({"error": "Неверный пароль администратора"}), 403

    # Password is correct -> this is a trusted admin operation.
    # Allow calls that only have the password (bulk import tool, CLI script).
    # Browser-based admin calls will also have is_admin user.
    user = get_current_user()
    if not user or not user.get('is_admin'):
        # Password already verified above, so proceed.
        pass

    title = (request.form.get("title") or "").strip()
    author = (request.form.get("author") or "").strip()
    description = (request.form.get("description") or "").strip()
    tags_raw = (request.form.get("tags") or "").strip()

    if not title:
        return jsonify({"error": "Название обязательно"}), 400

    tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
    slug = slugify(title)

    # Make unique slug if exists
    conn = get_db()
    existing = conn.execute("SELECT id FROM mangas WHERE slug = ?", (slug,)).fetchone()
    if existing:
        slug = f"{slug}-{uuid.uuid4().hex[:6]}"

    manga_dir = get_manga_dir(slug)

    # Cover
    cover_file = request.files.get("cover")
    if not cover_file or not cover_file.filename:
        conn.close()
        return jsonify({"error": "Нужна обложка (cover)"}), 400

    cover_name = save_uploaded_file(cover_file, manga_dir, "cover")
    if not cover_name:
        conn.close()
        return jsonify({"error": "Неверный формат обложки"}), 400

    cover_name = _finalize_cover(manga_dir, cover_name)

    # Pages: either zip or multiple page images
    page_files = request.files.getlist("pages")
    pages = []

    zip_file = request.files.get("zipfile")
    if zip_file and zip_file.filename:
        ext = zip_file.filename.rsplit(".", 1)[-1].lower()
        if ext in ALLOWED_ZIP:
            try:
                pages = extract_zip_and_normalize(zip_file, manga_dir, "p")
            except Exception as e:
                conn.close()
                return jsonify({"error": f"Ошибка распаковки zip: {str(e)}"}), 400
        else:
            conn.close()
            return jsonify({"error": "Zip должен быть .zip или .cbz"}), 400
    elif page_files:
        idx = 1
        for f in page_files:
            if not f.filename:
                continue
            saved = save_uploaded_file(f, manga_dir, f"{idx:03d}")
            if saved:
                pages.append(saved)
                full_path = os.path.join(manga_dir, saved)
                base = os.path.splitext(saved)[0]

                # Generate thumb
                thumb_name = f"{base}-thumb.webp"
                _create_thumbnail(full_path, os.path.join(manga_dir, thumb_name), max_height=240, resample=Image.BILINEAR)

                # Re-encode full to high quality WebP (skip if already webp)
                if not saved.lower().endswith('.webp'):
                    webp_name = f"{base}.webp"
                    webp_path = os.path.join(manga_dir, webp_name)
                    if _create_thumbnail(full_path, webp_path, quality=95):
                        pages[-1] = webp_name
                        if saved != webp_name and os.path.exists(full_path):
                            try:
                                os.remove(full_path)
                            except:
                                pass
                idx += 1
        pages.sort(key=natural_sort_key)  # use natural for safety
    else:
        conn.close()
        return jsonify({"error": "Нужно загрузить страницы (множественный выбор или zip)"}), 400

    if not pages:
        conn.close()
        return jsonify({"error": "Не удалось загрузить ни одной страницы"}), 400

    # Save to DB
    try:
        conn.execute(
            """INSERT INTO mangas (slug, title, author, description, cover, pages, tags, rating_sum, rating_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)""",
            (slug, title, author, description, cover_name, json.dumps(pages), json.dumps(tags))
        )
        conn.commit()
        _cache.clear()  # new manga -> invalidate recs and other caches
    except Exception as e:
        conn.close()
        return jsonify({"error": f"Ошибка сохранения: {str(e)}"}), 500
    conn.close()

    return jsonify({"ok": True, "slug": slug, "title": title, "pages": len(pages)})


# -------------------- ADMIN EDIT / DELETE --------------------

# @app.route("/api/edit_manga", methods=["POST"])
def api_edit_manga():
    password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
    if password != ADMIN_PASS:
        return jsonify({"error": "Неверный пароль администратора"}), 403

    user = get_current_user()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Требуются права администратора (активируйте админку в профиле)"}), 403

    slug = (request.form.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "Не указан slug"}), 400

    row = get_manga_by_slug(slug)
    if not row:
        return jsonify({"error": "Манга не найдена"}), 404

    conn = get_db()

    # New metadata
    new_title = (request.form.get("title") or row["title"]).strip() or row["title"]
    new_author = (request.form.get("author") or row["author"] or "").strip()
    new_description = request.form.get("description", row["description"] or "")
    new_tags_raw = request.form.get("tags", None)
    if new_tags_raw is not None:
        tags = [t.strip() for t in new_tags_raw.split(",") if t.strip()]
    else:
        tags = json.loads(row["tags"] or "[]")

    manga_dir = get_manga_dir(slug)

    # Optional cover replacement
    cover_name = row["cover"]
    cover_file = request.files.get("cover")
    if cover_file and cover_file.filename:
        if not cover_name.startswith(("http://", "https://")):
            old_cover_path = os.path.join(manga_dir, cover_name)
            if os.path.exists(old_cover_path):
                try:
                    os.remove(old_cover_path)
                except Exception:
                    pass
            # also remove old thumb if existed
            old_thumb = os.path.join(manga_dir, "cover-thumb.webp")
            if os.path.exists(old_thumb):
                try:
                    os.remove(old_thumb)
                except Exception:
                    pass

        saved_cover = save_uploaded_file(cover_file, manga_dir, "cover")
        if saved_cover:
            cover_name = _finalize_cover(manga_dir, saved_cover)

    # Append new pages (files or zip)
    current_pages = json.loads(row["pages"] or "[]")
    added_count = 0

    zip_file = request.files.get("zipfile")
    page_files = request.files.getlist("pages")

    if zip_file and zip_file.filename:
        ext = zip_file.filename.rsplit(".", 1)[-1].lower()
        if ext in ALLOWED_ZIP:
            try:
                start_idx = get_next_page_index(slug)
                temp_zip = os.path.join(manga_dir, "_temp_edit.zip")
                zip_file.save(temp_zip)
                with zipfile.ZipFile(temp_zip, "r") as z:
                    entries = []
                    for name in z.namelist():
                        if name.endswith("/"):
                            continue
                        eext = name.rsplit(".", 1)[-1].lower()
                        if eext in ALLOWED_PAGE:
                            entries.append(name)
                    entries.sort(key=lambda n: natural_sort_key(Path(n).name))
                    for entry in entries:
                        eext = entry.rsplit(".", 1)[-1].lower()
                        out_name = f"{start_idx:03d}.{eext}"
                        out_path = os.path.join(manga_dir, out_name)
                        with z.open(entry) as src, open(out_path, "wb") as dst:
                            shutil.copyfileobj(src, dst)
                        current_pages.append(out_name)

                        base = os.path.splitext(out_name)[0]

                        # thumb
                        thumb_name = f"{base}-thumb.webp"
                        _create_thumbnail(out_path, os.path.join(manga_dir, thumb_name), max_height=240)

                        # re-encode full webp (skip if already)
                        if not out_name.lower().endswith('.webp'):
                            webp_name = f"{base}.webp"
                            webp_path = os.path.join(manga_dir, webp_name)
                            if _create_thumbnail(out_path, webp_path, quality=95):
                                current_pages[-1] = webp_name
                                if out_name != webp_name and os.path.exists(out_path):
                                    try:
                                        os.remove(out_path)
                                    except:
                                        pass
                        start_idx += 1
                        added_count += 1
                os.remove(temp_zip)
            except Exception as e:
                conn.close()
                return jsonify({"error": f"Ошибка добавления из zip: {str(e)}"}), 400
        else:
            conn.close()
            return jsonify({"error": "Поддерживаются только .zip и .cbz"}), 400

    elif page_files:
        start_idx = get_next_page_index(slug)
        for f in page_files:
            if not f.filename:
                continue
            saved = save_uploaded_file(f, manga_dir, f"{start_idx:03d}")
            if saved:
                current_pages.append(saved)
                full_path = os.path.join(manga_dir, saved)
                base = os.path.splitext(saved)[0]

                thumb_name = f"{base}-thumb.webp"
                _create_thumbnail(full_path, os.path.join(manga_dir, thumb_name), max_height=240, resample=Image.BILINEAR)

                webp_name = f"{base}.webp"
                webp_path = os.path.join(manga_dir, webp_name)
                if _create_thumbnail(full_path, webp_path, quality=95):
                    current_pages[-1] = webp_name
                    if saved != webp_name and os.path.exists(full_path):
                        try:
                            os.remove(full_path)
                        except:
                            pass
                start_idx += 1
                added_count += 1

    # Update DB
    try:
        conn.execute(
            """UPDATE mangas 
               SET title = ?, author = ?, description = ?, tags = ?, cover = ?, pages = ?
               WHERE slug = ?""",
            (new_title, new_author, new_description, json.dumps(tags), cover_name, json.dumps(current_pages), slug)
        )
        conn.commit()
        _cache.clear()
    except Exception as e:
        conn.close()
        return jsonify({"error": f"Ошибка сохранения: {str(e)}"}), 500
    finally:
        conn.close()

    return jsonify({
        "ok": True,
        "slug": slug,
        "title": new_title,
        "added_pages": added_count,
        "total_pages": len(current_pages)
    })


# @app.route("/api/delete_manga", methods=["POST"])
def api_delete_manga():
    password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
    if password != ADMIN_PASS:
        return jsonify({"error": "Неверный пароль администратора"}), 403

    user = get_current_user()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Требуются права администратора (активируйте админку в профиле)"}), 403

    slug = (request.form.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug обязателен"}), 400

    # Remove files
    try:
        shutil.rmtree(get_manga_dir(slug), ignore_errors=True)
    except Exception:
        pass

    conn = get_db()
    conn.execute("DELETE FROM mangas WHERE slug = ?", (slug,))
    conn.commit()
    conn.close()
    _cache.clear()

    return jsonify({"ok": True, "deleted": slug})


# @app.route("/api/delete_page", methods=["POST"])
def api_delete_page():
    password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
    if password != ADMIN_PASS:
        return jsonify({"error": "Неверный пароль администратора"}), 403

    user = get_current_user()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Требуются права администратора (активируйте админку в профиле)"}), 403

    slug = (request.form.get("slug") or "").strip()
    page = (request.form.get("page") or "").strip()   # e.g. "003.jpg"

    if not slug or not page:
        return jsonify({"error": "slug и page обязательны"}), 400

    row = get_manga_by_slug(slug)
    if not row:
        return jsonify({"error": "Манга не найдена"}), 404

    pages = json.loads(row["pages"] or "[]")
    if page not in pages:
        return jsonify({"error": "Страница не найдена"}), 404

    # Delete file + its thumb
    manga_dir = get_manga_dir(slug)
    try:
        ppath = os.path.join(manga_dir, page)
        if os.path.exists(ppath):
            os.remove(ppath)
    except Exception:
        pass

    # delete thumb version if present
    base = os.path.splitext(page)[0]
    thumb_p = f"{base}-thumb.webp"
    try:
        tpath = os.path.join(manga_dir, thumb_p)
        if os.path.exists(tpath):
            os.remove(tpath)
    except Exception:
        pass

    pages.remove(page)

    conn = get_db()
    conn.execute("UPDATE mangas SET pages = ? WHERE slug = ?", (json.dumps(pages), slug))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "remaining": len(pages)})


# All routes have been moved to the blueprints/ directory:
# - blueprints/main.py : public pages and some user routes
# - blueprints/admin.py : admin dashboard and bulk UI
# - blueprints/api.py : all API endpoints including add/edit/delete/bulk APIs

# Registration is handled inside create_app() at the top of this file.

# Static file serving and context processor moved into create_app for cleanliness.

# Re-bind all shared helpers from helpers.py right before app creation.
# There are large blocks of old duplicate function definitions later in this file
# (left as reference after split). Without this, they would shadow the canonical
# implementations (especially get_cover_url, get_page_urls, _create_thumbnail,
# _finalize_cover and image-related logic) causing broken image handling.
from helpers import (
    slugify, compute_rating, get_manga_by_slug, get_all_mangas,
    get_cover_url, get_page_urls, _create_thumbnail, save_uploaded_file,
    extract_zip_and_normalize, get_manga_dir, get_next_page_index, _finalize_cover,
    natural_sort_key,
    get_current_user, login_user, logout_user, update_user_history,
    get_user_tag_weights, get_recommendations,
    get_db, init_db, get_all_users,
    get_all_tags
)

app = create_app()

# -------------------- Startup --------------------
init_db()
# seed_demo_if_empty()  -- disabled for production (prevents demo placeholder from reappearing)
if __name__ == "__main__":
    init_db()
    # seed_demo_if_empty()  -- disabled for production
    import os
    print("Hentach - Hentai Manga Reader starting (modular split structure)...")
    print(f"PID: {os.getpid()}")
    print(f"Admin password: {ADMIN_PASS}")
    print(f"DB: {DB_PATH} (WAL + indexes enabled)")
    print("Open on this PC: http://127.0.0.1:5000")
    # For development only. For production use: gunicorn -w 4 -b 0.0.0.0:5000 app:app
    # With nginx in front for static/uploads, gzip, etc.
    # This setup + query limits + caching should handle 500+ concurrent read-heavy users comfortably on decent VPS.
    app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False, threaded=True)
