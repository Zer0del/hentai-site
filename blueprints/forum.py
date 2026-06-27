from flask import Blueprint, render_template, request, redirect, url_for, abort, jsonify
from datetime import datetime
import json
import os

forum_bp = Blueprint('forum', __name__, url_prefix='/forum')

def _get_shared():
    from app import (
        get_current_user, get_db, save_uploaded_file, FORUM_ATTACH_DIR
    )
    return {
        'get_current_user': get_current_user,
        'get_db': get_db,
        'save_uploaded_file': save_uploaded_file,
        'FORUM_ATTACH_DIR': FORUM_ATTACH_DIR,
    }

@forum_bp.route('/')
def forum_index():
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    user = get_current_user()

    conn = get_db()
    categories = conn.execute("""
        SELECT * FROM forum_categories ORDER BY display_order, id
    """).fetchall()

    forums_by_cat = {}
    for cat in categories:
        forums = conn.execute("""
            SELECT f.*, 
                   (SELECT COUNT(*) FROM forum_topics WHERE forum_id = f.id) as topic_count,
                   (SELECT COUNT(*) FROM forum_posts p JOIN forum_topics t ON p.topic_id = t.id WHERE t.forum_id = f.id) as post_count
            FROM forum_forums f 
            WHERE f.category_id = ? 
            ORDER BY f.display_order, f.id
        """, (cat['id'],)).fetchall()
        forums_by_cat[cat['id']] = forums

    # Get recent topics
    recent = conn.execute("""
        SELECT t.*, f.name as forum_name, u.username, 
               (SELECT COUNT(*) FROM forum_posts WHERE topic_id = t.id) - 1 as reply_count
        FROM forum_topics t
        JOIN forum_forums f ON t.forum_id = f.id
        JOIN users u ON t.user_id = u.id
        ORDER BY t.last_post_at DESC LIMIT 5
    """).fetchall()

    conn.close()
    return render_template("forum/index.html", 
                           categories=categories, 
                           forums_by_cat=forums_by_cat,
                           recent=recent,
                           current_user=user)

@forum_bp.route('/f/<int:forum_id>')
def view_forum(forum_id):
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    user = get_current_user()

    conn = get_db()
    forum = conn.execute("SELECT * FROM forum_forums WHERE id = ?", (forum_id,)).fetchone()
    if not forum:
        conn.close()
        abort(404)

    category = conn.execute("SELECT * FROM forum_categories WHERE id = ?", (forum['category_id'],)).fetchone()

    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page

    topics = conn.execute("""
        SELECT t.*, u.username,
               (SELECT COUNT(*) FROM forum_posts WHERE topic_id = t.id) - 1 as reply_count,
               (SELECT p.created_at FROM forum_posts p WHERE p.topic_id = t.id ORDER BY p.created_at DESC LIMIT 1) as last_post_time
        FROM forum_topics t
        JOIN users u ON t.user_id = u.id
        WHERE t.forum_id = ?
        ORDER BY t.is_pinned DESC, t.last_post_at DESC
        LIMIT ? OFFSET ?
    """, (forum_id, per_page, offset)).fetchall()

    total_topics = conn.execute("SELECT COUNT(*) as c FROM forum_topics WHERE forum_id = ?", (forum_id,)).fetchone()["c"]
    conn.close()

    total_pages = (total_topics + per_page - 1) // per_page

    return render_template("forum/forum.html",
                           forum=forum,
                           category=category,
                           topics=topics,
                           page=page,
                           total_pages=total_pages,
                           current_user=user)

