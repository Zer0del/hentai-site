from flask import Blueprint, render_template, request, redirect, url_for, jsonify, abort, current_app
import json
import os

admin_bp = Blueprint('admin', __name__)

def _get_shared():
    from app import (
        get_current_user, get_db, get_cover_url, get_all_mangas,
        get_manga_by_slug, _append_bulk_log, _bulk_lock, _bulk_state,
        _bulk_stop_requested,
        _bulk_import_worker,
        BULK_PROGRESS_FILE, ADMIN_PASS, get_bulk_root, _force_wal_checkpoint,
        HAS_BULK_HELPERS, _scan_bulk_candidates, set_bulk_root
    )
    d = locals()
    d['ADMIN_PASS'] = ADMIN_PASS
    d['BULK_PROGRESS_FILE'] = BULK_PROGRESS_FILE
    d['set_bulk_root'] = set_bulk_root
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

@admin_bp.route("/admin/bulk")
def admin_bulk_page():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return redirect(url_for('main.index'))
    current_root = shared['get_bulk_root']()
    root_exists = current_root.exists()
    from app import ADMIN_PASS
    return render_template(
        "bulk.html",
        bulk_root=str(current_root),
        root_exists=root_exists,
        admin_pass=ADMIN_PASS
    )

@admin_bp.route("/api/bulk/scan", methods=["POST"])
def api_bulk_scan():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    if not shared.get('HAS_BULK_HELPERS'):
        return jsonify({"error": "bulk helpers not loaded"}), 500

    candidates = shared['_scan_bulk_candidates']()

    with shared['_bulk_lock']:
        shared['_bulk_state']["items"] = candidates
        shared['_bulk_state']["total"] = len(candidates)
        shared['_bulk_state']["done"] = 0
        shared['_bulk_state']["current"] = ""
        if not shared['_bulk_state'].get("running"):
            shared['_append_bulk_log'](f"Scanned {len(candidates)} manga folders.")

    return jsonify({
        "ok": True,
        "count": len(candidates),
        "items": candidates
    })

@admin_bp.route("/api/bulk/start", methods=["POST"])
def api_bulk_start():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "admin only"}), 403

    with shared['_bulk_lock']:
        if shared['_bulk_state'].get("running"):
            return jsonify({"error": "already running"}), 400
        if not shared['_bulk_state'].get("items"):
            return jsonify({"error": "nothing to import — run Scan first"}), 400

        import threading
        worker = shared.get('_bulk_import_worker') or shared.get('_bulk_import_worker_ref')
        t = threading.Thread(target=worker or (lambda: None), daemon=True)
        t.start()

    return jsonify({"ok": True, "message": "Import started"})

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

print("admin blueprint loaded")