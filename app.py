import os
import json
import csv
import io
from datetime import datetime
from functools import wraps
from flask import Flask, render_template, request, redirect, url_for, flash, jsonify, Response, send_file
from flask_login import LoginManager, login_user, login_required, logout_user, current_user
from models import (init_db, get_db, User, log_revision, DB_PATH,
                    create_batch, batch_add_sample_record, batch_add_annotation_record,
                    batch_add_conflict_record, batch_add_review_task_record,
                    batch_link_revision, update_batch_stats, confirm_batch,
                    get_batch, list_batches, get_batch_sample_records,
                    get_batch_annotation_records, get_batch_conflict_records,
                    get_batch_review_task_records, revert_batch)

app = Flask(__name__)
app.secret_key = 'offline-annotation-review-secret-key-local-only'
app.config['TEMPLATES_AUTO_RELOAD'] = True

login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'
login_manager.login_message = '请先登录'


@login_manager.user_loader
def load_user(user_id):
    return User.get(int(user_id))


def role_required(*roles):
    def decorator(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for('login'))
            if current_user.role not in roles:
                flash('无权限访问该页面', 'error')
                return redirect(url_for('dashboard'))
            return f(*args, **kwargs)
        return decorated_function
    return decorator


@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        user = User.authenticate(username, password)
        if user:
            login_user(user)
            log_revision('user', user.id, 'login', user_id=user.id)
            return redirect(url_for('dashboard'))
        flash('用户名或密码错误', 'error')
    return render_template('login.html')


@app.route('/logout')
@login_required
def logout():
    log_revision('user', current_user.id, 'logout', user_id=current_user.id)
    logout_user()
    return redirect(url_for('login'))


@app.route('/')
@login_required
def dashboard():
    conn = get_db()
    stats = {}
    stats['total_samples'] = conn.execute("SELECT COUNT(*) FROM samples").fetchone()[0]
    stats['total_annotations'] = conn.execute("SELECT COUNT(*) FROM annotations").fetchone()[0]
    stats['open_conflicts'] = conn.execute("SELECT COUNT(*) FROM conflicts WHERE status IN ('open','assigned')").fetchone()[0]
    stats['resolved_conflicts'] = conn.execute("SELECT COUNT(*) FROM conflicts WHERE status = 'resolved'").fetchone()[0]

    if current_user.is_annotator():
        stats['my_annotations'] = conn.execute(
            "SELECT COUNT(*) FROM annotations WHERE annotator_id = ?", (current_user.id,)
        ).fetchone()[0]
    if current_user.is_reviewer():
        stats['my_reviews_pending'] = conn.execute(
            "SELECT COUNT(*) FROM review_tasks WHERE reviewer_id = ? AND status IN ('pending','in_progress')",
            (current_user.id,)
        ).fetchone()[0]
        stats['my_reviews_done'] = conn.execute(
            "SELECT COUNT(*) FROM review_tasks WHERE reviewer_id = ? AND status = 'reviewed'",
            (current_user.id,)
        ).fetchone()[0]

    active_schemes = conn.execute(
        "SELECT ls.*, COUNT(DISTINCT l.id) as label_count FROM label_schemes ls "
        "LEFT JOIN labels l ON l.scheme_id = ls.id GROUP BY ls.id ORDER BY ls.is_active DESC, ls.created_at DESC"
    ).fetchall()
    conn.close()

    return render_template('dashboard.html', stats=stats, active_schemes=active_schemes)


# ============ 标签方案管理 ============

@app.route('/schemes')
@login_required
@role_required('admin')
def schemes():
    conn = get_db()
    schemes_data = conn.execute(
        "SELECT ls.*, u.display_name as creator_name, "
        "(SELECT COUNT(*) FROM labels l WHERE l.scheme_id = ls.id) as label_count, "
        "(SELECT COUNT(*) FROM samples s WHERE s.scheme_id = ls.id) as sample_count, "
        "(SELECT COUNT(*) FROM annotations a WHERE a.scheme_id = ls.id) as annotation_count "
        "FROM label_schemes ls LEFT JOIN users u ON u.id = ls.created_by "
        "ORDER BY ls.name, ls.version DESC"
    ).fetchall()
    conn.close()
    return render_template('schemes.html', schemes=schemes_data)


@app.route('/schemes/new', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def new_scheme():
    if request.method == 'POST':
        name = request.form.get('name', '').strip()
        description = request.form.get('description', '').strip()
        labels_json = request.form.get('labels_json', '[]')

        if not name:
            flash('方案名称不能为空', 'error')
            return redirect(url_for('new_scheme'))

        try:
            labels_data = json.loads(labels_json)
        except:
            labels_data = []

        conn = get_db()
        max_version = conn.execute(
            "SELECT MAX(version) FROM label_schemes WHERE name = ?", (name,)
        ).fetchone()[0] or 0
        new_version = max_version + 1

        cursor = conn.execute(
            "INSERT INTO label_schemes (name, version, description, is_active, created_by) VALUES (?, ?, ?, 1, ?)",
            (name, new_version, description, current_user.id)
        )
        scheme_id = cursor.lastrowid

        if max_version > 0:
            conn.execute("UPDATE label_schemes SET is_active = 0 WHERE name = ? AND version < ?", (name, new_version))

        for label in labels_data:
            if label.get('key') and label.get('text'):
                conn.execute(
                    "INSERT INTO labels (scheme_id, label_key, label_text, color, description) VALUES (?, ?, ?, ?, ?)",
                    (scheme_id, label['key'].strip(), label['text'].strip(),
                     label.get('color', '#3b82f6'), label.get('description', ''))
                )

        log_revision('label_scheme', scheme_id, 'create',
                     new_value=f"{name} v{new_version}", user_id=current_user.id,
                     comment=f"创建标签方案，含{len(labels_data)}个标签",
                     conn=conn)
        conn.commit()
        conn.close()
        flash(f'标签方案 "{name}" v{new_version} 创建成功', 'success')
        return redirect(url_for('schemes'))

    return render_template('scheme_edit.html', scheme=None, labels=[])


@app.route('/schemes/<int:scheme_id>')
@login_required
def view_scheme(scheme_id):
    conn = get_db()
    scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (scheme_id,)).fetchone()
    if not scheme:
        conn.close()
        flash('方案不存在', 'error')
        return redirect(url_for('schemes'))
    labels = conn.execute("SELECT * FROM labels WHERE scheme_id = ? ORDER BY label_key", (scheme_id,)).fetchall()

    old_annotations = conn.execute(
        "SELECT a.*, u.display_name as annotator_name, s.sample_id, s.content "
        "FROM annotations a JOIN users u ON u.id = a.annotator_id "
        "JOIN samples s ON s.id = a.sample_id WHERE a.scheme_id = ? LIMIT 50",
        (scheme_id,)
    ).fetchall()
    conn.close()
    return render_template('scheme_view.html', scheme=scheme, labels=labels, old_annotations=old_annotations)


@app.route('/schemes/<int:scheme_id>/labels/new', methods=['POST'])
@login_required
@role_required('admin')
def add_label_to_scheme(scheme_id):
    conn = get_db()
    scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (scheme_id,)).fetchone()
    if not scheme:
        conn.close()
        flash('方案不存在', 'error')
        return redirect(url_for('schemes'))

    label_key = request.form.get('label_key', '').strip()
    label_text = request.form.get('label_text', '').strip()
    color = request.form.get('color', '#3b82f6').strip()
    description = request.form.get('description', '').strip()

    if not label_key or not label_text:
        conn.close()
        flash('标签键和标签文本不能为空', 'error')
        return redirect(url_for('view_scheme', scheme_id=scheme_id))

    try:
        conn.execute(
            "INSERT INTO labels (scheme_id, label_key, label_text, color, description) VALUES (?, ?, ?, ?, ?)",
            (scheme_id, label_key, label_text, color, description)
        )
        log_revision('label_scheme', scheme_id, 'add_label',
                     new_value=f"{label_key}: {label_text}",
                     user_id=current_user.id,
                     comment=f"向方案 {scheme['name']} v{scheme['version']} 添加标签",
                     conn=conn)
        conn.commit()
        flash(f'标签 "{label_text}" 添加成功', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'添加标签失败: {e}', 'error')
    finally:
        conn.close()

    return redirect(url_for('view_scheme', scheme_id=scheme_id))