@forum_bp.route('/topic/<int:topic_id>')
def view_topic(topic_id):
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    user = get_current_user()

    conn = get_db()
    topic = conn.execute("""
        SELECT t.*, f.name as forum_name, f.id as forum_id, u.username
        FROM forum_topics t
        JOIN forum_forums f ON t.forum_id = f.id
        JOIN users u ON t.user_id = u.id
        WHERE t.id = ?
    """, (topic_id,)).fetchone()
    if not topic:
        conn.close()
        abort(404)

    page = request.args.get('page', 1, type=int)
    per_page = 20
    offset = (page - 1) * per_page

    posts = conn.execute("""
        SELECT p.*, u.username, u.username_color, u.avatar,
               COALESCE((SELECT SUM(vote) FROM forum_post_votes WHERE post_id=p.id), 0) as score,
               COALESCE((SELECT vote FROM forum_post_votes WHERE post_id=p.id AND user_id=?), 0) as my_vote
        FROM forum_posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.topic_id = ?
        ORDER BY p.created_at ASC
        LIMIT ? OFFSET ?
    """, (user['id'] if user else 0, topic_id, per_page, offset)).fetchall()

    total_posts = conn.execute("SELECT COUNT(*) as c FROM forum_posts WHERE topic_id = ?", (topic_id,)).fetchone()["c"]
    conn.close()

    total_pages = (total_posts + per_page - 1) // per_page

    return render_template("forum/topic.html",
                           topic=topic,
                           posts=posts,
                           page=page,
                           total_pages=total_pages,
                           current_user=user)

@forum_bp.route('/create_topic/<int:forum_id>', methods=['GET', 'POST'])
def create_topic(forum_id):
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    save_uploaded_file = shared['save_uploaded_file']
    FORUM_ATTACH_DIR = shared['FORUM_ATTACH_DIR']
    user = get_current_user()

    if not user:
        return redirect(url_for('main.login'))

    conn = get_db()
    forum = conn.execute("SELECT * FROM forum_forums WHERE id = ?", (forum_id,)).fetchone()
    if not forum:
        conn.close()
        abort(404)

    if request.method == 'POST':
        title = (request.form.get('title') or '').strip()
        content = (request.form.get('content') or '').strip()

        if not title or not content:
            conn.close()
            return render_template("forum/create_topic.html", forum=forum, error="Title and content required", current_user=user)

        attachment = ''
        file = request.files.get('attachment')
        if file and file.filename:
            if not user.get('is_premium'):
                conn.close()
                return render_template("forum/create_topic.html", forum=forum, error="Attachments only for premium users", current_user=user)
            if file.content_length and file.content_length > 5 * 1024 * 1024:
                conn.close()
                return render_template("forum/create_topic.html", forum=forum, error="File too large (max 5MB)", current_user=user)
            saved = save_uploaded_file(file, FORUM_ATTACH_DIR, f"topic{user['id']}")
            if saved:
                attachment = saved

        now = datetime.utcnow().isoformat()
        topic_id = conn.execute("""
            INSERT INTO forum_topics (forum_id, user_id, title, created_at, last_post_at)
            VALUES (?, ?, ?, ?, ?)
        """, (forum_id, user['id'], title, now, now)).lastrowid

        conn.execute("""
            INSERT INTO forum_posts (topic_id, user_id, content, created_at, attachment)
            VALUES (?, ?, ?, ?, ?)
        """, (topic_id, user['id'], content, now, attachment))

        conn.execute("""
            UPDATE forum_topics SET last_post_at = ? WHERE id = ?
        """, (now, topic_id))

        conn.commit()
        conn.close()
        return redirect(url_for('forum.view_topic', topic_id=topic_id))

    conn.close()
    return render_template("forum/create_topic.html", forum=forum, current_user=user)

