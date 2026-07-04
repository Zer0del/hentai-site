from flask import Blueprint, request, jsonify, abort
import json
import os

api_bp = Blueprint('api', __name__)

def _get_shared():
    # Late import to avoid circular import issues during app startup
    import uuid
    from app import (
        get_current_user, get_db, get_all_mangas, get_manga_by_slug,
        get_cover_url, get_page_urls, compute_rating, get_recommendations,
        get_user_tag_weights, update_user_history, save_uploaded_file,
        _finalize_cover, extract_zip_and_normalize, get_manga_dir, get_next_page_index,
        slugify, _create_thumbnail,
        natural_sort_key,
        logger, _bulk_lock, _bulk_state, _append_bulk_log, _force_wal_checkpoint,
        BULK_PROGRESS_FILE, ADMIN_PASS, get_bulk_root
    )
    d = locals()
    d['ADMIN_PASS'] = ADMIN_PASS
    d['ALLOWED_ZIP'] = {'zip', 'cbz'}
    d['uuid'] = uuid
    d['os'] = __import__('os')
    d['json'] = __import__('json')
    d['shutil'] = __import__('shutil')
    return d

@api_bp.route("/api/mangas")
def api_mangas():
    shared = _get_shared()
    mangas = shared['get_all_mangas']()
    result = []
    for m in mangas:
        tags = json.loads(m["tags"] or "[]")
        avg, cnt = shared['compute_rating'](m)
        cover = shared['get_cover_url'](m, thumb=True)
        result.append({
            "id": m["id"],
            "slug": m["slug"],
            "title": m["title"],
            "author": m["author"] or "",
            "cover": cover,
            "tags": tags,
            "rating": avg,
            "rating_count": cnt,
            "pages_count": len(json.loads(m["pages"] or "[]")),
        })
    return jsonify(result)

@api_bp.route("/api/manga/<slug>")
def api_manga(slug):
    shared = _get_shared()
    row = shared['get_manga_by_slug'](slug)
    if not row:
        return jsonify({"error": "not found"}), 404
    pages = json.loads(row["pages"] or "[]")
    cover = shared['get_cover_url'](row)
    cover_thumb = shared['get_cover_url'](row, thumb=True)
    resolved_pages = shared['get_page_urls'](slug, pages)
    resolved_pages_thumb = shared['get_page_urls'](slug, pages, thumb=True)
    return jsonify({
        "slug": row["slug"],
        "title": row["title"],
        "author": row["author"] or "",
        "cover": cover,
        "cover_thumb": cover_thumb,
        "raw_cover": row["cover"],
        "pages": resolved_pages,
        "pages_thumb": resolved_pages_thumb,
        "raw_pages": pages,
        "tags": json.loads(row["tags"] or "[]"),
        "description": row["description"],
        "rating": shared['compute_rating'](row)[0],
    })

