from flask import Blueprint, render_template, request, redirect, url_for, abort, jsonify, current_app, send_file
import json

main_bp = Blueprint('main', __name__)

# Helper to get common functions without circular import
def _get_shared():
    from app import (
        get_all_mangas, get_current_user, get_db, get_recommendations,
        get_cover_url, compute_rating, get_manga_by_slug, get_page_urls,
        get_all_users, get_user_tag_weights, natural_sort_key,
        get_all_tags
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
        'get_all_tags': get_all_tags,
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
    min_rating = float(request.args.get("min_rating") or 0)
    view_all = request.args.get("view") == "all"

    user = get_current_user()
    completed_map = {}
    if user:
        conn = get_db()
        hrows = conn.execute("SELECT manga_id, completed FROM user_history WHERE user_id=?", (user['id'],)).fetchall()
        completed_map = {r['manga_id']: r['completed'] for r in hrows}
        conn.close()

    # Latest added with pagination (larger block + pages) - efficient
    latest_page = request.args.get('page', 1, type=int) or 1
    if latest_page < 1:
        latest_page = 1
    latest_per_page = 24  # much larger to extend further down
    start_idx = (latest_page - 1) * latest_per_page
    latest_raw = get_all_mangas(limit=latest_per_page, offset=start_idx)

    # Efficient total count (no full load)
    db_conn = get_db()
    total_latest = db_conn.execute("SELECT COUNT(*) as c FROM mangas").fetchone()["c"]
    db_conn.close()

    latest = []
    all_tags_set = set()
    for m in latest_raw:
        tags = json.loads(m["tags"]) if m["tags"] else []
        for t in tags:
            all_tags_set.add(t)
        avg, cnt = compute_rating(m)
        cover = get_cover_url(m, thumb=True)
        is_read = completed_map.get(m['id'], 0) == 1
        latest.append({
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
    total_latest_pages = (total_latest + latest_per_page - 1) // latest_per_page if total_latest else 1

    recommendations = []
    if user and user.get('is_premium'):
        recommendations = get_recommendations(user['id'], limit=6)

    show_full_library = bool(search or tag or view_all or unread_only or (min_rating > 0) or (sort != "date"))

    if show_full_library:
        # Unified with /search page (better multi-tag, live, nice UI)
        qs = request.query_string.decode()
        return redirect('/search' + ('?' + qs if qs else ''))
        if search:
            s = search.lower()
            filtered = []
            for m in mangas:
                tags = json.loads(m["tags"] or "[]")
                author = (m["author"] or "").lower() if "author" in (dict(m).keys() if hasattr(m, "keys") else []) else (getattr(m, "author", "") or "").lower()
                if s in m["title"].lower() or s in author or any(s in t.lower() for t in tags):
                    filtered.append(m)
            mangas = filtered

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
        grid_mangas = results
        all_tags_out = sorted(all_tags)
    else:
        grid_mangas = latest
        all_tags_out = []

    return render_template(
        "index.html",
        mangas=grid_mangas,
        all_tags=all_tags_out,
        current_search=search,
        current_tag=tag,
        current_user=user,
        current_sort=sort,
        unread_only=unread_only,
        min_rating=min_rating,
        recommendations=recommendations,
        show_full_library=show_full_library,
        latest_mangas=latest,
        latest_page=latest_page,
        total_latest_pages=total_latest_pages,
    )

@main_bp.route("/random")
def random_manga():
    shared = _get_shared()
    get_current_user = shared['get_current_user']
    get_db = shared['get_db']
    try:
        user = get_current_user()
        if not (user and user.get('is_premium')):
            return redirect(url_for('main.index'))
        conn = get_db()
        query = "SELECT slug FROM mangas"
        params = []
        if request.args.get("unread"):
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


@main_bp.route("/search")
def search_page():
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    get_cover_url = shared['get_cover_url']
    compute_rating = shared['compute_rating']
    get_all_mangas = shared['get_all_mangas']

    user = get_current_user()
    completed_map = {}
    if user:
        conn = get_db()
        hrows = conn.execute("SELECT manga_id, completed FROM user_history WHERE user_id=?", (user['id'],)).fetchall()
        completed_map = {r['manga_id']: r['completed'] for r in hrows}
        conn.close()

    q = (request.args.get("q") or "").strip()
    sort = request.args.get("sort", "date")
    min_rating = float(request.args.get("min_rating") or 0)
    unread_only = request.args.get("unread") == "1"
    # Multi tags support: ?tag=foo&tag=bar  or comma separated for convenience
    raw_tags = request.args.getlist("tag")
    if not raw_tags:
        raw_tags = (request.args.get("tags") or "").split(",")
    selected_tags = [t.strip() for t in raw_tags if t.strip()]

    # Get all unique tags for the nice multi-select UI - use cached version
    get_all_tags_fn = shared.get('get_all_tags', lambda: [])
    all_tags = get_all_tags_fn()

    # Optimized: use get_all_mangas for text search (uses indexed LIKE)
    search_for_query = q if q else None
    # Load more than page to allow filtering, then paginate
    mangas = get_all_mangas(search=search_for_query, limit=200)  # reasonable cap for perf

    results = []
    for m in mangas:
        tags = json.loads(m["tags"] or "[]")
        title = m["title"] or ""
        author = dict(m).get("author", "") or ""

        # multi-tag filter (AND) - python is fine after limited fetch
        if selected_tags and not all(t in tags for t in selected_tags):
            continue

        avg, cnt = compute_rating(m)
        if min_rating > 0 and avg < min_rating:
            continue

        is_read = completed_map.get(m["id"], 0) == 1
        if unread_only and user and is_read:
            continue

        cover = get_cover_url(m, thumb=True)
        pages_json = m["pages"] or "[]"
        results.append({
            "id": m["id"],
            "slug": m["slug"],
            "title": title,
            "author": author,
            "cover": cover,
            "tags": tags,
            "rating": avg,
            "rating_count": cnt,
            "pages_count": len(json.loads(pages_json)),
            "is_read": is_read,
        })

    # sorting
    if sort == "rating":
        results.sort(key=lambda x: -x["rating"])
    elif sort == "pages":
        results.sort(key=lambda x: -x["pages_count"])
    elif sort == "title":
        results.sort(key=lambda x: x["title"].lower())
    else:
        # date-ish (id desc as proxy since no created in this query easily)
        results.sort(key=lambda x: -x["id"])

    # Pagination for search
    search_page = request.args.get('page', 1, type=int) or 1
    if search_page < 1: search_page = 1
    per_page = 24
    start = (search_page - 1) * per_page
    paged_results = results[start : start + per_page]
    total_search_pages = (len(results) + per_page - 1) // per_page if results else 1

    partial = bool(request.args.get('partial'))

    return render_template(
        "search.html",
        results=paged_results,
        q=q,
        sort=sort,
        min_rating=min_rating,
        unread_only=unread_only,
        selected_tags=selected_tags,
        all_tags=all_tags,
        current_user=user,
        partial=partial,
        search_page=search_page,
        total_search_pages=total_search_pages,
    )

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
            # Ban check
            if user.get('banned_until'):
                try:
                    import datetime
                    ban_time = datetime.datetime.fromisoformat(user['banned_until'])
                    if ban_time > datetime.datetime.utcnow():
                        return render_template("login.html", error="Аккаунт заблокирован до " + user['banned_until'][:16], users=shared['get_all_users']())
                except:
                    pass
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
        WHERE f.user_id = ? ORDER BY f.added_at DESC LIMIT 100
    """, (user['id'],)).fetchall()
    favorites = []
    for r in fav_rows:
        cover = get_cover_url(r, thumb=True)
        favorites.append({**dict(r), "cover": cover})

    hist_rows = conn.execute("""
        SELECT m.*, h.last_page, h.completed, h.last_read_at
        FROM user_history h
        JOIN mangas m ON m.id = h.manga_id
        WHERE h.user_id = ? ORDER BY h.last_read_at DESC LIMIT 100
    """, (user['id'],)).fetchall()
    history = []
    for r in hist_rows:
        cover = get_cover_url(r, thumb=True)
        history.append({**dict(r), "cover": cover})

    rating_rows = conn.execute("""
        SELECT m.* , ur.score FROM user_ratings ur
        JOIN mangas m ON m.id = ur.manga_id
        WHERE ur.user_id = ? ORDER BY ur.rated_at DESC
    """, (user['id'],)).fetchall()
    ratings = []
    for r in rating_rows:
        rd = dict(r)
        rd["cover"] = get_cover_url(r, thumb=True)
        ratings.append(rd)

    conn.close()

    tag_weights = get_user_tag_weights(user['id'])
    sorted_weights = sorted(tag_weights.items(), key=lambda x: -x[1])[:15]

    recommendations = []
    if user.get('is_premium'):
        recommendations = get_recommendations(user['id'], limit=6)

    return render_template("profile.html", user=user, favorites=favorites, history=history, my_ratings=ratings, recommendations=recommendations, tag_weights=sorted_weights, show_full_history=bool(request.args.get('show_full_history')))

@main_bp.route("/u/<username>")
def public_showcase(username):
    shared = _get_shared()
    get_db = shared['get_db']
    get_cover_url = shared['get_cover_url']
    get_user_tag_weights = shared.get('get_user_tag_weights', lambda x: {})

    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    user_dict = dict(user) if user else {}
    if not user or not user_dict.get('showcase_public', 1):
        conn.close()
        # Friendly instead of hard error page
        return render_template("public_showcase.html", 
                               profile_user={"username": username, "hidden": True}, 
                               favorites=[], my_ratings=[], tag_weights=[])

    # Public data only
    fav_rows = conn.execute("""
        SELECT m.* FROM mangas m
        JOIN user_favorites f ON m.id = f.manga_id
        WHERE f.user_id = ? ORDER BY f.added_at DESC LIMIT 12
    """, (user['id'],)).fetchall()
    favorites = []
    for r in fav_rows:
        cover = get_cover_url(r, thumb=True)
        favorites.append({**dict(r), "cover": cover})

    rating_rows = conn.execute("""
        SELECT m.title, m.slug, ur.score FROM user_ratings ur
        JOIN mangas m ON m.id = ur.manga_id
        WHERE ur.user_id = ? ORDER BY ur.rated_at DESC LIMIT 12
    """, (user['id'],)).fetchall()
    rating_rows = [dict(r) for r in rating_rows]

    tag_weights = get_user_tag_weights(user['id'])
    sorted_weights = sorted(tag_weights.items(), key=lambda x: -x[1])[:8]

    conn.close()

    return render_template("public_showcase.html", 
                           profile_user=user_dict, 
                           favorites=favorites, 
                           my_ratings=rating_rows, 
                           tag_weights=sorted_weights)


@main_bp.route("/recommendations")
def recommendations_page():
    shared = _get_shared()
    get_current_user = shared['get_current_user']
    get_recommendations = shared.get('get_recommendations', lambda x, limit=20: [])
    user = get_current_user()
    if not user or not user.get('is_premium'):
        return redirect(url_for('main.index'))
    recs = get_recommendations(user['id'], limit=48)
    return render_template("recommendations.html", recommendations=recs, current_user=user)


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