"""
Common helpers extracted from app.py for better organization.
Pure functions and small utilities.
"""

import os
import re
import json
import sqlite3
import uuid
import tempfile
import zipfile
from pathlib import Path
from typing import List, Optional

from werkzeug.utils import secure_filename

def natural_sort_key(text: str):
    """Sort strings with numbers naturally (1, 2, 10, 11...)."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]

# These will be set by app.py after import
UPLOAD_DIR = None
DB_PATH = None
HAS_PIL = False
Image = None  # PIL.Image if available

ALLOWED_COVER = {"png", "jpg", "jpeg", "webp", "gif"}
ALLOWED_PAGE = {"png", "jpg", "jpeg", "webp", "gif"}
ALLOWED_ZIP = {"zip", "cbz"}

def init_helpers(upload_dir: str, db_path: str, has_pil: bool, pil_image=None):
    """Initialize module-level paths and PIL reference (called from app.py)."""
    global UPLOAD_DIR, DB_PATH, HAS_PIL, Image
    UPLOAD_DIR = upload_dir
    DB_PATH = db_path
    HAS_PIL = has_pil
    Image = pil_image


def slugify(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"[^a-z0-9\s-]", "", text)
    text = re.sub(r"[\s-]+", "-", text)
    return text or "manga-" + str(uuid.uuid4())[:8]


def compute_rating(row) -> tuple[float, int]:
    if row["rating_count"] == 0:
        return 0.0, 0
    avg = round(row["rating_sum"] / row["rating_count"], 1)
    return avg, row["rating_count"]


def get_manga_dir(slug: str) -> str:
    if UPLOAD_DIR is None:
        raise RuntimeError("helpers not initialized")
    d = os.path.join(UPLOAD_DIR, slug)
    os.makedirs(d, exist_ok=True)
    return d


def get_next_page_index(slug: str) -> int:
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


def _create_thumbnail(
    src_path: str,
    thumb_path: str,
    max_width: Optional[int] = None,
    max_height: Optional[int] = None,
    quality: int = 90,
    resample=None,
) -> bool:
    """Resize/re-encode image to WebP using Pillow. Falls back silently if Pillow unavailable."""
    if not HAS_PIL or not os.path.exists(src_path) or Image is None:
        return False

    try:
        if resample is None:
            resample = Image.LANCZOS

        with Image.open(src_path) as im:
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

            if not thumb_path.lower().endswith(".webp"):
                thumb_path = os.path.splitext(thumb_path)[0] + ".webp"

            im.save(thumb_path, "WEBP", quality=quality, method=6, lossless=False)
        return True
    except Exception as e:
        # logger would be better, but we keep it simple here
        print(f"Thumbnail generation failed for {src_path}: {e}")
        return False


def save_uploaded_file(file, dest_dir: str, prefix: str = "file") -> Optional[str]:
    if not file or not file.filename:
        return None
    filename = secure_filename(file.filename)
    ext = filename.rsplit(".", 1)[-1].lower()
    # Allow common image and zip types
    allowed = {"png", "jpg", "jpeg", "webp", "gif", "bmp", "zip", "cbz"}
    if ext not in allowed:
        return None
    unique = f"{prefix}-{uuid.uuid4().hex[:8]}.{ext}"
    path = os.path.join(dest_dir, unique)
    file.save(path)
    return unique


def extract_zip_and_normalize(zip_file, dest_dir: str, prefix: str = "page"):
    """Extract images from zip, rename sequentially 001.jpg etc. Return list of filenames."""
    pages = []
    temp_zip = os.path.join(dest_dir, "_temp.zip")
    zip_file.save(temp_zip)

    with zipfile.ZipFile(temp_zip, "r") as z:
        entries = []
        for name in z.namelist():
            if name.endswith("/"):
                continue
            ext = name.rsplit(".", 1)[-1].lower()
            if ext in {"png", "jpg", "jpeg", "webp", "gif"}:  # ALLOWED_PAGE without bmp usually
                entries.append(name)
        # Use natural sort so numbered pages like 1.jpg, 2.jpg, 10.jpg stay in order
        # (nhentai and many archives don't use leading zeros)
        entries.sort(key=lambda n: natural_sort_key(Path(n).name))

        idx = 1
        for entry in entries:
            ext = entry.rsplit(".", 1)[-1].lower()
            out_name = f"{idx:03d}.{ext}"
            out_path = os.path.join(dest_dir, out_name)
            with z.open(entry) as src, open(out_path, "wb") as dst:
                import shutil
                shutil.copyfileobj(src, dst)
            pages.append(out_name)

            base = os.path.splitext(out_name)[0]

            # Generate small thumbnail
            thumb_name = f"{base}-thumb.webp"
            thumb_path = os.path.join(dest_dir, thumb_name)
            _create_thumbnail(out_path, thumb_path, max_height=240, resample=getattr(Image, 'BILINEAR', None) if Image else None)

            # Re-encode full page as high-quality WebP
            if not out_name.lower().endswith('.webp'):
                webp_name = f"{base}.webp"
                webp_path = os.path.join(dest_dir, webp_name)
                if _create_thumbnail(out_path, webp_path, quality=95):
                    pages[-1] = webp_name
                    if out_name != webp_name and os.path.exists(out_path):
                        try:
                            os.remove(out_path)
                        except:
                            pass

            idx += 1

    try:
        os.remove(temp_zip)
    except:
        pass
    return pages


def get_cover_url(row, thumb: bool = False) -> str:
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

    if not slug or not cover or UPLOAD_DIR is None:
        return cover or ""

    if thumb:
        thumb_name = "cover-thumb.webp"
        thumb_path = os.path.join(UPLOAD_DIR, slug, thumb_name)
        if os.path.exists(thumb_path):
            return f"/uploads/manga/{slug}/{thumb_name}"

    # Prefer optimized main cover
    main_name = "cover-main.webp"
    main_path = os.path.join(UPLOAD_DIR, slug, main_name)
    if os.path.exists(main_path):
        return f"/uploads/manga/{slug}/{main_name}"

    # Auto-generate if current cover is large (one-time)
    orig_path = os.path.join(UPLOAD_DIR, slug, cover)
    if os.path.exists(orig_path):
        try:
            from PIL import Image as PILImage
            with PILImage.open(orig_path) as im:
                if im.width > 800 or im.height > 1000:
                    if _create_thumbnail(orig_path, main_path, max_width=800, quality=92, resample=PILImage.LANCZOS):
                        thumb_p = os.path.join(UPLOAD_DIR, slug, "cover-thumb.webp")
                        if not os.path.exists(thumb_p):
                            _create_thumbnail(main_path, thumb_p, max_width=320, resample=PILImage.BILINEAR)
                        return f"/uploads/manga/{slug}/{main_name}"
        except Exception:
            pass

    return f"/uploads/manga/{slug}/{cover}"


def get_page_urls(slug: str, pages: list, thumb: bool = False) -> list:
    """Convert internal page list to public URLs. Prefers *-thumb.webp when thumb=True."""
    if not slug or UPLOAD_DIR is None:
        return pages or []
    resolved = []
    for p in (pages or []):
        if isinstance(p, str) and p.startswith(("http://", "https://")):
            resolved.append(p)
            continue
        if thumb:
            base = os.path.splitext(p)[0]
            thumb_name = f"{base}-thumb.webp"
            thumb_path = os.path.join(UPLOAD_DIR, slug, thumb_name)
            if os.path.exists(thumb_path):
                resolved.append(f"/uploads/manga/{slug}/{thumb_name}")
                continue
        resolved.append(f"/uploads/manga/{slug}/{p}")
    return resolved


def _finalize_cover(manga_dir: str, initial_cover_name: str) -> str:
    """Common cover finalization: WebP + cover-main (800px) + thumb."""
    cover_name = initial_cover_name
    cover_full_path = os.path.join(manga_dir, cover_name)

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

    # Main cover resized for detail (prevents rippling)
    main_cover_path = os.path.join(manga_dir, "cover-main.webp")
    if _create_thumbnail(cover_full_path, main_cover_path, max_width=800, quality=92, resample=Image.LANCZOS if Image else None):
        try:
            if cover_full_path != main_cover_path and os.path.exists(cover_full_path):
                os.remove(cover_full_path)
        except:
            pass
        cover_name = "cover-main.webp"
        cover_full_path = main_cover_path

    # Thumbnail
    _create_thumbnail(
        cover_full_path,
        os.path.join(manga_dir, "cover-thumb.webp"),
        max_width=320,
        resample=Image.BILINEAR if Image else None,
    )

    return cover_name


# User session helpers
def get_current_user():
    # Note: requires Flask session, so call from within request context
    from flask import session
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
    from flask import session
    session['user_id'] = user_id
    session.permanent = True

def logout_user():
    from flask import session
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

    if completed:
        # Invalidate recommendations + tag weights cache so finished manga disappears from recs/home immediately
        for k in list(_cache.keys()):
            if str(k).startswith(f"recs:{user_id}") or str(k).startswith(f"tag_weights:{user_id}"):
                _cache.pop(k, None)


# Recommendations and cache
_cache = {}
_CACHE_TTL = 45  # seconds

def _get_cached(key, func, *args, **kwargs):
    import time
    now = time.time()
    entry = _cache.get(key)
    if entry and now - entry["ts"] < _CACHE_TTL:
        return entry["val"]
    val = func(*args, **kwargs)
    _cache[key] = {"val": val, "ts": now}
    return val

def get_user_tag_weights(user_id):
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
            w += 4
        if row['score'] >= 5:
            w += 3
        elif row['score'] == 4:
            w += 1
        elif row['score'] == 2:
            w -= 1
        elif row['score'] == 1:
            w -= 2
        for t in tags:
            tag_weights[t] = tag_weights.get(t, 0) + w

    return tag_weights

def _compute_recommendations(user_id, limit=8):
    tag_weights = _compute_user_tag_weights(user_id)

    total_signal = sum(tag_weights.values())
    if total_signal < 5:
        conn = get_db()
        read_ids = {r['manga_id'] for r in conn.execute(
            "SELECT manga_id FROM user_history WHERE user_id=? AND completed=1", (user_id,)
        ).fetchall()}

        recs = conn.execute("""
            SELECT *, 
                   (rating_sum * 1.0 / NULLIF(rating_count, 0)) as avg_rating
            FROM mangas
            ORDER BY avg_rating DESC, id DESC
        """).fetchall()

        result = []
        for r in recs:
            if r['id'] in read_ids:
                continue
            r = dict(r)
            r["cover"] = get_cover_url(r, thumb=True)
            result.append(r)
            if len(result) >= limit:
                break
        conn.close()
        return result

    conn = get_db()
    read_ids = {r['manga_id'] for r in conn.execute(
        "SELECT manga_id FROM user_history WHERE user_id=? AND completed=1", (user_id,)
    ).fetchall()}

    candidates = conn.execute("SELECT * FROM mangas").fetchall()
    scored = []
    for m in candidates:
        if m['id'] in read_ids:
            continue
        tags = json.loads(m['tags'] or '[]')
        score = sum(tag_weights.get(t, 0) for t in tags)

        matching_positive = sum(1 for t in tags if tag_weights.get(t, 0) > 0)
        if matching_positive >= 3:
            bonus = (matching_positive - 2) * 2
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
    return result

def get_recommendations(user_id, limit=8):
    if not user_id:
        return []

    def _compute_recs(uid, lim):
        return _compute_recommendations(uid, lim)

    return _get_cached(f"recs:{user_id}:{limit}", _compute_recs, user_id, limit)


# -------------------- DB helpers (moved for split) --------------------
def _configure_connection(conn):
    """Apply performance and stability pragmas."""
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")  # ~64MB cache
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn

def get_db():
    """Always fresh connection with performance pragmas applied.
    Simple and stable for this app size. (g-based reuse can be added later if needed)"""
    if DB_PATH is None:
        raise RuntimeError("helpers not initialized with DB_PATH")
    conn = sqlite3.connect(DB_PATH)
    return _configure_connection(conn)

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""
        CREATE TABLE IF NOT EXISTS mangas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            title TEXT NOT NULL,
            author TEXT DEFAULT '',
            description TEXT DEFAULT '',
            cover TEXT NOT NULL,
            pages TEXT NOT NULL,
            tags TEXT NOT NULL,
            rating_sum INTEGER DEFAULT 0,
            rating_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL,
            is_premium INTEGER DEFAULT 0,
            avatar TEXT DEFAULT '',
            username_color TEXT DEFAULT '#e11d48',
            is_admin INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_favorites (
            user_id INTEGER,
            manga_id INTEGER,
            added_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, manga_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_ratings (
            user_id INTEGER,
            manga_id INTEGER,
            score INTEGER,
            rated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, manga_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS user_history (
            user_id INTEGER,
            manga_id INTEGER,
            last_page INTEGER DEFAULT 1,
            completed INTEGER DEFAULT 0,
            last_read_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (user_id, manga_id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            manga_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Forum tables
    c.execute("""
        CREATE TABLE IF NOT EXISTS forum_categories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS forum_forums (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            category_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            display_order INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (category_id) REFERENCES forum_categories(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS forum_topics (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            forum_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            title TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            last_post_at TEXT DEFAULT CURRENT_TIMESTAMP,
            is_pinned INTEGER DEFAULT 0,
            is_locked INTEGER DEFAULT 0,
            FOREIGN KEY (forum_id) REFERENCES forum_forums(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS forum_posts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            content TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
            attachment TEXT DEFAULT '',
            FOREIGN KEY (topic_id) REFERENCES forum_topics(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)
    c.execute("""
        CREATE TABLE IF NOT EXISTS forum_post_votes (
            post_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            vote INTEGER NOT NULL, -- 1 = up, -1 = down
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (post_id, user_id),
            FOREIGN KEY (post_id) REFERENCES forum_posts(id),
            FOREIGN KEY (user_id) REFERENCES users(id)
        )
    """)

    # Performance indexes (safe, IF NOT EXISTS)
    c.execute("CREATE INDEX IF NOT EXISTS idx_mangas_slug ON mangas(slug)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mangas_created ON mangas(created_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mangas_title ON mangas(title)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mangas_author ON mangas(author)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_mangas_rating ON mangas(rating_sum, rating_count)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_ratings_user ON user_ratings(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_favorites_user ON user_favorites(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_history_user ON user_history(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_user_history_completed ON user_history(user_id, completed)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_comments_manga ON comments(manga_id)")

    # Forum indexes
    c.execute("CREATE INDEX IF NOT EXISTS idx_forum_forums_category ON forum_forums(category_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_forum_topics_forum ON forum_topics(forum_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_forum_topics_last ON forum_topics(last_post_at DESC)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_forum_posts_topic ON forum_posts(topic_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_forum_posts_user ON forum_posts(user_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_forum_post_votes_post ON forum_post_votes(post_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_forum_post_votes_user ON forum_post_votes(user_id)")

    # Safe column additions for existing databases (robust check)
    try:
        mangas_cols = [row[1] for row in c.execute("PRAGMA table_info(mangas)").fetchall()]
        if "author" not in mangas_cols:
            c.execute("ALTER TABLE mangas ADD COLUMN author TEXT DEFAULT ''")
    except Exception:
        pass

    try:
        for stmt in [
            "ALTER TABLE users ADD COLUMN is_premium INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN avatar TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN username_color TEXT DEFAULT '#e11d48'",
            "ALTER TABLE users ADD COLUMN is_admin INTEGER DEFAULT 0",
            "ALTER TABLE users ADD COLUMN created_at TEXT DEFAULT CURRENT_TIMESTAMP",
            "ALTER TABLE users ADD COLUMN banned_until TEXT DEFAULT ''",
            "ALTER TABLE users ADD COLUMN showcase_public INTEGER DEFAULT 1",
        ]:
            try:
                c.execute(stmt)
            except Exception:
                pass
    except Exception:
        pass

    # Forum attachment column (safe)
    try:
        posts_cols = [row[1] for row in c.execute("PRAGMA table_info(forum_posts)").fetchall()]
        if "attachment" not in posts_cols:
            c.execute("ALTER TABLE forum_posts ADD COLUMN attachment TEXT DEFAULT ''")
    except Exception:
        pass

    conn.commit()
    conn.close()

def get_manga_by_slug(slug):
    conn = get_db()
    row = conn.execute("SELECT * FROM mangas WHERE slug = ?", (slug,)).fetchone()
    conn.close()
    return row

def get_all_mangas(search=None, tag=None, limit=None, offset=0):
    conn = get_db()
    query = "SELECT * FROM mangas"
    params = []
    clauses = []
    if search:
        clauses.append("(title LIKE ? OR description LIKE ? OR author LIKE ?)")
        params.extend([f"%{search}%", f"%{search}%", f"%{search}%"])
    if tag:
        # Simple json match
        clauses.append("tags LIKE ?")
        params.append(f'%"{tag}"%')
    if clauses:
        query += " WHERE " + " AND ".join(clauses)
    query += " ORDER BY created_at DESC"
    if limit:
        query += " LIMIT ? OFFSET ?"
        params.extend([limit, offset])
    rows = conn.execute(query, params).fetchall()
    conn.close()
    return rows

def get_all_users():
    conn = get_db()
    users = conn.execute("SELECT id, username FROM users ORDER BY username").fetchall()
    conn.close()
    return users

# Simple cached tags for search UI (avoids full scan every time)
_tags_cache = {"ts": 0, "tags": []}
_TAGS_CACHE_TTL = 300  # 5 min

def get_all_tags():
    import time
    now = time.time()
    if now - _tags_cache["ts"] < _TAGS_CACHE_TTL and _tags_cache["tags"]:
        return _tags_cache["tags"]
    conn = get_db()
    rows = conn.execute("SELECT tags FROM mangas").fetchall()
    conn.close()
    tag_set = set()
    for r in rows:
        for t in (json.loads(r["tags"] or "[]")):
            tag_set.add(t)
    _tags_cache["tags"] = sorted(tag_set)
    _tags_cache["ts"] = now
    return _tags_cache["tags"]