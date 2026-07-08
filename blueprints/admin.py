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
        get_manga_by_slug, ADMIN_PASS,
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

# Legacy bulk system fully removed (replaced by mass folder import + multiple ZIP in add form)

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
        return jsonify({"error": "Папка не найдена"}), 400
    _load_mass_state()
    mass_stop_requested = False
    mass_import_state["running"] = True
    mass_import_state["total"] = 0
    mass_import_state["done"] = 0
    mass_import_state["added"] = 0
    mass_import_state["current"] = "Начинаем..."
    mass_import_state["error"] = None
    _save_mass_state()
    import threading
    t = threading.Thread(target=_process_mass_import, args=(root_str,), daemon=True)
    t.start()
    return jsonify({"ok": True, "message": "Import started in background"})

@admin_bp.route("/api/mass_import/status")
def api_mass_import_status():
    _load_mass_state()
    return jsonify(mass_import_state)

@admin_bp.route("/api/mass_import/stop", methods=["POST"])
def api_mass_import_stop():
    user = _get_shared()['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403
    mass_stop_requested = True
    mass_import_state["current"] = "Остановка..."
    _save_mass_state()
    return jsonify({"ok": True})


mass_import_state = {
    "running": False,
    "total": 0,
    "done": 0,
    "current": "",
    "added": 0,
    "error": None
}
mass_stop_requested = False

MASS_PROGRESS_FILE = Path("mass_import_progress.json")

def _save_mass_state():
    try:
        MASS_PROGRESS_FILE.write_text(json.dumps(mass_import_state, ensure_ascii=False))
    except Exception:
        pass

def _load_mass_state():
    try:
        if MASS_PROGRESS_FILE.exists():
            data = json.loads(MASS_PROGRESS_FILE.read_text())
            for k in mass_import_state:
                if k in data:
                    mass_import_state[k] = data[k]
    except Exception:
        pass

_load_mass_state()  # load persisted on startup

def _process_mass_import(root_str):
    global mass_import_state, mass_stop_requested
    mass_stop_requested = False
    mass_import_state["running"] = True
    mass_import_state["error"] = None
    mass_import_state["added"] = 0
    mass_import_state["done"] = 0
    mass_import_state["current"] = ""
    _save_mass_state()
    try:
        root = Path(root_str)
        if not root.exists() or not root.is_dir():
            print("Mass import: invalid root")
            return
        # count total items for progress
        total = 0
        for author_dir in sorted(root.iterdir()):
            if not author_dir.is_dir(): continue
            for item in sorted(author_dir.iterdir()):
                if (item.is_dir() or 
                    (item.is_file() and item.suffix.lower() in {'.zip', '.cbz'})):
                    total += 1
        mass_import_state["total"] = total
        mass_import_state["done"] = 0
        _save_mass_state()
        from app import slugify as _slugify, get_manga_dir as _get_manga_dir, get_db as _get_db
        from helpers import extract_zip_and_normalize as _extract, _finalize_cover as _final, _create_thumbnail as _thumb, natural_sort_key as _natural_sort
        conn = _get_db()
        added = 0
        try:
            for author_dir in sorted(root.iterdir()):
                if not author_dir.is_dir(): continue
                author = author_dir.name
                for item in sorted(author_dir.iterdir()):
                    try:
                        if mass_stop_requested:
                            mass_import_state["current"] = "Остановлено пользователем"
                            break
                        if item.is_dir():
                            title = item.name
                            author = author  # from parent
                            description = ""
                            tags_list = []
                            orig_title = ""
                            # Tags.txt support
                            try:
                                tags_f = item / "Tags.txt"
                                if tags_f.exists():
                                    for line in tags_f.read_text(encoding='utf-8', errors='ignore').splitlines():
                                        if ':' in line:
                                            k, v = [x.strip() for x in line.split(':', 1)]
                                            kl = k.lower()
                                            if kl in ('name', 'title'):
                                                title = v
                                            elif kl in ('original', 'orig'):
                                                orig_title = v
                                            elif kl in ('artist', 'author'):
                                                author = v
                                            elif kl in ('descr', 'discr', 'description'):
                                                description = v
                                            elif kl == 'tags':
                                                tags_list = [t.strip() for t in v.split(',') if t.strip()]
                            except:
                                pass
                            exts = {'.webp', '.jpg', '.jpeg', '.png', '.gif'}
                            images = sorted([f for f in item.iterdir() if f.is_file() and f.suffix.lower() in exts], key=lambda f: _natural_sort(f.name))
                            if not images: continue
                            pages = []
                            manga_slug = _slugify(title)
                            ex = conn.execute("SELECT id FROM mangas WHERE slug=?", (manga_slug,)).fetchone()
                            if ex: manga_slug = f"{manga_slug}-{uuid.uuid4().hex[:6]}"
                            manga_dir = _get_manga_dir(manga_slug)
                            for i, imgf in enumerate(images, 1):
                                new_name = f"{i:03d}{imgf.suffix.lower()}"
                                shutil.copy2(str(imgf), os.path.join(manga_dir, new_name))
                                pages.append(new_name)
                            cover_name = pages[0]
                            cover_src = os.path.join(manga_dir, cover_name)
                            temp_cover = "cover" + os.path.splitext(cover_name)[1]
                            shutil.copy2(cover_src, os.path.join(manga_dir, temp_cover))
                            cover_name = temp_cover
                            if _thumb:
                                for p in pages:
                                    _thumb(os.path.join(manga_dir, p), os.path.join(manga_dir, f"{os.path.splitext(p)[0]}-thumb.webp"), max_height=240)
                            if _final:
                                try:
                                    c = _final(manga_dir, cover_name)
                                    if c: cover_name = c
                                except: pass
                            conn.execute("INSERT INTO mangas (slug, title, author, original_title, description, cover, pages, tags, rating_sum, rating_count) VALUES (?,?,?,?,?,?,?,?,0,0)", (manga_slug, title, author, orig_title or "", description or "", cover_name, json.dumps(pages), json.dumps(tags_list or [])))
                            conn.commit()
                            added += 1
                            mass_import_state["added"] = added
                            _save_mass_state()
                        elif item.is_file() and item.suffix.lower() in {'.zip','.cbz'}:
                            title = item.stem
                            author = author
                            description = ""
                            tags_list = []
                            orig_title = ""
                            # Tags.txt support inside zip
                            try:
                                import zipfile
                                with zipfile.ZipFile(item) as zf:
                                    for n in zf.namelist():
                                        if n.lower().endswith('tags.txt') or 'tags.txt' in n.lower():
                                            content = zf.read(n).decode('utf-8', errors='ignore')
                                            for line in content.splitlines():
                                                if ':' in line:
                                                    k, v = [x.strip() for x in line.split(':', 1)]
                                                    kl = k.lower()
                                                    if kl in ('name', 'title'):
                                                        title = v
                                                    elif kl in ('original', 'orig'):
                                                        orig_title = v
                                                    elif kl in ('artist', 'author'):
                                                        author = v
                                                    elif kl in ('descr', 'discr', 'description'):
                                                        description = v
                                                    elif kl == 'tags':
                                                        tags_list = [t.strip() for t in v.split(',') if t.strip()]
                                            break
                            except:
                                pass
                            manga_slug = _slugify(title)
                            ex = conn.execute("SELECT id FROM mangas WHERE slug=?", (manga_slug,)).fetchone()
                            if ex: manga_slug = f"{manga_slug}-{uuid.uuid4().hex[:6]}"
                            manga_dir = _get_manga_dir(manga_slug)
                            class F:
                                def __init__(self,p): self.filename=item.name; self._path=p
                                def save(self,d): shutil.copy2(self._path, d)
                            pages = _extract(F(str(item)), manga_dir, "p")
                            if not pages: continue
                            cover_name = pages[0]
                            cover_src = os.path.join(manga_dir, cover_name)
                            temp_cover = "cover" + os.path.splitext(cover_name)[1]
                            shutil.copy2(cover_src, os.path.join(manga_dir, temp_cover))
                            cover_name = temp_cover
                            if _thumb:
                                for p in pages:
                                    _thumb(os.path.join(manga_dir, p), os.path.join(manga_dir, f"{os.path.splitext(p)[0]}-thumb.webp"), max_height=240)
                            if _final:
                                try:
                                    c = _final(manga_dir, cover_name)
                                    if c: cover_name = c
                                except: pass
                            conn.execute("INSERT INTO mangas (slug, title, author, original_title, description, cover, pages, tags, rating_sum, rating_count) VALUES (?,?,?,?,?,?,?,?,0,0)", (manga_slug, title, author, orig_title or "", description or "", cover_name, json.dumps(pages), json.dumps(tags_list or [])))
                            conn.commit()
                            added += 1
                            mass_import_state["added"] = added
                            _save_mass_state()
                    except Exception as e: print("mass item err", e)
                    mass_import_state["done"] = mass_import_state.get("done", 0) + 1
                    mass_import_state["current"] = getattr(locals().get('item'), 'name', str(locals().get('item', '')))
                    _save_mass_state()
            print("Mass import bg done, added", added)
        finally:
            conn.close()
    except Exception as e: 
        print("mass bg fatal", e)
        mass_import_state["error"] = str(e)
    finally:
        mass_import_state["running"] = False
        mass_stop_requested = False
        _save_mass_state()

print("admin blueprint loaded")