@app.route('/schemes/<int:scheme_id>/upgrade', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def upgrade_scheme(scheme_id):
    conn = get_db()
    old_scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (scheme_id,)).fetchone()
    if not old_scheme:
        conn.close()
        flash('方案不存在', 'error')
        return redirect(url_for('schemes'))

    old_labels = conn.execute("SELECT * FROM labels WHERE scheme_id = ? ORDER BY label_key", (scheme_id,)).fetchall()

    if request.method == 'POST':
        name = request.form.get('name', old_scheme['name']).strip()
        description = request.form.get('description', old_scheme['description'] or '').strip()
        labels_json = request.form.get('labels_json', '[]')

        try:
            labels_data = json.loads(labels_json)
        except:
            labels_data = []

        if not name:
            conn.close()
            flash('方案名称不能为空', 'error')
            return redirect(url_for('upgrade_scheme', scheme_id=scheme_id))

        max_version = conn.execute(
            "SELECT MAX(version) FROM label_schemes WHERE name = ?", (name,)
        ).fetchone()[0] or 0
        new_version = max_version + 1

        cursor = conn.execute(
            "INSERT INTO label_schemes (name, version, description, is_active, created_by) VALUES (?, ?, ?, 1, ?)",
            (name, new_version, description, current_user.id)
        )
        new_scheme_id = cursor.lastrowid

        conn.execute("UPDATE label_schemes SET is_active = 0 WHERE name = ? AND id != ?", (name, new_scheme_id))

        for label in labels_data:
            if label.get('key') and label.get('text'):
                conn.execute(
                    "INSERT INTO labels (scheme_id, label_key, label_text, color, description) VALUES (?, ?, ?, ?, ?)",
                    (new_scheme_id, label['key'].strip(), label['text'].strip(),
                     label.get('color', '#3b82f6'), label.get('description', ''))
                )

        log_revision('label_scheme', new_scheme_id, 'upgrade_from',
                     old_value=f"{old_scheme['name']} v{old_scheme['version']}",
                     new_value=f"{name} v{new_version}",
                     user_id=current_user.id,
                     comment=f"从旧方案升级，旧数据保留在原方案v{old_scheme['version']}，不自动迁移",
                     conn=conn)
        conn.commit()
        conn.close()
        flash(f'方案已升级为 v{new_version}，旧版本数据保持独立，不会自动迁移', 'success')
        return redirect(url_for('view_scheme', scheme_id=new_scheme_id))

    conn.close()
    return render_template('scheme_edit.html', scheme=old_scheme, labels=old_labels, is_upgrade=True)


# ============ 样本管理 ============

@app.route('/samples')
@login_required
def samples():
    conn = get_db()
    schemes = conn.execute("SELECT * FROM label_schemes ORDER BY is_active DESC, name, version DESC").fetchall()

    query = "SELECT s.*, ls.name as scheme_name, ls.version as scheme_version, " \
            "(SELECT COUNT(*) FROM annotations a WHERE a.sample_id = s.id) as annotation_count " \
            "FROM samples s LEFT JOIN label_schemes ls ON ls.id = s.scheme_id WHERE 1=1"
    params = []

    scheme_filter = request.args.get('scheme_id', '')
    keyword = request.args.get('keyword', '').strip()

    if scheme_filter:
        query += " AND s.scheme_id = ?"
        params.append(int(scheme_filter))
    if keyword:
        query += " AND (s.sample_id LIKE ? OR s.content LIKE ?)"
        params.extend([f'%{keyword}%', f'%{keyword}%'])

    query += " ORDER BY s.imported_at DESC LIMIT 500"
    samples_data = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('samples.html', samples=samples_data, schemes=schemes,
                           scheme_filter=scheme_filter, keyword=keyword)


