#!/usr/bin/env python3
"""
Bulk import script for FAKKU-like manga site.
Walks author/manga structure and uploads via /api/add_manga.

The scan folder is shared with the web UI (/admin/bulk):
- Change folder on the website (Bulk Import page) — the change is saved to bulk_root.txt
- This CLI script automatically uses the same folder (no need to edit code)

Usage:
1. Make sure the server is running (http://127.0.0.1:5000)
2. Login with an admin account (is_admin=1) — use the grant button in /profile if needed.
3. Optionally set folder via the website, or edit DEFAULT_MANGA_ROOT below.
4. Run: python bulk_import.py

It is resumable and skips already added titles (by title, case-insensitive).
For each manga folder:
- Uses folder name as title + author = parent folder name
- Supports TWO source types: loose images or .zip/.cbz inside the folder
- ALWAYS first page (natural sort) as cover
- Uploads via the site's /api/add_manga

Requirements: pip install requests
"""

import os
import re
import json
import tempfile
import zipfile
import shutil
from pathlib import Path
from typing import List, Set, Tuple

import requests

# ==================== CONFIG ====================
BASE_URL = "http://127.0.0.1:5000"
ADMIN_PASS = "admin123"          # the one used in forms / API

# Login credentials of a user that has is_admin=1
LOGIN_USERNAME = "demo_user"
LOGIN_PASSWORD = "demo"

# Shared with the web UI (bulk.html / app.py)
# If you set the folder in the web Bulk Import UI, this script will pick it up automatically.
BULK_ROOT_FILE = Path("bulk_root.txt")
DEFAULT_MANGA_ROOT = Path(os.environ.get("BULK_ROOT", "F:/Manga"))

def load_manga_root() -> Path:
    try:
        if BULK_ROOT_FILE.exists():
            p = Path(BULK_ROOT_FILE.read_text(encoding="utf-8").strip())
            if p.exists() and p.is_dir():
                return p
    except Exception:
        pass
    return DEFAULT_MANGA_ROOT

MANGA_ROOT = load_manga_root()

# Where to store progress (resumable)
PROGRESS_FILE = Path("bulk_import_progress.json")

# Image extensions we care about
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp"}

# Delay between uploads (seconds) to be nice to the server.
# Increased default helps keep the main site responsive during heavy imports
# (Pillow re-encoding of pages happens server-side on each upload).
DELAY_BETWEEN_UPLOADS = 1.0
# ===============================================


def natural_sort_key(text: str) -> List:
    """Sort strings with numbers naturally (001, 002, 10, 11...)."""
    return [int(c) if c.isdigit() else c.lower() for c in re.split(r'(\d+)', text)]


def get_image_files(folder: Path) -> List[Path]:
    """Return sorted list of image files in the folder."""
    files = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in IMAGE_EXTS
    ]
    files.sort(key=lambda p: natural_sort_key(p.name))
    return files


def choose_cover(images: List[Path]) -> Path:
    """Always take the first page (after natural sort) as the cover."""
    if not images:
        return None
    return images[0]