@api_bp.route("/api/rate", methods=["POST"])
def api_rate():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "Войдите в аккаунт, чтобы ставить оценки"}), 403

    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("id"))
        score = int(data.get("score", 0))
    except (TypeError, ValueError):
        return jsonify({"error": "bad input"}), 400

    if score != 0 and not (1 <= score <= 10):
        return jsonify({"error": "score 1-10 or 0 to remove"}), 400

    conn = shared['get_db']()
    existing = conn.execute("SELECT score FROM user_ratings WHERE user_id=? AND manga_id=?", (user['id'], manga_id)).fetchone()
    old_score = existing['score'] if existing else 0
    my_score_to_return = score

    if score == 0:
        if old_score > 0:
            conn.execute("DELETE FROM user_ratings WHERE user_id=? AND manga_id=?", (user['id'], manga_id))
        my_score_to_return = 0
    else:
        updated = conn.execute("""
            UPDATE user_ratings SET score = ?, rated_at = CURRENT_TIMESTAMP
            WHERE user_id = ? AND manga_id = ?
        """, (score, user['id'], manga_id)).rowcount
        if updated == 0:
            conn.execute("INSERT INTO user_ratings (user_id, manga_id, score) VALUES (?, ?, ?)", (user['id'], manga_id, score))
        my_score_to_return = score

    row = conn.execute("SELECT rating_sum, rating_count FROM mangas WHERE id = ?", (manga_id,)).fetchone()
    if row:
        rsum = row["rating_sum"] or 0
        rcnt = row["rating_count"] or 0
        if old_score > 0 and score == 0:
            conn.execute("UPDATE mangas SET rating_sum = ?, rating_count = ? WHERE id = ?", (rsum - old_score, rcnt - 1, manga_id))
        elif old_score == 0 and score > 0:
            conn.execute("UPDATE mangas SET rating_sum = ?, rating_count = ? WHERE id = ?", (rsum + score, rcnt + 1, manga_id))
        elif old_score > 0 and score > 0:
            conn.execute("UPDATE mangas SET rating_sum = ? WHERE id = ?", (rsum - old_score + score, manga_id))
    conn.commit()
    conn.close()

    # Invalidate weights cache so profile and recs reflect new rating immediately
    try:
        from app import invalidate_user_tag_cache
        invalidate_user_tag_cache(user['id'])
    except Exception:
        pass

    # Re-query fresh avg/count for immediate UI update in JS
    conn = shared['get_db']()
    row2 = conn.execute("SELECT rating_sum, rating_count FROM mangas WHERE id = ?", (manga_id,)).fetchone()
    conn.close()
    new_rating = 0.0
    new_count = 0
    if row2 and (row2["rating_count"] or 0) > 0:
        new_rating = round(row2["rating_sum"] / row2["rating_count"], 1)
        new_count = row2["rating_count"]
    return jsonify({"ok": True, "my_score": my_score_to_return, "rating": new_rating, "count": new_count})

@api_bp.route("/api/favorite", methods=["POST"])
def api_favorite():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "Войдите в систему"}), 403
    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("manga_id"))
    except Exception:
        return jsonify({"error": "bad id"}), 400
    conn = shared['get_db']()
    existing = conn.execute("SELECT 1 FROM user_favorites WHERE user_id=? AND manga_id=?", (user['id'], manga_id)).fetchone()
    if existing:
        conn.execute("DELETE FROM user_favorites WHERE user_id=? AND manga_id=?", (user['id'], manga_id))
        action = "removed"
    else:
        conn.execute("INSERT INTO user_favorites (user_id, manga_id) VALUES (?, ?)", (user['id'], manga_id))
        action = "added"
    conn.commit()
    conn.close()

    # Invalidate weights cache (fav affects weights)
    try:
        from app import invalidate_user_tag_cache
        invalidate_user_tag_cache(user['id'])
    except Exception:
        pass

    return jsonify({"ok": True, "action": action})

@api_bp.route("/api/update_profile", methods=["POST"])
def api_update_profile():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "Войдите в систему"}), 403
    username_color = request.form.get("username_color") or request.form.get("color", "#e11d48")
    avatar_file = request.files.get("avatar")
    conn = shared['get_db']()
    if avatar_file and avatar_file.filename:
        avatar_name = shared['save_uploaded_file'](avatar_file, os.path.join(os.path.dirname(__file__), '..', 'uploads', 'avatars'), "user" + str(user['id']))
        if avatar_name:
            conn.execute("UPDATE users SET avatar = ? WHERE id = ?", (avatar_name, user['id']))
    conn.execute("UPDATE users SET username_color = ? WHERE id = ?", (username_color, user['id']))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@api_bp.route("/api/update_showcase", methods=["POST"])
def api_update_showcase():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "login"}), 403
    public = int(request.form.get("showcase_public", 1))
    conn = shared['get_db']()
    conn.execute("UPDATE users SET showcase_public = ? WHERE id = ?", (public, user['id']))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@api_bp.route("/api/mark_read", methods=["POST"])