@app.route('/samples/import', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'annotator')
def import_samples():
    conn = get_db()
    schemes = conn.execute("SELECT * FROM label_schemes WHERE is_active = 1 ORDER BY name, version DESC").fetchall()

    if request.method == 'POST':
        scheme_id = request.form.get('scheme_id')
        file = request.files.get('file')

        if not scheme_id or not file:
            conn.close()
            flash('请选择标签方案和上传文件', 'error')
            return redirect(url_for('import_samples'))

        scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (int(scheme_id),)).fetchone()
        if not scheme:
            conn.close()
            flash('标签方案不存在', 'error')
            return redirect(url_for('import_samples'))

        flash('⚠️ 建议使用新的预演导入流程，可以先预览导入效果再确认入库。'
              '旧的直接导入方式已不推荐使用。', 'warning')

        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))

        imported = 0
        duplicates = []
        errors = []

        for i, row in enumerate(reader, start=2):
            sample_id = (row.get('sample_id') or row.get('id') or '').strip()
            text = (row.get('content') or row.get('text') or row.get('sample') or '').strip()
            metadata = {k: v for k, v in row.items() if k not in ('sample_id', 'id', 'content', 'text', 'sample')}

            if not sample_id or not text:
                errors.append(f"第{i}行: 缺少样本编号或内容")
                continue

            existing = conn.execute("SELECT id FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
            if existing:
                duplicates.append(sample_id)
                continue

            conn.execute(
                "INSERT INTO samples (sample_id, content, scheme_id, imported_by, metadata) VALUES (?, ?, ?, ?, ?)",
                (sample_id, text, scheme['id'], current_user.id, json.dumps(metadata, ensure_ascii=False) if metadata else None)
            )
            imported += 1

        log_revision('samples', scheme['id'], 'import',
                     new_value=f"导入{imported}条，重复{len(duplicates)}条，错误{len(errors)}条",
                     user_id=current_user.id,
                     comment=f"导入样本到方案 {scheme['name']} v{scheme['version']}",
                     conn=conn)
        conn.commit()
        conn.close()

        msg = f'成功导入 {imported} 条样本'
        if duplicates:
            msg += f'，跳过重复编号 {len(duplicates)} 条'
        if errors:
            msg += f'，错误 {len(errors)} 条'
        flash(msg, 'success' if imported > 0 else 'warning')
        return redirect(url_for('samples'))

    conn.close()
    return render_template('sample_import.html', schemes=schemes)


@app.route('/samples/<int:sample_id>')
@login_required
def view_sample(sample_id):
    conn = get_db()
    sample = conn.execute(
        "SELECT s.*, ls.name as scheme_name, ls.version as scheme_version "
        "FROM samples s LEFT JOIN label_schemes ls ON ls.id = s.scheme_id WHERE s.id = ?",
        (sample_id,)
    ).fetchone()
    if not sample:
        conn.close()
        flash('样本不存在', 'error')
        return redirect(url_for('samples'))

    annotations = conn.execute(
        "SELECT a.*, u.display_name as annotator_name "
        "FROM annotations a JOIN users u ON u.id = a.annotator_id WHERE a.sample_id = ? ORDER BY a.created_at",
        (sample_id,)
    ).fetchall()

    conflicts = conn.execute(
        "SELECT c.*, rt.status as review_status, u.display_name as reviewer_name "
        "FROM conflicts c LEFT JOIN review_tasks rt ON rt.conflict_id = c.id "
        "LEFT JOIN users u ON u.id = rt.reviewer_id WHERE c.sample_id = ? ORDER BY c.detected_at DESC",
        (sample_id,)
    ).fetchall()

    history = conn.execute(
        "SELECT rh.*, u.display_name as user_name FROM revision_history rh "
        "LEFT JOIN users u ON u.id = rh.user_id WHERE rh.entity_type = 'sample' AND rh.entity_id = ? "
        "ORDER BY rh.created_at DESC",
        (sample_id,)
    ).fetchall()

    conn.close()
    return render_template('sample_view.html', sample=sample, annotations=annotations,
                           conflicts=conflicts, history=history)


# ============ 标注结果管理 ============

@app.route('/annotations')
@login_required
def annotations():
    conn = get_db()
    schemes = conn.execute("SELECT * FROM label_schemes ORDER BY is_active DESC, name, version DESC").fetchall()

    query = "SELECT a.*, u.display_name as annotator_name, s.sample_id, s.content, " \
            "ls.name as scheme_name, ls.version as scheme_version " \
            "FROM annotations a JOIN users u ON u.id = a.annotator_id " \
            "JOIN samples s ON s.id = a.sample_id " \
            "LEFT JOIN label_schemes ls ON ls.id = a.scheme_id WHERE 1=1"
    params = []

    scheme_filter = request.args.get('scheme_id', '')
    annotator_filter = request.args.get('annotator_id', '')
    unknown_only = request.args.get('unknown_only', '')

    if scheme_filter:
        query += " AND a.scheme_id = ?"
        params.append(int(scheme_filter))
    if current_user.is_annotator() and not current_user.is_admin():
        query += " AND a.annotator_id = ?"
        params.append(current_user.id)
    elif annotator_filter:
        query += " AND a.annotator_id = ?"
        params.append(int(annotator_filter))
    if unknown_only:
        query += " AND a.is_unknown_label = 1"

    query += " ORDER BY a.created_at DESC LIMIT 500"
    annotations_data = conn.execute(query, params).fetchall()

    users = conn.execute("SELECT * FROM users ORDER BY role, username").fetchall()
    conn.close()
    return render_template('annotations.html', annotations=annotations_data, schemes=schemes,
                           users=users, scheme_filter=scheme_filter, annotator_filter=annotator_filter,
                           unknown_only=unknown_only)


@app.route('/annotations/import', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'annotator')
def import_annotations():
    conn = get_db()
    schemes = conn.execute("SELECT * FROM label_schemes ORDER BY is_active DESC, name, version DESC").fetchall()

    if request.method == 'POST':
        scheme_id = request.form.get('scheme_id')
        annotator_id = request.form.get('annotator_id', str(current_user.id))
        file = request.files.get('file')

        if not scheme_id or not file:
            conn.close()
            flash('请选择标签方案和上传文件', 'error')
            return redirect(url_for('import_annotations'))

        scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (int(scheme_id),)).fetchone()
        if not scheme:
            conn.close()
            flash('标签方案不存在', 'error')
            return redirect(url_for('import_annotations'))

        if current_user.is_annotator() and int(annotator_id) != current_user.id:
            conn.close()
            flash('标注员只能导入自己的标注结果', 'error')
            return redirect(url_for('import_annotations'))

        flash('⚠️ 建议使用新的预演导入流程，可以先预览导入效果再确认入库。'
              '旧的直接导入方式已不推荐使用。', 'warning')

        scheme_labels = conn.execute(
            "SELECT id, label_key, label_text FROM labels WHERE scheme_id = ?", (scheme['id'],)
        ).fetchall()
        label_map = {}
        for lbl in scheme_labels:
            label_map[lbl['label_key']] = lbl
            label_map[lbl['label_text']] = lbl

        content = file.read().decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(content))

        imported = 0
        unknown_labels = []
        missing_samples = []
        errors = []
        skipped_duplicates = 0

        for i, row in enumerate(reader, start=2):
            sample_id = (row.get('sample_id') or row.get('id') or '').strip()
            label_key = (row.get('label') or row.get('label_key') or '').strip()
            label_text = (row.get('label_text') or '').strip()
            comment = (row.get('comment') or row.get('note') or '').strip()

            if not sample_id or not label_key:
                errors.append(f"第{i}行: 缺少样本编号或标签")
                continue

            sample = conn.execute("SELECT id FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
            if not sample:
                missing_samples.append((sample_id, i))
                continue

            matched_label = label_map.get(label_key) or label_map.get(label_text)
            if not matched_label:
                unknown_labels.append((sample_id, label_key, i))
                continue

            final_label_id = matched_label['id']
            final_label_key = matched_label['label_key']
            final_label_text = matched_label['label_text']

            existing = conn.execute(
                "SELECT id FROM annotations WHERE sample_id = ? AND annotator_id = ? AND scheme_id = ?",
                (sample['id'], int(annotator_id), scheme['id'])
            ).fetchone()

            if existing:
                conn.execute(
                    "UPDATE annotations SET label_id=?, label_key=?, label_text=?, is_unknown_label=0, "
                    "comment=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (final_label_id, final_label_key, final_label_text, comment, existing['id'])
                )
                log_revision('annotation', existing['id'], 'update',
                             user_id=current_user.id, comment=f"更新标注 {sample_id}",
                             conn=conn)
            else:
                cursor = conn.execute(
                    "INSERT INTO annotations (sample_id, annotator_id, scheme_id, label_id, label_key, "
                    "label_text, is_unknown_label, comment) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
                    (sample['id'], int(annotator_id), scheme['id'], final_label_id,
                     final_label_key, final_label_text, comment)
                )
                log_revision('annotation', cursor.lastrowid, 'create',
                             user_id=current_user.id, comment=f"新增标注 {sample_id}",
                             conn=conn)
            imported += 1

        conn.commit()
        conn.close()

        parts = []
        if imported > 0:
            parts.append(f'成功导入/更新 {imported} 条标注')
        if unknown_labels:
            detail = ", ".join([f"{s}(L{ln}:{lb})" for s, lb, ln in unknown_labels[:8]])
            if len(unknown_labels) > 8:
                detail += f"... 共{len(unknown_labels)}条"
            parts.append(f'未知标签被跳过 {len(unknown_labels)} 条: {detail}')
        if missing_samples:
            detail = ", ".join([f"{s}(L{ln})" for s, ln in missing_samples[:8]])
            if len(missing_samples) > 8:
                detail += f"... 共{len(missing_samples)}条"
            parts.append(f'缺失样本被跳过 {len(missing_samples)} 条: {detail}')
        if errors:
            parts.append(f'其他错误 {len(errors)} 条')
        msg = '；'.join(parts) if parts else '没有合法的标注行被导入'
        flash(msg, 'success' if imported > 0 else 'warning')
        return redirect(url_for('annotations'))

    users = conn.execute("SELECT * FROM users WHERE role = 'annotator' ORDER BY username").fetchall()
    conn.close()
    return render_template('annotation_import.html', schemes=schemes, users=users)


# ============ 冲突检测与管理 ============

@app.route('/conflicts')
@login_required
def conflicts():
    conn = get_db()
    schemes = conn.execute("SELECT * FROM label_schemes ORDER BY is_active DESC, name, version DESC").fetchall()

    query = "SELECT c.*, s.sample_id, s.content, ls.name as scheme_name, ls.version as scheme_version, "
    query += "(SELECT COUNT(*) FROM conflict_annotations ca WHERE ca.conflict_id = c.id) as party_count, "
    query += "u.display_name as resolver_name "
    query += "FROM conflicts c JOIN samples s ON s.id = c.sample_id "
    query += "LEFT JOIN label_schemes ls ON ls.id = c.scheme_id "
    query += "LEFT JOIN users u ON u.id = c.resolved_by WHERE 1=1"
    params = []

    scheme_filter = request.args.get('scheme_id', '')
    status_filter = request.args.get('status', '')

    if scheme_filter:
        query += " AND c.scheme_id = ?"
        params.append(int(scheme_filter))
    if status_filter:
        query += " AND c.status = ?"
        params.append(status_filter)

    query += " ORDER BY c.detected_at DESC LIMIT 500"
    conflicts_data = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('conflicts.html', conflicts=conflicts_data, schemes=schemes,
                           scheme_filter=scheme_filter, status_filter=status_filter)


@app.route('/conflicts/detect', methods=['POST'])
@login_required
@role_required('admin')
def detect_conflicts():
    scheme_id = request.form.get('scheme_id')
    if not scheme_id:
        flash('请选择标签方案', 'error')
        return redirect(url_for('conflicts'))

    conn = get_db()
    scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (int(scheme_id),)).fetchone()
    if not scheme:
        conn.close()
        flash('方案不存在', 'error')
        return redirect(url_for('conflicts'))

    cursor = conn.execute(
        "SELECT sample_id, GROUP_CONCAT(DISTINCT COALESCE(label_key, '')) as labels, "
        "GROUP_CONCAT(DISTINCT annotator_id) as annotators, COUNT(*) as cnt "
        "FROM annotations WHERE scheme_id = ? AND is_unknown_label = 0 "
        "GROUP BY sample_id HAVING COUNT(DISTINCT COALESCE(label_key, '')) > 1 OR COUNT(DISTINCT annotator_id) > 1",
        (scheme['id'],)
    )
    potential_conflicts = cursor.fetchall()

    new_conflicts = 0
    for pc in potential_conflicts:
        labels_list = [l for l in pc['labels'].split(',') if l]
        if len(set(labels_list)) <= 1:
            continue

        existing = conn.execute(
            "SELECT id FROM conflicts WHERE sample_id = ? AND scheme_id = ? AND status != 'resolved'",
            (pc['sample_id'], scheme['id'])
        ).fetchone()
        if existing:
            continue

        cursor2 = conn.execute(
            "INSERT INTO conflicts (sample_id, scheme_id, status) VALUES (?, ?, 'open')",
            (pc['sample_id'], scheme['id'])
        )
        conflict_id = cursor2.lastrowid

        anns = conn.execute(
            "SELECT id FROM annotations WHERE sample_id = ? AND scheme_id = ?",
            (pc['sample_id'], scheme['id'])
        ).fetchall()
        for a in anns:
            conn.execute(
                "INSERT INTO conflict_annotations (conflict_id, annotation_id) VALUES (?, ?)",
                (conflict_id, a['id'])
            )
        log_revision('conflict', conflict_id, 'detect',
                     user_id=current_user.id, comment=f"检测到样本冲突",
                     conn=conn)
        new_conflicts += 1

    conn.commit()
    conn.close()
    flash(f'检测完成，新增冲突 {new_conflicts} 个', 'success')
    return redirect(url_for('conflicts'))


