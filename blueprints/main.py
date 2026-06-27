from flask import Blueprint, render_template, request, redirect, url_for, abort, jsonify, current_app, send_file
import json

main_bp = Blueprint('main', __name__)

# Helper to get common functions without circular import
def _get_shared():
    from app import (
        get_all_mangas, get_current_user, get_db, get_recommendations,
        get_cover_url, compute_rating, get_manga_by_slug, get_page_urls,
        get_all_users, get_user_tag_weights, natural_sort_key
    )
    return {
        'get_all_mangas': get_all_mangas,
        'get_current_user': get_current_user,
        'get_db': get_db,
        'get_recommendations': get_recommendations,
        'get_cover_url': get_cover_url,
        'compute_rating': compute_rating,
        'get_manga_by_slug': get_manga_by_slug,
        'get_page_urls': get_page_urls,
        'get_all_users': get_all_users,
        'get_user_tag_weights': get_user_tag_weights,
        'natural_sort_key': natural_sort_key,
    }

@main_bp.route("/")
def index():
    shared = _get_shared()
    get_all_mangas = shared['get_all_mangas']
    get_current_user = shared['get_current_user']
    get_db = shared['get_db']
    get_recommendations = shared['get_recommendations']
    get_cover_url = shared['get_cover_url']
    compute_rating = shared['compute_rating']

    search = request.args.get("q", "").strip()
    tag = request.args.get("tag", "").strip()
    sort = request.args.get("sort", "date")
    unread_only = request.args.get("unread") == "1"
    min_rating = float(request.args.get("min_rating", 0))

    mangas = get_all_mangas(search=search or None, tag=tag or None)

    if search:
        s = search.lower()
        filtered = []
        for m in mangas:
            tags = json.loads(m["tags"] or "[]")
            author = (m["author"] or "").lower() if "author" in (dict(m).keys() if hasattr(m, "keys") else []) else (getattr(m, "author", "") or "").lower()
            if s in m["title"].lower() or s in author or any(s in t.lower() for t in tags):
                filtered.append(m)
        mangas = filtered

    user = get_current_user()
    completed_map = {}
    if user:
        conn = get_db()
        hrows = conn.execute("SELECT manga_id, completed FROM user_history WHERE user_id=?", (user['id'],)).fetchall()
        completed_map = {r['manga_id']: r['completed'] for r in hrows}
        conn.close()

    results = []
    all_tags = set()
    for m in mangas:
        tags = json.loads(m["tags"]) if m["tags"] else []
        for t in tags:
            all_tags.add(t)
        avg, cnt = compute_rating(m)
        cover = get_cover_url(m, thumb=True)
        is_read = completed_map.get(m['id'], 0) == 1
        results.append({
            "id": m["id"],
            "slug": m["slug"],
            "title": m["title"],
            "author": m["author"] or "",
            "cover": cover,
            "tags": tags,
            "rating": avg,
            "rating_count": cnt,
            "pages_count": len(json.loads(m["pages"])) if m["pages"] else 0,
            "is_read": is_read,
        })

    if unread_only and user:
        results = [r for r in results if not r['is_read']]
    if min_rating > 0:
        results = [r for r in results if r['rating'] >= min_rating]

    if sort == "rating":
        results.sort(key=lambda x: -x['rating'])
    elif sort == "pages":
        results.sort(key=lambda x: -x['pages_count'])
    elif sort == "title":
        results.sort(key=lambda x: x['title'].lower())

    recommendations = []
    if user and user.get('is_premium'):
        recommendations = get_recommendations(user['id'], limit=6)

    return render_template(
        "index.html",
        mangas=results,
        all_tags=sorted(all_tags),
        current_search=search,
        current_tag=tag,
        current_user=user,
        current_sort=sort,
        unread_only=unread_only,
        min_rating=min_rating,
        recommendations=recommendations,
    )

