from flask import Blueprint, render_template, request, redirect, url_for, jsonify, abort, current_app
import json
import os
import subprocess
import datetime
from pathlib import Path
import shutil
import uuid

admin_bp = Blueprint('admin', __name__)

def _get_shared():
    from app import (
        get_current_user, get_db, get_cover_url, get_all_mangas,
        get_manga_by_slug, ADMIN_PASS, _force_wal_checkpoint,
        slugify, get_manga_dir, extract_zip_and_normalize, save_uploaded_file, _finalize_cover,
        _create_thumbnail
    )
    d = locals()
    d['ADMIN_PASS'] = ADMIN_PASS
    return d

@admin_bp.route("/admin")
def admin_page():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return redirect(url_for('main.index'))

    conn = shared['get_db']()
    rows = conn.execute("SELECT id, slug, title, description, tags, cover FROM mangas ORDER BY created_at DESC").fetchall()
    conn.close()

    mangas = []
    for r in rows:
        tags = json.loads(r["tags"] or "[]")
        cover = shared['get_cover_url'](r, thumb=True)
        mangas.append({
            "id": r["id"],
            "slug": r["slug"],
            "title": r["title"],
            "author": getattr(r, 'author', '') or "",
            "description": r["description"] or "",
            "tags": tags,
            "cover": cover,
        })

    conn = shared['get_db']()
    rows2 = conn.execute("SELECT slug, pages FROM mangas").fetchall()
    pages_map = {r["slug"]: len(json.loads(r["pages"] or "[]")) for r in rows2}
    conn.close()

    for m in mangas:
        m["pages_count"] = pages_map.get(m["slug"], 0)

    from app import ADMIN_PASS
    return render_template("admin.html", admin_pass=ADMIN_PASS, mangas=mangas)

# Bulk removed - use multiple ZIP uploads in the main admin form instead

# Bulk scan/start etc removed

# Bulk removed - multiple ZIP upload in main form is the new way to add several mangas at once.

@admin_bp.route("/api/bulk/stop", methods=["POST"])
def api_bulk_stop():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403
    with shared['_bulk_lock']:
        shared['_bulk_stop_requested'] = True
        shared['_bulk_state']["running"] = False
    # Sync the module-level global that the worker function (defined in app.py) actually checks
    try:
        import app as appmod
        appmod._bulk_stop_requested = True
    except Exception:
        pass
    return jsonify({"ok": True})

@admin_bp.route("/api/bulk/status")
def api_bulk_status():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    with shared['_bulk_lock']:
        state_copy = {
            "running": shared['_bulk_state'].get("running", False),
            "total": shared['_bulk_state'].get("total", 0),
            "done": shared['_bulk_state'].get("done", 0),
            "current": shared['_bulk_state'].get("current", ""),
            "logs": list(shared['_bulk_state'].get("logs", [])),
            "items": list(shared['_bulk_state'].get("items", [])),
            "current_root": str(shared['get_bulk_root']()),
        }
    return jsonify(state_copy)

@admin_bp.route("/api/bulk/clear-progress", methods=["POST"])
def api_bulk_clear_progress():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403
    try:
        if shared['BULK_PROGRESS_FILE'].exists():
            shared['BULK_PROGRESS_FILE'].unlink()
        with shared['_bulk_lock']:
            shared['_append_bulk_log']("Progress file cleared. Rescan recommended.")
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@admin_bp.route("/api/bulk/set-root", methods=["POST"])
def api_bulk_set_root():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    if request.is_json:
        data = request.get_json(silent=True) or {}
    else:
        data = request.form
    path = (data.get("path") or "").strip()

    if not path:
        return jsonify({"error": "Укажите путь к папке"}), 400

    try:
        new_root = shared['set_bulk_root'](path)
        shared['_append_bulk_log'](f"📁 Папка сканирования изменена на: {new_root}")
        return jsonify({"ok": True, "root": str(new_root)})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ==================== FORUM ADMIN ====================

@admin_bp.route("/admin/forum")
def admin_forum():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return redirect(url_for('main.index'))

    get_db = shared['get_db']
    conn = get_db()

    categories = conn.execute("SELECT * FROM forum_categories ORDER BY display_order").fetchall()
    forums = {}
    for cat in categories:
        f = conn.execute("SELECT * FROM forum_forums WHERE category_id = ? ORDER BY display_order", (cat['id'],)).fetchall()
        forums[cat['id']] = f

    topics_count = conn.execute("SELECT COUNT(*) as c FROM forum_topics").fetchone()["c"]
    posts_count = conn.execute("SELECT COUNT(*) as c FROM forum_posts").fetchone()["c"]
    conn.close()

    return render_template("admin_forum.html", 
                           categories=categories, 
                           forums=forums,
                           topics_count=topics_count,
                           posts_count=posts_count,
                           current_user=user)