@app.route('/conflicts/<int:conflict_id>')
@login_required
def view_conflict(conflict_id):
    conn = get_db()
    conflict = conn.execute(
        "SELECT c.*, s.sample_id, s.content, ls.name as scheme_name, ls.version as scheme_version, "
        "u.display_name as resolver_name "
        "FROM conflicts c JOIN samples s ON s.id = c.sample_id "
        "LEFT JOIN label_schemes ls ON ls.id = c.scheme_id "
        "LEFT JOIN users u ON u.id = c.resolved_by WHERE c.id = ?",
        (conflict_id,)
    ).fetchone()
    if not conflict:
        conn.close()
        flash('冲突不存在', 'error')
        return redirect(url_for('conflicts'))

    conflict_annotations = conn.execute(
        "SELECT a.*, u.display_name as annotator_name FROM conflict_annotations ca "
        "JOIN annotations a ON a.id = ca.annotation_id "
        "JOIN users u ON u.id = a.annotator_id WHERE ca.conflict_id = ?",
        (conflict_id,)
    ).fetchall()

    labels = conn.execute(
        "SELECT * FROM labels WHERE scheme_id = ? ORDER BY label_key", (conflict['scheme_id'],)
    ).fetchall()

    review_tasks = conn.execute(
        "SELECT rt.*, u.display_name as reviewer_name, u2.display_name as assigner_name "
        "FROM review_tasks rt LEFT JOIN users u ON u.id = rt.reviewer_id "
        "LEFT JOIN users u2 ON u2.id = rt.assigned_by WHERE rt.conflict_id = ? ORDER BY rt.assigned_at DESC",
        (conflict_id,)
    ).fetchall()

    reviewers = conn.execute(
        "SELECT * FROM users WHERE role = 'reviewer' ORDER BY username"
    ).fetchall()

    history = conn.execute(
        "SELECT rh.*, u.display_name as user_name FROM revision_history rh "
        "LEFT JOIN users u ON u.id = rh.user_id WHERE rh.entity_type = 'conflict' AND rh.entity_id = ? "
        "ORDER BY rh.created_at DESC",
        (conflict_id,)
    ).fetchall()

    conn.close()
    return render_template('conflict_view.html', conflict=conflict,
                           conflict_annotations=conflict_annotations, labels=labels,
                           review_tasks=review_tasks, reviewers=reviewers, history=history)


@app.route('/conflicts/<int:conflict_id>/assign', methods=['POST'])
@login_required
@role_required('admin')
def assign_review(conflict_id):
    reviewer_id = request.form.get('reviewer_id', type=int)
    if not reviewer_id:
        flash('请选择复核员', 'error')
        return redirect(url_for('view_conflict', conflict_id=conflict_id))

    conn = get_db()
    conflict = conn.execute("SELECT * FROM conflicts WHERE id = ?", (conflict_id,)).fetchone()
    if not conflict:
        conn.close()
        flash('冲突不存在', 'error')
        return redirect(url_for('conflicts'))

    reviewer = conn.execute("SELECT * FROM users WHERE id = ? AND role = 'reviewer'", (reviewer_id,)).fetchone()
    if not reviewer:
        conn.close()
        flash('复核员不存在或角色错误', 'error')
        return redirect(url_for('view_conflict', conflict_id=conflict_id))

    conflict_annotators = conn.execute(
        "SELECT DISTINCT a.annotator_id FROM conflict_annotations ca "
        "JOIN annotations a ON a.id = ca.annotation_id WHERE ca.conflict_id = ?",
        (conflict_id,)
    ).fetchall()
    annotator_ids = [x['annotator_id'] for x in conflict_annotators]
    if reviewer_id in annotator_ids:
        conn.close()
        flash('复核员不能复核自己参与标注的样本', 'error')
        return redirect(url_for('view_conflict', conflict_id=conflict_id))

    conn.execute(
        "INSERT INTO review_tasks (conflict_id, reviewer_id, status, assigned_by) VALUES (?, ?, 'pending', ?)",
        (conflict_id, reviewer_id, current_user.id)
    )
    conn.execute("UPDATE conflicts SET status = 'assigned' WHERE id = ?", (conflict_id,))
    log_revision('conflict', conflict_id, 'assign_review',
                 new_value=f"分配给复核员 #{reviewer_id}",
                 user_id=current_user.id, comment=f"分配复核任务给 {reviewer['display_name']}",
                 conn=conn)
    conn.commit()
    conn.close()
    flash(f'已分配给 {reviewer["display_name"]}', 'success')
    return redirect(url_for('view_conflict', conflict_id=conflict_id))


# ============ 复核任务 ============

@app.route('/reviews')
@login_required
@role_required('admin', 'reviewer')
def reviews():
    conn = get_db()
    query = "SELECT rt.*, c.sample_id as conflict_sample_id, s.content, ls.name as scheme_name, "
    query += "ls.version as scheme_version, u.display_name as assigner_name "
    query += "FROM review_tasks rt JOIN conflicts c ON c.id = rt.conflict_id "
    query += "JOIN samples s ON s.id = c.sample_id "
    query += "LEFT JOIN label_schemes ls ON ls.id = c.scheme_id "
    query += "LEFT JOIN users u ON u.id = rt.assigned_by WHERE 1=1"
    params = []

    if current_user.is_reviewer() and not current_user.is_admin():
        query += " AND rt.reviewer_id = ?"
        params.append(current_user.id)

    status_filter = request.args.get('status', '')
    if status_filter:
        query += " AND rt.status = ?"
        params.append(status_filter)

    query += " ORDER BY rt.assigned_at DESC LIMIT 500"
    reviews_data = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('reviews.html', reviews=reviews_data, status_filter=status_filter)


@app.route('/reviews/<int:task_id>', methods=['GET', 'POST'])
@login_required
@role_required('admin', 'reviewer')
def do_review(task_id):
    conn = get_db()
    task = conn.execute(
        "SELECT rt.*, c.sample_id, s.content, c.scheme_id, ls.name as scheme_name, ls.version as scheme_version "
        "FROM review_tasks rt JOIN conflicts c ON c.id = rt.conflict_id "
        "JOIN samples s ON s.id = c.sample_id "
        "LEFT JOIN label_schemes ls ON ls.id = c.scheme_id WHERE rt.id = ?",
        (task_id,)
    ).fetchone()
    if not task:
        conn.close()
        flash('复核任务不存在', 'error')
        return redirect(url_for('reviews'))

    if current_user.is_reviewer() and task['reviewer_id'] != current_user.id:
        conn.close()
        flash('你没有权限复核该任务', 'error')
        return redirect(url_for('reviews'))

    conflict_annotations = conn.execute(
        "SELECT a.*, u.display_name as annotator_name FROM conflict_annotations ca "
        "JOIN annotations a ON a.id = ca.annotation_id "
        "JOIN users u ON u.id = a.annotator_id WHERE ca.conflict_id = ?",
        (task['conflict_id'],)
    ).fetchall()

    labels = conn.execute(
        "SELECT * FROM labels WHERE scheme_id = ? ORDER BY label_key", (task['scheme_id'],)
    ).fetchall()

    if request.method == 'POST':
        decision_label_id = request.form.get('label_id', type=int)
        reviewer_comment = request.form.get('comment', '').strip()

        if not decision_label_id:
            conn.close()
            flash('请选择最终标签', 'error')
            return redirect(url_for('do_review', task_id=task_id))

        label = conn.execute("SELECT * FROM labels WHERE id = ? AND scheme_id = ?",
                             (decision_label_id, task['scheme_id'])).fetchone()
        if not label:
            conn.close()
            flash('标签无效', 'error')
            return redirect(url_for('do_review', task_id=task_id))

        conn.execute(
            "UPDATE review_tasks SET status = 'reviewed', reviewer_comment = ?, reviewed_at = CURRENT_TIMESTAMP, "
            "decision_label_id = ?, decision_label_key = ?, decision_label_text = ? WHERE id = ?",
            (reviewer_comment, label['id'], label['label_key'], label['label_text'], task_id)
        )
        conn.execute(
            "UPDATE conflicts SET status = 'resolved', resolved_at = CURRENT_TIMESTAMP, "
            "final_label_id = ?, final_label_key = ?, final_label_text = ?, "
            "resolved_by = ?, resolution_note = ? WHERE id = ?",
            (label['id'], label['label_key'], label['label_text'], current_user.id, reviewer_comment, task['conflict_id'])
        )
        log_revision('conflict', task['conflict_id'], 'resolve',
                     new_value=f"最终标签: {label['label_text']}",
                     user_id=current_user.id, comment=reviewer_comment or "复核完成",
                     conn=conn)
        log_revision('review_task', task_id, 'complete',
                     new_value=f"决策: {label['label_text']}",
                     user_id=current_user.id, comment=reviewer_comment,
                     conn=conn)
        conn.commit()
        conn.close()
        flash('复核完成', 'success')
        return redirect(url_for('reviews'))

    conn.close()
    return render_template('review_do.html', task=task, conflict_annotations=conflict_annotations, labels=labels)