@main_bp.route("/random")
def random_manga():
    shared = _get_shared()
    get_current_user = shared['get_current_user']
    get_db = shared['get_db']
    try:
        user = get_current_user()
        conn = get_db()
        query = "SELECT slug FROM mangas"
        params = []
        if user and request.args.get("unread"):
            read_ids = [r['manga_id'] for r in conn.execute(
                "SELECT manga_id FROM user_history WHERE user_id=? AND completed=1", (user['id'],)).fetchall()]
            if read_ids:
                placeholders = ','.join(['?'] * len(read_ids))
                query += f" WHERE id NOT IN ({placeholders})"
                params = read_ids
        mangas = conn.execute(query, params).fetchall()
        conn.close()
        if mangas:
            return redirect(url_for('main.manga_detail', slug=mangas[0]['slug']))
        return redirect(url_for('main.index'))
    except Exception as e:
        from app import logger
        logger.warning("Error in /random: %s", e)
        return redirect(url_for('main.index'))

@main_bp.route("/tags")
def tags_page():
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    get_cover_url = shared['get_cover_url']
    conn = get_db()
    rows = conn.execute("SELECT tags FROM mangas").fetchall()
    conn.close()
    tag_count = {}
    for r in rows:
        for t in (json.loads(r['tags']) if r['tags'] else []):
            tag_count[t] = tag_count.get(t, 0) + 1
    sorted_tags = sorted(tag_count.items(), key=lambda x: -x[1])
    return render_template("tags.html", tags=sorted_tags, current_user=get_current_user())

@main_bp.route("/manga/<slug>")
def manga_detail(slug):
    shared = _get_shared()
    get_manga_by_slug = shared['get_manga_by_slug']
    get_current_user = shared['get_current_user']
    get_db = shared['get_db']
    get_cover_url = shared['get_cover_url']
    get_page_urls = shared['get_page_urls']
    compute_rating = shared['compute_rating']

    row = get_manga_by_slug(slug)
    if not row:
        abort(404)

    tags = json.loads(row["tags"]) if row["tags"] else []
    pages = json.loads(row["pages"]) if row["pages"] else []
    avg, cnt = compute_rating(row)

    cover = get_cover_url(row)
    page_images = get_page_urls(row["slug"], pages, thumb=True)

    conn = get_db()
    user = get_current_user()
    my_rating = None
    is_favorite = False
    is_read = False
    if user:
        r = conn.execute("SELECT score FROM user_ratings WHERE user_id=? AND manga_id=?", (user['id'], row['id'])).fetchone()
        if r:
            my_rating = r['score']
        is_favorite = bool(conn.execute("SELECT 1 FROM user_favorites WHERE user_id=? AND manga_id=?", (user['id'], row['id'])).fetchone())
        h = conn.execute("SELECT completed FROM user_history WHERE user_id=? AND manga_id=?", (user['id'], row['id'])).fetchone()
        is_read = bool(h and h['completed'])

    comments = conn.execute("""
        SELECT c.id, c.user_id, c.content, c.created_at, u.username, u.username_color, u.avatar, u.is_admin
        FROM comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.manga_id = ?
        ORDER BY c.created_at ASC
    """, (row['id'],)).fetchall()
    conn.close()

    manga = {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "author": row["author"] or "",
        "description": row["description"],
        "cover": cover,
        "tags": tags,
        "rating": avg,
        "rating_count": cnt,
        "pages_count": len(pages),
        "pages": page_images,
        "my_rating": my_rating,
        "is_favorite": is_favorite,
        "is_read": is_read,
    }
    return render_template("detail.html", manga=manga, current_user=user, comments=comments)

@main_bp.route("/read/<slug>")
def reader(slug):
    shared = _get_shared()
    get_manga_by_slug = shared['get_manga_by_slug']
    get_current_user = shared['get_current_user']
    get_page_urls = shared['get_page_urls']

    row = get_manga_by_slug(slug)
    if not row:
        abort(404)

    pages = json.loads(row["pages"] or "[]")
    page_images = get_page_urls(row["slug"], pages)
    page_thumbs = get_page_urls(row["slug"], pages, thumb=True)

    manga = {
        "id": row["id"],
        "slug": row["slug"],
        "title": row["title"],
        "pages_count": len(pages),
        "pages": page_images,
        "pages_thumb": page_thumbs,
    }
    user = get_current_user()
    return render_template("reader.html", manga=manga, current_user=user)

@main_bp.route("/login", methods=["GET", "POST"])
def login():
    shared = _get_shared()
    get_db = shared['get_db']
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username = ? AND password = ?", (username, password)).fetchone()
        conn.close()
        if user:
            from app import login_user
            login_user(user["id"])
            return redirect(url_for("main.index"))
        else:
            return render_template("login.html", error="Неверный логин или пароль", users=shared['get_all_users']())
    return render_template("login.html", users=shared['get_all_users']())