@admin_bp.route("/admin/forum/create_category", methods=["POST"])
def admin_forum_create_category():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    get_db = shared['get_db']
    name = request.form.get("name", "").strip()
    desc = request.form.get("description", "").strip()
    if not name:
        return jsonify({"error": "Название обязательно"}), 400

    conn = get_db()
    conn.execute("INSERT INTO forum_categories (name, description) VALUES (?, ?)", (name, desc))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@admin_bp.route("/admin/forum/create_forum", methods=["POST"])
def admin_forum_create_forum():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    get_db = shared['get_db']
    cat_id = int(request.form.get("category_id"))
    name = request.form.get("name", "").strip()
    desc = request.form.get("description", "").strip()
    if not name or not cat_id:
        return jsonify({"error": "Название и категория обязательны"}), 400

    conn = get_db()
    conn.execute("INSERT INTO forum_forums (category_id, name, description) VALUES (?, ?, ?)", (cat_id, name, desc))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@admin_bp.route("/admin/forum/delete_post", methods=["POST"])
def admin_forum_delete_post():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    post_id = int(request.form.get("post_id"))
    get_db = shared['get_db']
    conn = get_db()

    post = conn.execute("SELECT * FROM forum_posts WHERE id=?", (post_id,)).fetchone()
    if post:
        topic_id = post['topic_id']
        conn.execute("DELETE FROM forum_posts WHERE id=?", (post_id,))
        # update last activity or delete topic if empty
        remaining = conn.execute("SELECT COUNT(*) as c FROM forum_posts WHERE topic_id=?", (topic_id,)).fetchone()["c"]
        if remaining == 0:
            conn.execute("DELETE FROM forum_topics WHERE id=?", (topic_id,))
        else:
            last = conn.execute("SELECT created_at FROM forum_posts WHERE topic_id=? ORDER BY created_at DESC LIMIT 1", (topic_id,)).fetchone()
            if last:
                conn.execute("UPDATE forum_topics SET last_post_at=? WHERE id=?", (last['created_at'], topic_id))
        conn.commit()

    conn.close()
    return jsonify({"ok": True})

@admin_bp.route("/admin/users")
def admin_users():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return redirect(url_for('main.index'))

    get_db = shared['get_db']
    conn = get_db()

    # Get all users + stats
    users_raw = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    users = []
    for u in users_raw:
        uid = u['id']
        ratings_c = conn.execute("SELECT COUNT(*) as c FROM user_ratings WHERE user_id=?", (uid,)).fetchone()['c']
        favs_c = conn.execute("SELECT COUNT(*) as c FROM user_favorites WHERE user_id=?", (uid,)).fetchone()['c']
        comm_c = conn.execute("SELECT COUNT(*) as c FROM comments WHERE user_id=?", (uid,)).fetchone()['c']
        forum_c = conn.execute("SELECT COUNT(*) as c FROM forum_posts WHERE user_id=?", (uid,)).fetchone()['c']

        # tag weights
        try:
            from app import get_user_tag_weights
            weights = get_user_tag_weights(uid)
            top_w = sorted(weights.items(), key=lambda x: -x[1])[:4]
        except:
            top_w = []

        d = dict(u)
        d['ratings_count'] = ratings_c
        d['favs_count'] = favs_c
        d['comments_count'] = comm_c
        d['forum_posts_count'] = forum_c
        d['top_weights'] = top_w
        users.append(d)

    conn.close()
    return render_template("admin_users.html", users=users, current_user=user)


@admin_bp.route("/admin/api/set_vip", methods=["POST"])
def admin_set_vip():
    shared = _get_shared()
    admin = shared['get_current_user']()
    if not admin or not admin.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    user_id = int(request.form.get("user_id"))
    give = int(request.form.get("give", 1))

    conn = shared['get_db']()
    conn.execute("UPDATE users SET is_premium=? WHERE id=?", (give, user_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})


@admin_bp.route("/admin/api/ban_user", methods=["POST"])
def admin_ban_user():
    shared = _get_shared()
    admin = shared['get_current_user']()
    if not admin or not admin.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    user_id = int(request.form.get("user_id"))
    duration = (request.form.get("duration") or "24h").strip().lower()

    banned_until = ""
    if duration != "permanent":
        import datetime
        now = datetime.datetime.utcnow()
        if duration.endswith('h'):
            delta = datetime.timedelta(hours=int(duration[:-1]))
        elif duration.endswith('d'):
            delta = datetime.timedelta(days=int(duration[:-1]))
        else:
            delta = datetime.timedelta(hours=24)
        banned_until = (now + delta).isoformat()

    conn = shared['get_db']()
    conn.execute("UPDATE users SET banned_until=? WHERE id=?", (banned_until, user_id))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "banned_until": banned_until})