# ============ 用户管理 ============

@app.route('/users')
@login_required
@role_required('admin')
def users():
    conn = get_db()
    users_data = conn.execute(
        "SELECT u.*, "
        "(SELECT COUNT(*) FROM annotations a WHERE a.annotator_id = u.id) as annotation_count, "
        "(SELECT COUNT(*) FROM review_tasks rt WHERE rt.reviewer_id = u.id) as review_count "
        "FROM users u ORDER BY u.role, u.username"
    ).fetchall()
    conn.close()
    return render_template('users.html', users=users_data)


@app.route('/users/new', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def new_user():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        role = request.form.get('role', 'annotator')
        display_name = request.form.get('display_name', '').strip()

        if not username or not password:
            flash('用户名和密码不能为空', 'error')
            return redirect(url_for('new_user'))
        if role not in ('admin', 'annotator', 'reviewer'):
            flash('角色无效', 'error')
            return redirect(url_for('new_user'))

        from werkzeug.security import generate_password_hash
        conn = get_db()
        try:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
                (username, generate_password_hash(password), role, display_name or username)
            )
            log_revision('user', cursor.lastrowid, 'create',
                         new_value=f"{username} ({role})", user_id=current_user.id,
                         conn=conn)
            conn.commit()
            flash('用户创建成功', 'success')
            conn.close()
            return redirect(url_for('users'))
        except Exception as e:
            conn.close()
            flash(f'创建失败: {e}', 'error')
            return redirect(url_for('new_user'))

    return render_template('user_edit.html', user=None)


@app.route('/users/<int:user_id>/edit', methods=['GET', 'POST'])
@login_required
@role_required('admin')
def edit_user(user_id):
    conn = get_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    if not user:
        conn.close()
        flash('用户不存在', 'error')
        return redirect(url_for('users'))

    if request.method == 'POST':
        display_name = request.form.get('display_name', '').strip()
        role = request.form.get('role', user['role'])
        password = request.form.get('password', '')

        if role not in ('admin', 'annotator', 'reviewer'):
            conn.close()
            flash('角色无效', 'error')
            return redirect(url_for('edit_user', user_id=user_id))

        old_value = f"{user['display_name']} ({user['role']})"
        if password:
            from werkzeug.security import generate_password_hash
            conn.execute(
                "UPDATE users SET display_name=?, role=?, password_hash=? WHERE id=?",
                (display_name or user['username'], role, generate_password_hash(password), user_id)
            )
        else:
            conn.execute(
                "UPDATE users SET display_name=?, role=? WHERE id=?",
                (display_name or user['username'], role, user_id)
            )
        log_revision('user', user_id, 'update',
                     old_value=old_value,
                     new_value=f"{display_name or user['username']} ({role})",
                     user_id=current_user.id,
                     conn=conn)
        conn.commit()
        conn.close()
        flash('用户更新成功', 'success')
        return redirect(url_for('users'))

    conn.close()
    return render_template('user_edit.html', user=user)


# ============ 数据导出 ============

@app.route('/export')
@login_required
@role_required('admin')
def export_page():
    conn = get_db()
    schemes = conn.execute(
        "SELECT ls.*, (SELECT COUNT(*) FROM samples s WHERE s.scheme_id = ls.id) as sample_count "
        "FROM label_schemes ls ORDER BY ls.is_active DESC, ls.name, ls.version DESC"
    ).fetchall()
    conn.close()
    return render_template('export.html', schemes=schemes)


@app.route('/export/evidence', methods=['POST'])
@login_required
@role_required('admin')
def export_evidence():
    scheme_id = request.form.get('scheme_id', type=int)
    if not scheme_id:
        flash('请选择标签方案', 'error')
        return redirect(url_for('export_page'))

    conn = get_db()
    scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (scheme_id,)).fetchone()
    if not scheme:
        conn.close()
        flash('方案不存在', 'error')
        return redirect(url_for('export_page'))

    labels = conn.execute("SELECT * FROM labels WHERE scheme_id = ? ORDER BY label_key", (scheme_id,)).fetchall()

    samples = conn.execute(
        "SELECT s.* FROM samples s WHERE s.scheme_id = ? ORDER BY s.sample_id",
        (scheme_id,)
    ).fetchall()

    output = io.StringIO()
    writer = csv.writer(output)

    writer.writerow(['# 文本标注复核证据导出'])
    writer.writerow([f'# 方案: {scheme["name"]} v{scheme["version"]}'])
    writer.writerow([f'# 导出时间: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'])
    writer.writerow([f'# 操作人: {current_user.display_name} ({current_user.username})'])
    writer.writerow([])
    writer.writerow(['## 标签列表'])
    writer.writerow(['标签键', '标签文本', '描述', '颜色'])
    for lbl in labels:
        writer.writerow([lbl['label_key'], lbl['label_text'], lbl['description'] or '', lbl['color']])
    writer.writerow([])
    writer.writerow(['## 标注与复核证据'])
    writer.writerow([
        '样本编号', '样本内容', '标注员', '标注标签', '标注备注',
        '是否冲突', '复核员', '最终标签', '复核意见', '状态', '标注时间', '复核时间'
    ])

    for sample in samples:
        anns = conn.execute(
            "SELECT a.*, u.display_name as annotator_name FROM annotations a "
            "JOIN users u ON u.id = a.annotator_id "
            "WHERE a.sample_id = ? AND a.scheme_id = ? AND a.is_unknown_label = 0 ORDER BY a.created_at",
            (sample['id'], scheme_id)
        ).fetchall()

        conflict = conn.execute(
            "SELECT c.*, u.display_name as resolver_name FROM conflicts c "
            "LEFT JOIN users u ON u.id = c.resolved_by "
            "WHERE c.sample_id = ? AND c.scheme_id = ? ORDER BY c.detected_at DESC LIMIT 1",
            (sample['id'], scheme_id)
        ).fetchone()

        review_task = None
        if conflict:
            review_task = conn.execute(
                "SELECT rt.*, u.display_name as reviewer_name FROM review_tasks rt "
                "LEFT JOIN users u ON u.id = rt.reviewer_id "
                "WHERE rt.conflict_id = ? AND rt.status = 'reviewed' ORDER BY rt.reviewed_at DESC LIMIT 1",
                (conflict['id'],)
            ).fetchone()

        if not anns:
            writer.writerow([
                sample['sample_id'], sample['content'], '', '', '',
                '否' if not conflict else '是',
                review_task['reviewer_name'] if review_task else '',
                conflict['final_label_text'] if conflict and conflict['final_label_text'] else '',
                conflict['resolution_note'] if conflict else '',
                conflict['status'] if conflict else '无标注',
                '', ''
            ])
        else:
            for ann in anns:
                writer.writerow([
                    sample['sample_id'], sample['content'],
                    ann['annotator_name'],
                    ann['label_text'] + (' (未知标签!)' if ann['is_unknown_label'] else ''),
                    ann['comment'] or '',
                    '否' if not conflict else '是',
                    review_task['reviewer_name'] if review_task else '',
                    conflict['final_label_text'] if conflict and conflict['final_label_text'] else '',
                    conflict['resolution_note'] if conflict else '',
                    conflict['status'] if conflict else ('一致' if len(set(a['label_key'] for a in anns)) == 1 else '有分歧'),
                    ann['created_at'],
                    review_task['reviewed_at'] if review_task else ''
                ])

    writer.writerow([])
    writer.writerow(['## 修订历史'])
    writer.writerow(['时间', '实体类型', '实体ID', '操作', '旧值', '新值', '操作人', '备注'])
    history = conn.execute(
        "SELECT rh.*, u.display_name as user_name FROM revision_history rh "
        "LEFT JOIN users u ON u.id = rh.user_id ORDER BY rh.created_at DESC LIMIT 1000"
    ).fetchall()
    for h in history:
        writer.writerow([
            h['created_at'], h['entity_type'], h['entity_id'], h['action'],
            h['old_value'] or '', h['new_value'] or '', h['user_name'] or '', h['comment'] or ''
        ])

    conn.close()

    output.seek(0)
    filename = f"evidence_{scheme['name'].replace(' ', '_')}_v{scheme['version']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"

    return Response(
        output.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


@app.route('/export/json', methods=['POST'])
@login_required
@role_required('admin')
def export_json():
    scheme_id = request.form.get('scheme_id', type=int)
    if not scheme_id:
        flash('请选择标签方案', 'error')
        return redirect(url_for('export_page'))

    conn = get_db()
    scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (scheme_id,)).fetchone()
    if not scheme:
        conn.close()
        flash('方案不存在', 'error')
        return redirect(url_for('export_page'))

    labels = [dict(row) for row in conn.execute(
        "SELECT * FROM labels WHERE scheme_id = ? ORDER BY label_key", (scheme_id,)
    ).fetchall()]

    samples_data = []
    samples = conn.execute("SELECT * FROM samples WHERE scheme_id = ? ORDER BY sample_id", (scheme_id,)).fetchall()
    for s in samples:
        anns = conn.execute(
            "SELECT a.*, u.display_name as annotator_name, u.username as annotator FROM annotations a "
            "JOIN users u ON u.id = a.annotator_id "
            "WHERE a.sample_id = ? AND a.is_unknown_label = 0 ORDER BY a.created_at",
            (s['id'],)
        ).fetchall()
        conflict = conn.execute(
            "SELECT c.*, u.display_name as resolver_name FROM conflicts c "
            "LEFT JOIN users u ON u.id = c.resolved_by "
            "WHERE c.sample_id = ? AND c.scheme_id = ? ORDER BY c.detected_at DESC LIMIT 1",
            (s['id'], scheme_id)
        ).fetchone()
        reviews = []
        if conflict:
            reviews = [dict(row) for row in conn.execute(
                "SELECT rt.*, u.display_name as reviewer_name FROM review_tasks rt "
                "LEFT JOIN users u ON u.id = rt.reviewer_id WHERE rt.conflict_id = ? ORDER BY rt.assigned_at",
                (conflict['id'],)
            ).fetchall()]

        samples_data.append({
            'sample_id': s['sample_id'],
            'content': s['content'],
            'metadata': json.loads(s['metadata']) if s['metadata'] else None,
            'annotations': [dict(a) for a in anns],
            'conflict': dict(conflict) if conflict else None,
            'reviews': reviews
        })

    result = {
        'exported_at': datetime.now().isoformat(),
        'exported_by': {'id': current_user.id, 'username': current_user.username, 'display_name': current_user.display_name},
        'scheme': dict(scheme),
        'labels': labels,
        'samples': samples_data
    }
    conn.close()

    filename = f"evidence_{scheme['name'].replace(' ', '_')}_v{scheme['version']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    return Response(
        json.dumps(result, ensure_ascii=False, indent=2),
        mimetype='application/json; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )


# ============ 修订历史 ============

@app.route('/history')
@login_required
@role_required('admin')
def history():
    conn = get_db()
    query = "SELECT rh.*, u.display_name as user_name FROM revision_history rh "
    query += "LEFT JOIN users u ON u.id = rh.user_id WHERE 1=1"
    params = []

    entity_type = request.args.get('entity_type', '')
    if entity_type:
        query += " AND rh.entity_type = ?"
        params.append(entity_type)

    query += " ORDER BY rh.created_at DESC LIMIT 500"
    history_data = conn.execute(query, params).fetchall()
    conn.close()
    return render_template('history.html', history=history_data, entity_type=entity_type)


# ============ 批次管理 ============

@app.route('/batches')
@login_required
@role_required('admin')
def batches_list():
    conn = get_db()
    batch_type = request.args.get('type', '')
    status = request.args.get('status', '')
    batches = list_batches(batch_type=batch_type or None, status=status or None, conn=conn)
    conn.close()
    return render_template('batches.html', batches=batches,
                           batch_type=batch_type, status=status)


@app.route('/batches/<int:batch_id>')
@login_required
@role_required('admin')
def batch_detail(batch_id):
    conn = get_db()
    batch = get_batch(batch_id, conn=conn)
    if not batch:
        conn.close()
        flash('批次不存在', 'error')
        return redirect(url_for('batches_list'))

    sample_records = []
    annotation_records = []
    conflict_records = []
    review_task_records = []

    if batch['batch_type'] == 'sample':
        sample_records = get_batch_sample_records(batch_id, conn=conn)
    elif batch['batch_type'] == 'annotation':
        annotation_records = get_batch_annotation_records(batch_id, conn=conn)
        conflict_records = get_batch_conflict_records(batch_id, conn=conn)
        review_task_records = get_batch_review_task_records(batch_id, conn=conn)

    creator = None
    reverter = None
    if batch['created_by']:
        row = conn.execute("SELECT display_name, username FROM users WHERE id = ?",
                          (batch['created_by'],)).fetchone()
        if row:
            creator = row['display_name'] or row['username']
    if batch['reverted_by']:
        row = conn.execute("SELECT display_name, username FROM users WHERE id = ?",
                          (batch['reverted_by'],)).fetchone()
        if row:
            reverter = row['display_name'] or row['username']

    conn.close()
    return render_template('batch_detail.html',
                           batch=batch,
                           sample_records=sample_records,
                           annotation_records=annotation_records,
                           conflict_records=conflict_records,
                           review_task_records=review_task_records,
                           creator=creator,
                           reverter=reverter)