def api_mark_read():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "login required"}), 403
    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("manga_id"))
        last_page = int(data.get("last_page", 1))
        completed = bool(data.get("completed", False))
    except Exception:
        return jsonify({"error": "bad input"}), 400
    shared['update_user_history'](user['id'], manga_id, last_page, completed)
    return jsonify({"ok": True})

@api_bp.route("/api/progress", methods=["POST"])
def api_progress():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "login required"}), 403
    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("manga_id"))
        last_page = int(data.get("last_page", 1))
        total = int(data.get("total", 0))
        completed = last_page >= total
    except Exception:
        return jsonify({"error": "bad input"}), 400
    shared['update_user_history'](user['id'], manga_id, last_page, completed)
    return jsonify({"ok": True})

@api_bp.route("/api/grant_premium", methods=["POST"])
def api_grant_premium():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "login"}), 403
    conn = shared['get_db']()
    conn.execute("UPDATE users SET is_premium=1 WHERE id=?", (user['id'],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@api_bp.route("/api/grant_admin", methods=["POST"])
def api_grant_admin():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "login"}), 403
    conn = shared['get_db']()
    conn.execute("UPDATE users SET is_admin=1 WHERE id=?", (user['id'],))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@api_bp.route("/api/comment", methods=["POST"])
def api_comment():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user:
        return jsonify({"error": "login"}), 403
    data = request.get_json(silent=True) or request.form
    try:
        manga_id = int(data.get("manga_id"))
        content = (data.get("content") or "").strip()
    except Exception:
        return jsonify({"error": "bad input"}), 400
    if not content or len(content) > 2000:
        return jsonify({"error": "Комментарий пустой или слишком длинный"}), 400
    conn = shared['get_db']()
    conn.execute("INSERT INTO comments (user_id, manga_id, content) VALUES (?, ?, ?)", (user['id'], manga_id, content))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@api_bp.route("/api/delete_comment", methods=["POST"])
def api_delete_comment():
    shared = _get_shared()
    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Нет прав"}), 403
    data = request.get_json(silent=True) or request.form
    try:
        cid = int(data.get("id"))
    except Exception:
        return jsonify({"error": "bad id"}), 400
    conn = shared['get_db']()
    conn.execute("DELETE FROM comments WHERE id=?", (cid,))
    conn.commit()
    conn.close()
    return jsonify({"ok": True})