@admin_bp.route("/api/mass_import", methods=["POST"])
def api_mass_import():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    root_str = request.form.get("root") or ""
    if not root_str:
        return jsonify({"error": "Укажите корневую папку"}), 400

    root = Path(root_str)
    if not root.exists() or not root.is_dir():
        return jsonify({"error": "Папка не найдена или недоступна"}), 400

    slugify = shared.get('slugify')
    get_manga_dir = shared.get('get_manga_dir')
    extract_zip_and_normalize = shared.get('extract_zip_and_normalize')
    save_uploaded_file = shared.get('save_uploaded_file')
    _finalize_cover = shared.get('_finalize_cover')
    get_db = shared['get_db']

    if not all([slugify, get_manga_dir, extract_zip_and_normalize]):
        return jsonify({"error": "Не все функции доступны для импорта"}), 500

    added = 0
    conn = get_db()
    try:
        for author_dir in sorted(root.iterdir()):
            if not author_dir.is_dir():
                continue
            author = author_dir.name
            for item in sorted(author_dir.iterdir()):
                if item.is_dir():
                    title = item.name
                    exts = {'.webp', '.jpg', '.jpeg', '.png', '.gif'}
                    images = sorted(
                        [f for f in item.iterdir() if f.is_file() and f.suffix.lower() in exts],
                        key=lambda f: f.name
                    )
                    if not images:
                        continue
                    title = item.name
                    manga_slug = slugify(title)
                    existing = conn.execute("SELECT id FROM mangas WHERE slug = ?", (manga_slug,)).fetchone()
                    if existing:
                        manga_slug = f"{manga_slug}-{uuid.uuid4().hex[:6]}"
                    manga_dir = get_manga_dir(manga_slug)
                    pages = []
                    for i, imgf in enumerate(images, 1):
                        ext = imgf.suffix.lower()
                        new_name = f"{i:03d}{ext}"
                        dst = os.path.join(manga_dir, new_name)
                        shutil.copy2(str(imgf), dst)
                        pages.append(new_name)
                    cover_name = pages[0]
                    # duplicate first page as cover so finalize doesn't delete the page file
                    cover_src = os.path.join(manga_dir, cover_name)
                    temp_cover = "cover" + os.path.splitext(cover_name)[1]
                    shutil.copy2(cover_src, os.path.join(manga_dir, temp_cover))
                    cover_name = temp_cover
                    # create thumbs like normal add
                    if shared.get('_create_thumbnail'):
                        for p in pages:
                            full = os.path.join(manga_dir, p)
                            base = os.path.splitext(p)[0]
                            thumb_name = f"{base}-thumb.webp"
                            try:
                                shared['_create_thumbnail'](full, os.path.join(manga_dir, thumb_name), max_height=240)
                            except:
                                pass
                    if _finalize_cover:
                        try:
                            cfinal = _finalize_cover(manga_dir, cover_name)
                            if cfinal:
                                cover_name = cfinal
                        except:
                            pass
                    try:
                        conn.execute("""
                            INSERT INTO mangas (slug, title, author, description, cover, pages, tags, rating_sum, rating_count)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
                        """, (manga_slug, title, author, "", cover_name, json.dumps(pages), json.dumps([])))
                        conn.commit()
                        added += 1
                    except Exception as e:
                        pass
                elif item.is_file() and item.suffix.lower() in {'.zip', '.cbz'}:
                    title = item.stem
                    manga_slug = slugify(title)
                    existing = conn.execute("SELECT id FROM mangas WHERE slug = ?", (manga_slug,)).fetchone()
                    if existing:
                        manga_slug = f"{manga_slug}-{uuid.uuid4().hex[:6]}"
                    manga_dir = get_manga_dir(manga_slug)
                    try:
                        class FakeFile:
                            def __init__(self, path):
                                self.filename = item.name
                                self._path = path
                            def save(self, dst):
                                shutil.copy2(self._path, dst)
                        fake = FakeFile(str(item))
                        pages = extract_zip_and_normalize(fake, manga_dir, "p")
                        if not pages:
                            continue
                        cover_name = pages[0]
                        # duplicate to prevent finalize deleting the page
                        cover_src = os.path.join(manga_dir, cover_name)
                        temp_cover = "cover" + os.path.splitext(cover_name)[1]
                        shutil.copy2(cover_src, os.path.join(manga_dir, temp_cover))
                        cover_name = temp_cover
                        if _finalize_cover:
                            try:
                                cfinal = _finalize_cover(manga_dir, cover_name)
                                if cfinal:
                                    cover_name = cfinal
                            except:
                                pass
                        conn.execute("""
                            INSERT INTO mangas (slug, title, author, description, cover, pages, tags, rating_sum, rating_count)
                            VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
                        """, (manga_slug, title, author, "", cover_name, json.dumps(pages), json.dumps([])))
                        conn.commit()
                        added += 1
                    except Exception as e:
                        pass
    finally:
        conn.close()

    return jsonify({"ok": True, "added": added})

print("admin blueprint loaded")