@forum_bp.route('/topic/<int:topic_id>/reply', methods=['POST'])
def reply_topic(topic_id):
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    save_uploaded_file = shared['save_uploaded_file']
    FORUM_ATTACH_DIR = shared['FORUM_ATTACH_DIR']
    user = get_current_user()

    if not user:
        return redirect(url_for('main.login'))

    content = (request.form.get('content') or '').strip()
    if not content:
        return redirect(url_for('forum.view_topic', topic_id=topic_id))

    attachment = ''
    file = request.files.get('attachment')
    if file and file.filename:
        if not user.get('is_premium'):
            return redirect(url_for('forum.view_topic', topic_id=topic_id))
        # Basic size limit 5MB for forum attachments
        if file.content_length and file.content_length > 5 * 1024 * 1024:
            return redirect(url_for('forum.view_topic', topic_id=topic_id))
        saved = save_uploaded_file(file, FORUM_ATTACH_DIR, f"post{user['id']}")
        if saved:
            attachment = saved

    conn = get_db()
    topic = conn.execute("SELECT * FROM forum_topics WHERE id = ?", (topic_id,)).fetchone()
    if not topic or topic['is_locked']:
        conn.close()
        abort(403)

    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO forum_posts (topic_id, user_id, content, created_at, attachment)
        VALUES (?, ?, ?, ?, ?)
    """, (topic_id, user['id'], content, now, attachment))

    conn.execute("""
        UPDATE forum_topics SET last_post_at = ? WHERE id = ?
    """, (now, topic_id))

    conn.commit()
    conn.close()
    return redirect(url_for('forum.view_topic', topic_id=topic_id) + '#posts')

# Simple delete for admin
@forum_bp.route('/post/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    user = get_current_user()
    FORUM_ATTACH_DIR = shared.get('FORUM_ATTACH_DIR', '')

    if not user or not user.get('is_admin'):
        abort(403)

    conn = get_db()
    post = conn.execute("SELECT * FROM forum_posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        conn.close()
        abort(404)

    topic_id = post['topic_id']
    att = post.get('attachment', '')
    conn.execute("DELETE FROM forum_posts WHERE id = ?", (post_id,))

    # Update last post if needed
    last = conn.execute("""
        SELECT created_at FROM forum_posts WHERE topic_id = ? ORDER BY created_at DESC LIMIT 1
    """, (topic_id,)).fetchone()
    if last:
        conn.execute("UPDATE forum_topics SET last_post_at = ? WHERE id = ?", (last['created_at'], topic_id))
    else:
        conn.execute("DELETE FROM forum_topics WHERE id = ?", (topic_id))

    if att and FORUM_ATTACH_DIR:
        try:
            p = os.path.join(FORUM_ATTACH_DIR, att)
            if os.path.exists(p):
                os.remove(p)
        except:
            pass

    conn.commit()
    conn.close()
    return redirect(url_for('forum.view_topic', topic_id=topic_id) if last else url_for('forum.forum_index'))

@forum_bp.route('/post/<int:post_id>/vote', methods=['POST'])
def vote_post(post_id):
    shared = _get_shared()
    get_db = shared['get_db']
    get_current_user = shared['get_current_user']
    user = get_current_user()

    if not user:
        return jsonify({"error": "login required"}), 403

    vote = int(request.form.get('vote', 0))
    if vote not in (1, -1):
        return jsonify({"error": "invalid vote"}), 400

    conn = get_db()
    post = conn.execute("SELECT id FROM forum_posts WHERE id = ?", (post_id,)).fetchone()
    if not post:
        conn.close()
        return jsonify({"error": "post not found"}), 404

    # Upsert vote
    existing = conn.execute("SELECT vote FROM forum_post_votes WHERE post_id=? AND user_id=?", (post_id, user['id'])).fetchone()
    if existing:
        if existing['vote'] == vote:
            # toggle off
            conn.execute("DELETE FROM forum_post_votes WHERE post_id=? AND user_id=?", (post_id, user['id']))
            new_vote = 0
        else:
            conn.execute("UPDATE forum_post_votes SET vote=? WHERE post_id=? AND user_id=?", (vote, post_id, user['id']))
            new_vote = vote
    else:
        conn.execute("INSERT INTO forum_post_votes (post_id, user_id, vote) VALUES (?, ?, ?)", (post_id, user['id'], vote))
        new_vote = vote

    # get new score
    score_row = conn.execute("SELECT COALESCE(SUM(vote),0) as score FROM forum_post_votes WHERE post_id=?", (post_id,)).fetchone()
    score = score_row['score']
    conn.commit()
    conn.close()

    return jsonify({"ok": True, "score": score, "my_vote": new_vote})

print("forum blueprint loaded")