@app.route('/samples/import/preview', methods=['POST'])
@login_required
@role_required('admin', 'annotator')
def preview_samples_import():
    """样本导入预演：不真正入库，返回预演结果并创建 preview 状态的批次。"""
    conn = get_db()
    schemes = conn.execute("SELECT * FROM label_schemes WHERE is_active = 1 ORDER BY name, version DESC").fetchall()

    scheme_id = request.form.get('scheme_id')
    file = request.files.get('file')

    if not scheme_id or not file:
        conn.close()
        flash('请选择标签方案和上传文件', 'error')
        return redirect(url_for('import_samples'))

    scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (int(scheme_id),)).fetchone()
    if not scheme:
        conn.close()
        flash('标签方案不存在', 'error')
        return redirect(url_for('import_samples'))

    file_content = file.read()
    file_size = len(file_content)
    file_name = file.filename or 'samples.csv'
    import hashlib
    file_hash = hashlib.md5(file_content).hexdigest()

    try:
        content = file_content.decode('utf-8-sig')
    except UnicodeDecodeError:
        conn.close()
        flash('文件编码错误，请使用 UTF-8 编码', 'error')
        return redirect(url_for('import_samples'))

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    total_rows = len(rows)

    batch_id = create_batch(
        batch_type='sample',
        scheme_id=scheme['id'],
        file_name=file_name,
        file_hash=file_hash,
        file_size=file_size,
        created_by=current_user.id,
        conn=conn
    )

    new_count = 0
    duplicates = []
    errors = []
    old_scheme_residue = 0
    seen_sample_ids = set()

    for i, row in enumerate(rows, start=2):
        sample_id = (row.get('sample_id') or row.get('id') or '').strip()
        text = (row.get('content') or row.get('text') or row.get('sample') or '').strip()
        metadata = {k: v for k, v in row.items() if k not in ('sample_id', 'id', 'content', 'text', 'sample')}

        if not sample_id or not text:
            err = f"第{i}行: 缺少样本编号或内容"
            errors.append(err)
            batch_add_sample_record(
                batch_id=batch_id, row_number=i, sample_code=sample_id or '',
                action='skip_error', error_reason='缺少样本编号或内容',
                new_content=text, conn=conn
            )
            continue

        if sample_id in seen_sample_ids:
            duplicates.append(sample_id)
            batch_add_sample_record(
                batch_id=batch_id, row_number=i, sample_code=sample_id,
                action='skip_duplicate',
                old_content='',
                new_content=text,
                error_reason='本批次内重复的样本编号',
                conn=conn
            )
            continue

        existing = conn.execute("SELECT id, scheme_id FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
        if existing:
            duplicates.append(sample_id)
            batch_add_sample_record(
                batch_id=batch_id, row_number=i, sample_code=sample_id,
                action='skip_duplicate', sample_db_id=existing['id'],
                old_content='',
                new_content=text,
                error_reason=f'样本编号已存在于方案ID {existing["scheme_id"]}',
                conn=conn
            )
            if existing['scheme_id'] and existing['scheme_id'] != scheme['id']:
                old_scheme_residue += 1
            continue

        seen_sample_ids.add(sample_id)
        batch_add_sample_record(
            batch_id=batch_id, row_number=i, sample_code=sample_id,
            action='create',
            new_content=text,
            metadata=json.dumps(metadata, ensure_ascii=False) if metadata else None,
            conn=conn
        )
        new_count += 1

    stats = {
        'total_rows': total_rows,
        'new_count': new_count,
        'skip_duplicate_count': len(duplicates),
        'skip_error_count': len(errors),
        'old_scheme_residue_count': old_scheme_residue,
    }
    update_batch_stats(batch_id, stats, conn=conn)

    preview_data = json.dumps({
        'scheme': {'id': scheme['id'], 'name': scheme['name'], 'version': scheme['version']},
        'total_rows': total_rows,
        'new_count': new_count,
        'duplicates': duplicates,
        'errors': errors,
        'old_scheme_residue': old_scheme_residue,
    }, ensure_ascii=False)
    conn.execute("UPDATE import_batches SET preview_data = ? WHERE id = ?",
                 (preview_data, batch_id))

    conn.commit()
    conn.close()

    return redirect(url_for('batch_detail', batch_id=batch_id))


@app.route('/annotations/import/preview', methods=['POST'])
@login_required
@role_required('admin', 'annotator')
def preview_annotations_import():
    """标注导入预演：不真正入库，返回预演结果并创建 preview 状态的批次。"""
    conn = get_db()

    scheme_id = request.form.get('scheme_id')
    annotator_id = request.form.get('annotator_id', str(current_user.id))
    file = request.files.get('file')

    if not scheme_id or not file:
        conn.close()
        flash('请选择标签方案和上传文件', 'error')
        return redirect(url_for('import_annotations'))

    scheme = conn.execute("SELECT * FROM label_schemes WHERE id = ?", (int(scheme_id),)).fetchone()
    if not scheme:
        conn.close()
        flash('标签方案不存在', 'error')
        return redirect(url_for('import_annotations'))

    if current_user.is_annotator() and int(annotator_id) != current_user.id:
        conn.close()
        flash('标注员只能导入自己的标注结果', 'error')
        return redirect(url_for('import_annotations'))

    scheme_labels = conn.execute(
        "SELECT id, label_key, label_text FROM labels WHERE scheme_id = ?", (scheme['id'],)
    ).fetchall()
    label_map = {}
    for lbl in scheme_labels:
        label_map[lbl['label_key']] = lbl
        label_map[lbl['label_text']] = lbl

    file_content = file.read()
    file_size = len(file_content)
    file_name = file.filename or 'annotations.csv'
    import hashlib
    file_hash = hashlib.md5(file_content).hexdigest()

    try:
        content = file_content.decode('utf-8-sig')
    except UnicodeDecodeError:
        conn.close()
        flash('文件编码错误，请使用 UTF-8 编码', 'error')
        return redirect(url_for('import_annotations'))

    reader = csv.DictReader(io.StringIO(content))
    rows = list(reader)
    total_rows = len(rows)

    batch_id = create_batch(
        batch_type='annotation',
        scheme_id=scheme['id'],
        file_name=file_name,
        file_hash=file_hash,
        file_size=file_size,
        created_by=current_user.id,
        conn=conn
    )

    new_count = 0
    update_count = 0
    unknown_labels = []
    missing_samples = []
    errors = []
    potential_conflicts = []
    old_scheme_residue = 0

    for i, row in enumerate(rows, start=2):
        sample_id = (row.get('sample_id') or row.get('id') or '').strip()
        label_key = (row.get('label') or row.get('label_key') or '').strip()
        label_text = (row.get('label_text') or '').strip()
        comment = (row.get('comment') or row.get('note') or '').strip()

        if not sample_id or not label_key:
            err = f"第{i}行: 缺少样本编号或标签"
            errors.append(err)
            batch_add_annotation_record(
                batch_id=batch_id, row_number=i, sample_code=sample_id or '',
                action='skip_error', annotator_id=int(annotator_id),
                error_reason='缺少样本编号或标签',
                new_label_key=label_key,
                conn=conn
            )
            continue

        sample = conn.execute("SELECT id, scheme_id FROM samples WHERE sample_id = ?", (sample_id,)).fetchone()
        if not sample:
            missing_samples.append((sample_id, i))
            batch_add_annotation_record(
                batch_id=batch_id, row_number=i, sample_code=sample_id,
                action='skip_missing_sample', annotator_id=int(annotator_id),
                error_reason='样本不存在',
                new_label_key=label_key,
                conn=conn
            )
            continue

        if sample['scheme_id'] and sample['scheme_id'] != scheme['id']:
            old_scheme_residue += 1

        matched_label = label_map.get(label_key) or label_map.get(label_text)
        if not matched_label:
            unknown_labels.append((sample_id, label_key, i))
            batch_add_annotation_record(
                batch_id=batch_id, row_number=i, sample_code=sample_id,
                sample_db_id=sample['id'], action='skip_unknown_label',
                annotator_id=int(annotator_id),
                new_label_key=label_key,
                error_reason=f'未知标签: {label_key}',
                conn=conn
            )
            continue

        existing = conn.execute(
            "SELECT * FROM annotations WHERE sample_id = ? AND annotator_id = ? AND scheme_id = ?",
            (sample['id'], int(annotator_id), scheme['id'])
        ).fetchone()

        if existing:
            if existing['label_key'] == matched_label['label_key'] and existing['comment'] == comment:
                batch_add_annotation_record(
                    batch_id=batch_id, row_number=i, sample_code=sample_id,
                    sample_db_id=sample['id'], annotation_id=existing['id'],
                    action='skip_duplicate', annotator_id=int(annotator_id),
                    old_label_id=existing['label_id'], old_label_key=existing['label_key'],
                    old_label_text=existing['label_text'], old_comment=existing['comment'],
                    new_label_id=matched_label['id'], new_label_key=matched_label['label_key'],
                    new_label_text=matched_label['label_text'], new_comment=comment,
                    error_reason='标注完全相同，无需更新',
                    conn=conn
                )
                continue

            update_count += 1
            batch_add_annotation_record(
                batch_id=batch_id, row_number=i, sample_code=sample_id,
                sample_db_id=sample['id'], annotation_id=existing['id'],
                action='update', annotator_id=int(annotator_id),
                old_label_id=existing['label_id'], old_label_key=existing['label_key'],
                old_label_text=existing['label_text'], old_comment=existing['comment'],
                new_label_id=matched_label['id'], new_label_key=matched_label['label_key'],
                new_label_text=matched_label['label_text'], new_comment=comment,
                conn=conn
            )
        else:
            new_count += 1
            batch_add_annotation_record(
                batch_id=batch_id, row_number=i, sample_code=sample_id,
                sample_db_id=sample['id'], action='create',
                annotator_id=int(annotator_id),
                new_label_id=matched_label['id'], new_label_key=matched_label['label_key'],
                new_label_text=matched_label['label_text'], new_comment=comment,
                conn=conn
            )

        existing_other_anns = conn.execute(
            "SELECT DISTINCT label_key FROM annotations "
            "WHERE sample_id = ? AND scheme_id = ? AND annotator_id != ? AND is_unknown_label = 0",
            (sample['id'], scheme['id'], int(annotator_id))
        ).fetchall()
        other_labels = [r['label_key'] for r in existing_other_anns if r['label_key']]
        if other_labels and matched_label['label_key'] not in other_labels:
            potential_conflicts.append(sample_id)

    stats = {
        'total_rows': total_rows,
        'new_count': new_count,
        'update_count': update_count,
        'skip_duplicate_count': sum(1 for r in rows if False),
        'skip_error_count': len(errors),
        'skip_unknown_label_count': len(unknown_labels),
        'skip_missing_sample_count': len(missing_samples),
        'conflict_created_count': len(potential_conflicts),
        'old_scheme_residue_count': old_scheme_residue,
    }
    update_batch_stats(batch_id, stats, conn=conn)

    preview_data = json.dumps({
        'scheme': {'id': scheme['id'], 'name': scheme['name'], 'version': scheme['version']},
        'annotator_id': int(annotator_id),
        'total_rows': total_rows,
        'new_count': new_count,
        'update_count': update_count,
        'unknown_labels': unknown_labels,
        'missing_samples': missing_samples,
        'errors': errors,
        'potential_conflicts': potential_conflicts,
        'old_scheme_residue': old_scheme_residue,
    }, ensure_ascii=False)
    conn.execute("UPDATE import_batches SET preview_data = ? WHERE id = ?",
                 (preview_data, batch_id))

    conn.commit()
    conn.close()

    return redirect(url_for('batch_detail', batch_id=batch_id))


@app.route('/batches/<int:batch_id>/confirm', methods=['POST'])
@login_required
@role_required('admin')
def confirm_import_batch(batch_id):
    """确认导入批次：将预演数据真正写入数据库。"""
    conn = get_db()
    batch = get_batch(batch_id, conn=conn)
    if not batch:
        conn.close()
        flash('批次不存在', 'error')
        return redirect(url_for('batches_list'))
    if batch['status'] != 'preview':
        conn.close()
        flash(f'批次状态为 {batch["status"]}，无法确认导入', 'error')
        return redirect(url_for('batch_detail', batch_id=batch_id))

    try:
        if batch['batch_type'] == 'sample':
            _confirm_sample_batch(batch_id, conn=conn)
        elif batch['batch_type'] == 'annotation':
            _confirm_annotation_batch(batch_id, conn=conn)

        confirm_batch(batch_id, conn=conn)
        log_revision('import_batch', batch_id, 'confirm',
                     old_value='preview', new_value='confirmed',
                     user_id=current_user.id,
                     comment=f'确认导入批次 #{batch_id}',
                     conn=conn)

        conn.commit()
        flash('导入确认成功', 'success')
    except Exception as e:
        conn.rollback()
        flash(f'导入确认失败: {e}', 'error')
        return redirect(url_for('batch_detail', batch_id=batch_id))
    finally:
        conn.close()

    return redirect(url_for('batch_detail', batch_id=batch_id))


def _confirm_sample_batch(batch_id, conn=None):
    """确认样本批次：将 create 的样本真正入库。"""
    records = conn.execute(
        "SELECT * FROM batch_sample_records WHERE batch_id = ? AND action = 'create'",
        (batch_id,)
    ).fetchall()

    batch = get_batch(batch_id, conn=conn)

    for rec in records:
        cursor = conn.execute(
            "INSERT INTO samples (sample_id, content, scheme_id, imported_by, metadata) "
            "VALUES (?, ?, ?, ?, ?)",
            (rec['sample_code'], rec['new_content'], batch['scheme_id'],
             batch['created_by'], rec['metadata'])
        )
        sample_id = cursor.lastrowid
        conn.execute(
            "UPDATE batch_sample_records SET sample_db_id = ? WHERE id = ?",
            (sample_id, rec['id'])
        )
        log_revision('sample', sample_id, 'create',
                     new_value=rec['new_content'],
                     user_id=batch['created_by'],
                     comment=f'批次 #{batch_id} 导入样本',
                     conn=conn)
        rev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        batch_link_revision(batch_id, rev_id, conn=conn)


def _confirm_annotation_batch(batch_id, conn=None):
    """确认标注批次：将 create/update 的标注真正写入数据库。"""
    batch = get_batch(batch_id, conn=conn)

    create_records = conn.execute(
        "SELECT * FROM batch_annotation_records WHERE batch_id = ? AND action = 'create'",
        (batch_id,)
    ).fetchall()

    for rec in create_records:
        cursor = conn.execute(
            "INSERT INTO annotations (sample_id, annotator_id, scheme_id, label_id, label_key, "
            "label_text, is_unknown_label, comment) VALUES (?, ?, ?, ?, ?, ?, 0, ?)",
            (rec['sample_db_id'], rec['annotator_id'], batch['scheme_id'],
             rec['new_label_id'], rec['new_label_key'], rec['new_label_text'],
             rec['new_comment'] or '')
        )
        ann_id = cursor.lastrowid
        conn.execute(
            "UPDATE batch_annotation_records SET annotation_id = ? WHERE id = ?",
            (ann_id, rec['id'])
        )
        log_revision('annotation', ann_id, 'create',
                     new_value=rec['new_label_text'],
                     user_id=batch['created_by'],
                     comment=f'批次 #{batch_id} 导入标注',
                     conn=conn)
        rev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        batch_link_revision(batch_id, rev_id, conn=conn)

    update_records = conn.execute(
        "SELECT * FROM batch_annotation_records WHERE batch_id = ? AND action = 'update'",
        (batch_id,)
    ).fetchall()

    for rec in update_records:
        conn.execute(
            "UPDATE annotations SET label_id=?, label_key=?, label_text=?, is_unknown_label=0, "
            "comment=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (rec['new_label_id'], rec['new_label_key'], rec['new_label_text'],
             rec['new_comment'] or '', rec['annotation_id'])
        )
        log_revision('annotation', rec['annotation_id'], 'update',
                     old_value=rec['old_label_text'], new_value=rec['new_label_text'],
                     user_id=batch['created_by'],
                     comment=f'批次 #{batch_id} 更新标注',
                     conn=conn)
        rev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        batch_link_revision(batch_id, rev_id, conn=conn)

    _detect_batch_conflicts(batch_id, conn=conn)