def create_pages_zip(images: List[Path], title: str) -> Path:
    """Create a temporary ZIP with the page images (excluding cover)."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    zip_path = Path(tmp.name)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for img in images:
            # Use original filename inside the zip
            zf.write(img, arcname=img.name)

    return zip_path


def get_archive_files(folder: Path) -> List[Path]:
    """Return sorted list of .zip / .cbz files directly in the folder."""
    exts = {".zip", ".cbz"}
    files = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in exts
    ]
    files.sort(key=lambda p: natural_sort_key(p.name))
    return files


def choose_best_archive(archives: List[Path], manga_name: str) -> Path:
    """Pick best archive. Prefer one whose name contains the manga folder name."""
    if not archives:
        return None
    lower = manga_name.lower()
    for a in archives:
        if lower in a.name.lower():
            return a
    return archives[0]


def extract_first_page_from_archive(archive_path: Path) -> Path:
    """
    Extract only the first naturally-sorted image from the archive
    into a temporary file. Returns the temp Path (caller must delete).
    Used to provide the required 'cover' file to the API.
    """
    if not archive_path.exists():
        return None
    try:
        with zipfile.ZipFile(archive_path, "r") as z:
            entries = []
            for name in z.namelist():
                if name.endswith("/"):
                    continue
                ext = '.' + name.rsplit(".", 1)[-1].lower()
                if ext in IMAGE_EXTS:
                    entries.append(name)
            if not entries:
                return None
            # Natural sort by basename (handles subfolders inside zip)
            entries.sort(key=lambda n: natural_sort_key(Path(n).name))
            first_name = entries[0]
            ext = first_name.rsplit(".", 1)[-1].lower()

            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=f".{ext}")
            tmp.close()
            tmp_path = Path(tmp.name)

            with z.open(first_name) as src, open(tmp_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            return tmp_path
    except Exception as e:
        print(f"  Warning: could not extract first page from {archive_path.name}: {e}")
        return None


def count_images_in_archive(archive_path: Path) -> int:
    """Quick count of images inside a zip/cbz (for UI display)."""
    try:
        with zipfile.ZipFile(archive_path, "r") as z:
            return sum(
                1 for name in z.namelist()
                if not name.endswith("/") and ('.' + name.rsplit(".", 1)[-1].lower()) in IMAGE_EXTS
            )
    except Exception:
        return 0


def _force_wal_checkpoint(db_path="data/manga.db"):
    """Best-effort WAL checkpoint after heavy imports.
    Prevents the main site from appearing hung after lots of writes.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(db_path)
        conn.execute("PRAGMA wal_checkpoint(FULL);")
        conn.close()
    except Exception:
        pass  # non-critical


def get_existing_titles(session: requests.Session) -> Set[str]:
    """Fetch already imported titles (case-insensitive)."""
    try:
        r = session.get(f"{BASE_URL}/api/mangas", timeout=30)
        r.raise_for_status()
        data = r.json()
        return {item["title"].lower() for item in data}
    except Exception as e:
        print(f"Warning: could not fetch existing titles: {e}")
        return set()


def login(session: requests.Session) -> bool:
    """Login and keep the session."""
    print(f"Logging in as {LOGIN_USERNAME}...")
    try:
        resp = session.post(
            f"{BASE_URL}/login",
            data={"username": LOGIN_USERNAME, "password": LOGIN_PASSWORD},
            timeout=10,
        )
        if resp.status_code == 200 and "logout" in resp.text.lower():
            print("Login successful.")
            return True
        print(f"Login failed (status={resp.status_code}). Check credentials and is_admin flag.")
        return False
    except Exception as e:
        print(f"Login error: {e}")
        return False


def upload_manga(
    session: requests.Session,
    title: str,
    author: str,
    cover_path: Path,
    pages_zip: Path,
) -> bool:
    """Upload one manga using the site's API."""
    tags = ""
    data = {
        "password": ADMIN_PASS,
        "title": title,
        "author": author,
        "tags": tags,
        "description": f"Imported from folder structure (author: {author})",
    }

    files = {
        "cover": (cover_path.name, open(cover_path, "rb"), "image/*"),
        "zipfile": (f"{title}.zip", open(pages_zip, "rb"), "application/zip"),
    }

    try:
        resp = session.post(
            f"{BASE_URL}/api/add_manga",
            data=data,
            files=files,
            timeout=300,  # generous timeout for large ZIPs
        )
        if resp.status_code == 200:
            j = resp.json()
            if j.get("ok"):
                print(f"  ✓ Added: {title} (slug: {j.get('slug')})")
                return True
            else:
                print(f"  ✗ Server error for {title}: {j.get('error')}")
        else:
            print(f"  ✗ HTTP {resp.status_code} for {title}: {resp.text[:200]}")
    except Exception as e:
        print(f"  ✗ Exception while uploading {title}: {e}")
    finally:
        # close file handles
        for f in files.values():
            try:
                f[1].close()
            except:
                pass

    return False