@main_bp.route("/login_as/<int:user_id>")
def login_as(user_id):
    shared = _get_shared()
    get_db = shared['get_db']
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if user:
        from app import login_user
        login_user(user["id"])
    return redirect(url_for("main.index"))

@main_bp.route("/register", methods=["POST"])
def register():
    shared = _get_shared()
    get_db = shared['get_db']
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    if not username or not password:
        return redirect(url_for("main.login"))
    conn = get_db()
    try:
        conn.execute("INSERT INTO users (username, password) VALUES (?, ?)", (username, password))
        conn.commit()
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        from app import login_user
        login_user(user["id"])
    except Exception as e:
        from app import logger
        logger.warning("Register failed: %s", e)
    conn.close()
    return redirect(url_for("main.index"))

@main_bp.route("/logout")
def logout():
    from app import logout_user
    logout_user()
    return redirect(url_for("main.index"))

@main_bp.route("/profile")
def profile():
    shared = _get_shared()
    get_current_user = shared['get_current_user']
    get_db = shared['get_db']
    get_cover_url = shared['get_cover_url']
    get_user_tag_weights = shared.get('get_user_tag_weights', lambda x: {})
    get_recommendations = shared.get('get_recommendations', lambda x, limit=6: [])

    user = get_current_user()
    if not user:
        return redirect(url_for("main.login"))

    conn = get_db()
    fav_rows = conn.execute("""
        SELECT m.* FROM mangas m
        JOIN user_favorites f ON m.id = f.manga_id
        WHERE f.user_id = ? ORDER BY f.added_at DESC
    """, (user['id'],)).fetchall()
    favorites = []
    for r in fav_rows:
        cover = get_cover_url(r, thumb=True)
        favorites.append({**dict(r), "cover": cover})

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

    rating_rows = conn.execute("""
        SELECT m.title, m.slug, ur.score FROM user_ratings ur
        JOIN mangas m ON m.id = ur.manga_id
        WHERE ur.user_id = ? ORDER BY ur.rated_at DESC
    """, (user['id'],)).fetchall()

    conn.close()

    tag_weights = get_user_tag_weights(user['id'])
    sorted_weights = sorted(tag_weights.items(), key=lambda x: -x[1])[:15]

    recommendations = []
    if user.get('is_premium'):
        recommendations = get_recommendations(user['id'], limit=6)

    return render_template("profile.html", user=user, favorites=favorites, history=history, my_ratings=rating_rows, recommendations=recommendations, tag_weights=sorted_weights)

@main_bp.route("/download/<slug>")
def download_manga(slug):
    from app import get_current_user, get_manga_by_slug, get_manga_dir
    user = get_current_user()
    if not user or not user.get('is_premium'):
        return "Доступно только премиум пользователям", 403

    row = get_manga_by_slug(slug)
    if not row:
        abort(404)

    pages = json.loads(row["pages"] or "[]")
    manga_dir = get_manga_dir(slug)

    import os
    import tempfile
    import zipfile

    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".zip")
    tmp.close()
    zip_path = tmp.name

    try:
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zf:
            cover = row["cover"] or ""
            if cover and not cover.startswith(("http://", "https://")):
                cover_path = os.path.join(manga_dir, cover)
                if os.path.exists(cover_path):
                    zf.write(cover_path, arcname=f"cover{os.path.splitext(cover)[1]}")
            for p in pages:
                if not p.startswith(("http://", "https://")):
                    p_path = os.path.join(manga_dir, p)
                    if os.path.exists(p_path):
                        zf.write(p_path, arcname=p)

        safe_title = "".join(c for c in row["title"] if c.isalnum() or c in " -_").rstrip()
        return send_file(
            zip_path,
            as_attachment=True,
            download_name=f"{safe_title or slug}.zip",
            mimetype="application/zip"
        )
    except Exception as e:
        from app import logger
        logger.exception("Download failed")
        return "Ошибка при подготовке скачивания", 500
    finally:
        try:
            if os.path.exists(zip_path):
                os.unlink(zip_path)
        except:
            pass