@api_bp.route("/api/add_manga", methods=["POST"])
def api_add_manga():
    """Add manga via admin form (ZIP or multiple images).
    IMPORTANT for production:
    - set `client_max_body_size 2G;` (or higher) in nginx
    - set long timeouts: proxy_read_timeout 600s; etc.
    - For 200-300+ page archives, processing (thumbnails) can take time; use gunicorn --timeout 600
    """
    shared = _get_shared()
    conn = None
    try:
        password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
        if password != shared['ADMIN_PASS']:
            return jsonify({"error": "Неверный пароль администратора"}), 403

        # Password match is enough for admin add (bulk and direct uploads).
        # No longer strictly require is_admin session (simplifies internal bulk).

        title = (request.form.get("title") or "").strip()
        author = (request.form.get("author") or "").strip()
        description = (request.form.get("description") or "").strip()
        tags_raw = (request.form.get("tags") or "").strip()

        if not title:
            return jsonify({"error": "Название обязательно"}), 400

        tags = [t.strip() for t in tags_raw.split(",") if t.strip()]
        slug = shared['slugify'](title)

        conn = shared['get_db']()
        existing = conn.execute("SELECT id FROM mangas WHERE slug = ?", (slug,)).fetchone()
        if existing:
            slug = f"{slug}-{shared['uuid'].uuid4().hex[:6]}"

        manga_dir = shared['get_manga_dir'](slug)

        cover_file = request.files.get("cover")
        if not cover_file or not cover_file.filename:
            conn.close()
            return jsonify({"error": "Нужна обложка (cover)"}), 400

        cover_name = shared['save_uploaded_file'](cover_file, manga_dir, "cover")
        if not cover_name:
            conn.close()
            return jsonify({"error": "Неверный формат обложки"}), 400

        cover_name = shared['_finalize_cover'](manga_dir, cover_name)

        page_files = request.files.getlist("pages")
        pages = []

        zip_file = request.files.get("zipfile")
        if zip_file and zip_file.filename:
            ext = zip_file.filename.rsplit(".", 1)[-1].lower()
            if ext in shared.get('ALLOWED_ZIP', {"zip", "cbz"}):
                try:
                    pages = shared['extract_zip_and_normalize'](zip_file, manga_dir, "p")
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
                saved = shared['save_uploaded_file'](f, manga_dir, f"{idx:03d}")
                if saved:
                    pages.append(saved)
                    full_path = os.path.join(manga_dir, saved)
                    base = os.path.splitext(saved)[0]
                    thumb_name = f"{base}-thumb.webp"
                    shared['_create_thumbnail'](full_path, os.path.join(manga_dir, thumb_name), max_height=240, resample=None)
                    # Full page WebP re-encode removed for speed on large uploads (280+ pages)
                    idx += 1
            pages.sort(key=shared['natural_sort_key'])
        else:
            conn.close()
            return jsonify({"error": "Нужно загрузить страницы (множественный выбор или zip)"}), 400

        if not pages:
            conn.close()
            return jsonify({"error": "Не удалось загрузить ни одной страницы"}), 400

        try:
            conn.execute("""
                INSERT INTO mangas (slug, title, author, description, cover, pages, tags, rating_sum, rating_count)
                VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """, (slug, title, author, description, cover_name, json.dumps(pages), json.dumps(tags)))
            conn.commit()
        except Exception as e:
            conn.close()
            return jsonify({"error": f"Ошибка сохранения: {str(e)}"}), 500
        conn.close()

        return jsonify({"ok": True, "slug": slug, "title": title, "pages": len(pages)})
    except Exception as outer_e:
        try:
            if conn:
                conn.close()
        except:
            pass
        # Log full error for debugging (check gunicorn / server logs)
        import traceback
        print("api/add_manga ERROR:", traceback.format_exc())
        return jsonify({"error": f"Внутренняя ошибка сервера: {str(outer_e)}"}), 500

# Note: Other admin APIs like edit_manga, delete_manga, bulk APIs etc. can be moved here.
# For bulk specific, they are also in admin blueprint.

@api_bp.route("/api/delete_manga", methods=["POST"])
def api_delete_manga():
    shared = _get_shared()
    password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
    if password != shared['ADMIN_PASS']:
        return jsonify({"error": "Неверный пароль администратора"}), 403

    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Требуются права администратора (активируйте админку в профиле)"}), 403

    slug = (request.form.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "slug обязателен"}), 400

    # Remove files
    try:
        import shutil
        shutil.rmtree(shared['get_manga_dir'](slug), ignore_errors=True)
    except Exception:
        pass

    conn = shared['get_db']()
    conn.execute("DELETE FROM mangas WHERE slug = ?", (slug,))
    conn.commit()
    conn.close()
    # _cache.clear() if available
    try:
        from helpers import _cache
        _cache.clear()
    except:
        pass

    return jsonify({"ok": True, "deleted": slug})


@api_bp.route("/api/delete_all_manga", methods=["POST"])
def api_delete_all_manga():
    shared = _get_shared()
    password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
    if password != shared['ADMIN_PASS']:
        return jsonify({"error": "Неверный пароль администратора"}), 403

    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Требуются права администратора (активируйте админку в профиле)"}), 403

    # Remove all manga files
    try:
        import shutil
        manga_root = os.path.join(os.path.dirname(__file__), '..', 'uploads', 'manga')
        if os.path.exists(manga_root):
            for item in os.listdir(manga_root):
                p = os.path.join(manga_root, item)
                if os.path.isdir(p):
                    shutil.rmtree(p, ignore_errors=True)
    except Exception as e:
        # continue anyway
        pass

    conn = shared['get_db']()
    try:
        conn.execute("DELETE FROM mangas")
        conn.execute("DELETE FROM user_ratings")
        conn.execute("DELETE FROM user_favorites")
        conn.execute("DELETE FROM user_history")
        conn.execute("DELETE FROM comments")
        conn.commit()
    except Exception as e:
        conn.close()
        return jsonify({"error": f"DB error: {e}"}), 500
    conn.close()

    # clear cache and bulk progress
    try:
        from helpers import _cache
        _cache.clear()
    except:
        pass
    try:
        shared['BULK_PROGRESS_FILE'].unlink(missing_ok=True)
    except:
        pass

    return jsonify({"ok": True, "deleted": "all"})