def _detect_batch_conflicts(batch_id, conn=None):
    """检测批次导入后可能产生的冲突，并记录到批次明细。"""
    batch = get_batch(batch_id, conn=conn)
    if not batch or batch['scheme_id'] is None:
        return

    sample_ids = conn.execute(
        "SELECT DISTINCT sample_db_id FROM batch_annotation_records "
        "WHERE batch_id = ? AND action IN ('create', 'update') AND sample_db_id IS NOT NULL",
        (batch_id,)
    ).fetchall()

    created_count = 0
    affected_count = 0

    for s_row in sample_ids:
        sample_id = s_row['sample_db_id']
        if not sample_id:
            continue

        labels = conn.execute(
            "SELECT DISTINCT label_key FROM annotations "
            "WHERE sample_id = ? AND scheme_id = ? AND is_unknown_label = 0 AND label_key IS NOT NULL",
            (sample_id, batch['scheme_id'])
        ).fetchall()
        label_keys = [l['label_key'] for l in labels if l['label_key']]

        if len(set(label_keys)) <= 1:
            continue

        existing_conflict = conn.execute(
            "SELECT * FROM conflicts WHERE sample_id = ? AND scheme_id = ? AND status != 'resolved'",
            (sample_id, batch['scheme_id'])
        ).fetchone()

        if existing_conflict:
            old_status = existing_conflict['status']
            batch_add_conflict_record(
                batch_id=batch_id, conflict_id=existing_conflict['id'],
                sample_db_id=sample_id, action='affected',
                old_status=old_status, new_status=old_status,
                conn=conn
            )
            affected_count += 1
        else:
            cursor = conn.execute(
                "INSERT INTO conflicts (sample_id, scheme_id, status) VALUES (?, ?, 'open')",
                (sample_id, batch['scheme_id'])
            )
            conflict_id = cursor.lastrowid

            anns = conn.execute(
                "SELECT id FROM annotations WHERE sample_id = ? AND scheme_id = ?",
                (sample_id, batch['scheme_id'])
            ).fetchall()
            for a in anns:
                conn.execute(
                    "INSERT INTO conflict_annotations (conflict_id, annotation_id) VALUES (?, ?)",
                    (conflict_id, a['id'])
                )

            batch_add_conflict_record(
                batch_id=batch_id, conflict_id=conflict_id,
                sample_db_id=sample_id, action='created',
                old_status=None, new_status='open',
                conn=conn
            )
            log_revision('conflict', conflict_id, 'detect',
                         user_id=batch['created_by'],
                         comment=f'批次 #{batch_id} 导入后检测到冲突',
                         conn=conn)
            rev_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            batch_link_revision(batch_id, rev_id, conn=conn)
            created_count += 1

    update_batch_stats(batch_id, {
        'conflict_created_count': created_count,
        'conflict_affected_count': affected_count,
    }, conn=conn)


@app.route('/batches/<int:batch_id>/revert', methods=['POST'])
@login_required
@role_required('admin')
def revert_import_batch(batch_id):
    """回滚导入批次。"""
    conn = get_db()
    batch = get_batch(batch_id, conn=conn)
    if not batch:
        conn.close()
        flash('批次不存在', 'error')
        return redirect(url_for('batches_list'))

    if not current_user.is_admin():
        conn.close()
        flash('只有管理员可以回滚批次', 'error')
        return redirect(url_for('batch_detail', batch_id=batch_id))

    revert_note = request.form.get('note', '').strip()

    try:
        success, msg = revert_batch(
            batch_id, reverted_by=current_user.id,
            revert_note=revert_note, conn=conn
        )
        if success:
            conn.commit()
            flash(msg, 'success')
        else:
            conn.rollback()
            flash(msg, 'error')
    except Exception as e:
        conn.rollback()
        flash(f'回滚失败: {e}', 'error')
    finally:
        conn.close()

    return redirect(url_for('batch_detail', batch_id=batch_id))


if __name__ == '__main__':
    init_db()
    print(f"数据库已初始化: {DB_PATH}")
    print("启动服务: http://127.0.0.1:5000")
    print("默认账号: admin/admin123, annotator1/anno123, reviewer1/review123")
    app.run(host='127.0.0.1', port=5000, debug=False)