def main():
    if not MANGA_ROOT.exists():
        print(f"ERROR: Manga root not found: {MANGA_ROOT}")
        return

    # Load progress (set of "author/manga" strings)
    processed: Set[str] = set()
    if PROGRESS_FILE.exists():
        try:
            processed = set(json.loads(PROGRESS_FILE.read_text(encoding="utf-8")))
            print(f"Loaded {len(processed)} already processed entries from {PROGRESS_FILE}")
        except Exception:
            print("Could not read progress file, starting fresh.")

    session = requests.Session()
    if not login(session):
        return

    existing_titles = get_existing_titles(session)
    print(f"Found {len(existing_titles)} titles already in the database.")

    total_added = 0
    total_skipped = 0
    total_errors = 0

    for author_dir in sorted(MANGA_ROOT.iterdir()):
        if not author_dir.is_dir():
            continue
        author = author_dir.name

        for item in sorted(author_dir.iterdir()):
            if item.is_dir():
                # manga as subfolder
                manga_dir = item
                key = f"{author}/{manga_dir.name}"
                title = manga_dir.name

                if key in processed:
                    print(f"Skipping (already processed locally): {key}")
                    total_skipped += 1
                    continue

                if title.lower() in existing_titles:
                    print(f"Skipping (title already exists in DB): {title}")
                    processed.add(key)
                    total_skipped += 1
                    continue

                images = get_image_files(manga_dir)
                archives = get_archive_files(manga_dir)

                # Prefer archive if present (for "manga in archive" case).
                # Fall back to loose images if no archive and >=2 images.
                source_type = None
                cover_path = None
                zip_path = None
                temps_to_cleanup = []

                if archives:
                    source_type = "archive"
                    archive_path = choose_best_archive(archives, title)
                    print(f"\nProcessing: {author} / {title} (archive inside folder: {archive_path.name})")

                    cover_path = extract_first_page_from_archive(archive_path)
                    if not cover_path:
                        print(f"  Skipping (could not read images from archive): {key}")
                        processed.add(key)
                        total_skipped += 1
                        continue

                    if cover_path:
                        temps_to_cleanup.append(cover_path)
                    zip_path = archive_path

                elif len(images) >= 2:
                    source_type = "loose"
                    print(f"\nProcessing: {author} / {title} ({len(images)} loose images)")

                    cover_path = choose_cover(images)
                    pages = [p for p in images if p != cover_path]

                    zip_path = create_pages_zip(pages, title)
                    temps_to_cleanup.append(zip_path)

                else:
                    print(f"Skipping (no usable images or archive): {key}")
                    processed.add(key)
                    total_skipped += 1
                    continue

            elif item.is_file() and item.suffix.lower() in {".zip", ".cbz"}:
                # Direct zip/cbz representing the manga (common structure: Author/MangaTitle.zip)
                key = f"{author}/{item.stem}"
                title = item.stem

                if key in processed:
                    print(f"Skipping (already processed locally): {key}")
                    total_skipped += 1
                    continue

                if title.lower() in existing_titles:
                    print(f"Skipping (title already exists in DB): {title}")
                    processed.add(key)
                    total_skipped += 1
                    continue

                print(f"\nProcessing: {author} / {title} (direct archive: {item.name})")

                cover_path = extract_first_page_from_archive(item)
                if not cover_path:
                    print(f"  Skipping (could not read images from archive): {key}")
                    processed.add(key)
                    total_skipped += 1
                    continue

                source_type = "archive"
                zip_path = item  # use the zip directly
                temps_to_cleanup = []
                if cover_path:
                    temps_to_cleanup.append(cover_path)

            else:
                continue

            # common upload part (for both folder-based and direct zip)
            success = False
            try:
                success = upload_manga(
                    session,
                    title=title,
                    author=author,
                    cover_path=cover_path,
                    pages_zip=zip_path,
                )

                if success:
                    total_added += 1
                    processed.add(key)
                    PROGRESS_FILE.write_text(
                        json.dumps(sorted(processed), ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                    _force_wal_checkpoint()
                else:
                    total_errors += 1

            except Exception as e:
                print(f"  ✗ Unexpected error: {e}")
                total_errors += 1
            finally:
                for tf in temps_to_cleanup:
                    if tf and tf.exists():
                        try:
                            tf.unlink()
                        except:
                            pass

            # be polite to the server + give the main site breathing room after heavy Pillow work
            import time
            time.sleep(max(1.0, DELAY_BETWEEN_UPLOADS))

    print("\n" + "=" * 50)
    print(f"Done. Added: {total_added}, Skipped: {total_skipped}, Errors: {total_errors}")
    print(f"Progress saved to {PROGRESS_FILE}")

    _force_wal_checkpoint()


if __name__ == "__main__":
    main()