@api_bp.route("/api/edit_manga", methods=["POST"])
def api_edit_manga():
    shared = _get_shared()
    password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
    if password != shared['ADMIN_PASS']:
        return jsonify({"error": "Неверный пароль администратора"}), 403

    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Требуются права администратора (активируйте админку в профиле)"}), 403

    slug = (request.form.get("slug") or "").strip()
    if not slug:
        return jsonify({"error": "Не указан slug"}), 400

    row = shared['get_manga_by_slug'](slug)
    if not row:
        return jsonify({"error": "Манга не найдена"}), 404

    conn = shared['get_db']()

    # New metadata
    new_title = (request.form.get("title") or row["title"]).strip() or row["title"]
    new_author = (request.form.get("author") or row["author"] or "").strip()
    new_description = request.form.get("description", row["description"] or "")
    new_tags_raw = request.form.get("tags", None)
    if new_tags_raw is not None:
        tags = [t.strip() for t in new_tags_raw.split(",") if t.strip()]
    else:
        tags = json.loads(row["tags"] or "[]")

    manga_dir = shared['get_manga_dir'](slug)

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

        saved_cover = shared['save_uploaded_file'](cover_file, manga_dir, "cover")
        if saved_cover:
            cover_name = shared['_finalize_cover'](manga_dir, saved_cover)

    # Append new pages (files or zip)
    current_pages = json.loads(row["pages"] or "[]")
    added_count = 0

    zip_file = request.files.get("zipfile")
    page_files = request.files.getlist("pages")

    if zip_file and zip_file.filename:
        ext = zip_file.filename.rsplit(".", 1)[-1].lower()
        if ext in shared.get('ALLOWED_ZIP', {"zip", "cbz"}):
            try:
                start_idx = shared['get_next_page_index'](slug)
                temp_zip = os.path.join(manga_dir, "_temp_edit.zip")
                zip_file.save(temp_zip)
                with zipfile.ZipFile(temp_zip, "r") as z:
                    entries = []
                    for name in z.namelist():
                        if name.endswith("/"):
                            continue
                        eext = name.rsplit(".", 1)[-1].lower()
                        if eext in shared.get('ALLOWED_PAGE', {"png", "jpg", "jpeg", "webp", "gif"}):
                            entries.append(name)
                    # natural sort (in case archive has 1.jpg,10.jpg,2.jpg without padding)
                    entries.sort(key=lambda n: shared['natural_sort_key'](__import__('pathlib').Path(n).name))
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
                        shared['_create_thumbnail'](out_path, os.path.join(manga_dir, thumb_name), max_height=240)

                        # re-encode full webp (skip if already)
                        if not out_name.lower().endswith('.webp'):
                            webp_name = f"{base}.webp"
                            webp_path = os.path.join(manga_dir, webp_name)
                            if shared['_create_thumbnail'](out_path, webp_path, quality=95):
                                current_pages[-1] = webp_name
                                if out_name != webp_name and os.path.exists(out_path):
                                    try:
                                        os.remove(out_path)
                                    except:
                                        pass
                # remove temp
                try:
                    os.remove(temp_zip)
                except:
                    pass
            except Exception as e:
                conn.close()
                return jsonify({"error": f"Ошибка добавления страниц: {str(e)}"}), 400
    elif page_files:
        idx = shared['get_next_page_index'](slug)
        for f in page_files:
            if not f.filename:
                continue
            saved = shared['save_uploaded_file'](f, manga_dir, f"{idx:03d}")
            if saved:
                current_pages.append(saved)
                full_path = os.path.join(manga_dir, saved)
                base = os.path.splitext(saved)[0]
                thumb_name = f"{base}-thumb.webp"
                shared['_create_thumbnail'](full_path, os.path.join(manga_dir, thumb_name), max_height=240)
                if not saved.lower().endswith('.webp'):
                    webp_name = f"{base}.webp"
                    webp_path = os.path.join(manga_dir, webp_name)
                    if shared['_create_thumbnail'](full_path, webp_path, quality=95):
                        current_pages[-1] = webp_name
                        if saved != webp_name and os.path.exists(full_path):
                            try:
                                os.remove(full_path)
                            except:
                                pass
                idx += 1
    # update pages
    conn.execute("UPDATE mangas SET title = ?, author = ?, description = ?, tags = ?, cover = ?, pages = ? WHERE slug = ?", (new_title, new_author, new_description, json.dumps(tags), cover_name, json.dumps(current_pages), slug))
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "added_pages": added_count, "total_pages": len(current_pages)})

@api_bp.route("/api/delete_page", methods=["POST"])
def api_delete_page():
    shared = _get_shared()
    password = request.form.get("password", "") or request.headers.get("X-Admin-Pass", "")
    if password != shared['ADMIN_PASS']:
        return jsonify({"error": "Неверный пароль администратора"}), 403

    user = shared['get_current_user']()
    if not user or not user.get('is_admin'):
        return jsonify({"error": "Требуются права администратора (активируйте админку в профиле)"}), 403

    slug = (request.form.get("slug") or "").strip()
    page = (request.form.get("page") or "").strip()   # e.g. "003.jpg"

    if not slug or not page:
        return jsonify({"error": "slug и page обязательны"}), 400

    row = shared['get_manga_by_slug'](slug)
    if not row:
        return jsonify({"error": "Манга не найдена"}), 404

    pages = json.loads(row["pages"] or "[]")
    if page not in pages:
        return jsonify({"error": "Страница не найдена"}), 404

    # Delete file + its thumb
    manga_dir = shared['get_manga_dir'](slug)
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

    conn = shared['get_db']()
    conn.execute("UPDATE mangas SET pages = ? WHERE slug = ?", (json.dumps(pages), slug))
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "remaining": len(pages)})

@api_bp.route("/api/search")
def api_search():
    shared = _get_shared()
    get_all_mangas = shared['get_all_mangas']
    get_cover_url = shared['get_cover_url']
    compute_rating = shared['compute_rating']

    q = (request.args.get("q") or "").strip().lower()
    limit = min(int(request.args.get("limit", 8)), 12)

    if not q:
        return jsonify([])

    # Use limited fetch for perf (live search doesn't need all)
    mangas = get_all_mangas(limit=200)
    results = []
    for m in mangas:
        title = (m["title"] or "").lower()
        author = (m["author"] or "").lower() if "author" in m.keys() else ""
        tags = json.loads(m["tags"] or "[]")
        tags_lower = [t.lower() for t in tags]

        if q in title or q in author or any(q in t for t in tags_lower):
            avg, cnt = compute_rating(m)
            cover = get_cover_url(m, thumb=True)
            results.append({
                "id": m["id"],
                "slug": m["slug"],
                "title": m["title"],
                "author": m["author"] or "",
                "cover": cover,
                "rating": round(avg, 1),
                "rating_count": cnt,
                "pages_count": len(json.loads(m["pages"] or "[]")),
                "tags": tags[:4],
            })
            if len(results) >= limit:
                break

    # simple relevance: exact title start first
    results.sort(key=lambda x: (0 if x["title"].lower().startswith(q) else 1, -x["rating"]))
    return jsonify(results[:limit])

print("api blueprint loaded with core endpoints")