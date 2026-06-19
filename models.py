import sqlite3
import os
import json
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'app.db')


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute('''CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('admin', 'annotator', 'reviewer')),
        display_name TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS label_schemes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        description TEXT,
        is_active INTEGER DEFAULT 1,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        created_by INTEGER REFERENCES users(id),
        UNIQUE(name, version)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS labels (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        scheme_id INTEGER NOT NULL REFERENCES label_schemes(id) ON DELETE CASCADE,
        label_key TEXT NOT NULL,
        label_text TEXT NOT NULL,
        color TEXT DEFAULT '#3b82f6',
        description TEXT,
        UNIQUE(scheme_id, label_key)
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sample_id TEXT UNIQUE NOT NULL,
        content TEXT NOT NULL,
        scheme_id INTEGER REFERENCES label_schemes(id),
        imported_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        imported_by INTEGER REFERENCES users(id),
        metadata TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
        annotator_id INTEGER NOT NULL REFERENCES users(id),
        scheme_id INTEGER NOT NULL REFERENCES label_schemes(id),
        label_id INTEGER REFERENCES labels(id),
        label_key TEXT,
        label_text TEXT,
        is_unknown_label INTEGER DEFAULT 0,
        comment TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS conflicts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        sample_id INTEGER NOT NULL REFERENCES samples(id) ON DELETE CASCADE,
        scheme_id INTEGER NOT NULL REFERENCES label_schemes(id),
        status TEXT DEFAULT 'open' CHECK (status IN ('open', 'assigned', 'resolved', 'closed')),
        detected_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        resolved_at TIMESTAMP,
        final_label_id INTEGER REFERENCES labels(id),
        final_label_key TEXT,
        final_label_text TEXT,
        resolved_by INTEGER REFERENCES users(id),
        resolution_note TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS conflict_annotations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conflict_id INTEGER NOT NULL REFERENCES conflicts(id) ON DELETE CASCADE,
        annotation_id INTEGER NOT NULL REFERENCES annotations(id) ON DELETE CASCADE
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS review_tasks (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        conflict_id INTEGER NOT NULL REFERENCES conflicts(id) ON DELETE CASCADE,
        reviewer_id INTEGER NOT NULL REFERENCES users(id),
        status TEXT DEFAULT 'pending' CHECK (status IN ('pending', 'in_progress', 'reviewed', 'reassigned')),
        assigned_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        assigned_by INTEGER REFERENCES users(id),
        reviewer_comment TEXT,
        reviewed_at TIMESTAMP,
        decision_label_id INTEGER REFERENCES labels(id),
        decision_label_key TEXT,
        decision_label_text TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS revision_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_type TEXT NOT NULL,
        entity_id INTEGER NOT NULL,
        action TEXT NOT NULL,
        old_value TEXT,
        new_value TEXT,
        user_id INTEGER REFERENCES users(id),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        comment TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS import_batches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_type TEXT NOT NULL CHECK (batch_type IN ('sample', 'annotation')),
        scheme_id INTEGER REFERENCES label_schemes(id),
        file_name TEXT NOT NULL,
        file_hash TEXT,
        file_size INTEGER,
        total_rows INTEGER DEFAULT 0,
        new_count INTEGER DEFAULT 0,
        update_count INTEGER DEFAULT 0,
        skip_duplicate_count INTEGER DEFAULT 0,
        skip_error_count INTEGER DEFAULT 0,
        skip_unknown_label_count INTEGER DEFAULT 0,
        skip_missing_sample_count INTEGER DEFAULT 0,
        conflict_created_count INTEGER DEFAULT 0,
        conflict_affected_count INTEGER DEFAULT 0,
        review_task_created_count INTEGER DEFAULT 0,
        review_task_affected_count INTEGER DEFAULT 0,
        old_scheme_residue_count INTEGER DEFAULT 0,
        status TEXT DEFAULT 'preview' CHECK (status IN ('preview', 'confirmed', 'reverted')),
        created_by INTEGER REFERENCES users(id),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        confirmed_at TIMESTAMP,
        reverted_at TIMESTAMP,
        reverted_by INTEGER REFERENCES users(id),
        revert_note TEXT,
        preview_data TEXT,
        config_snapshot TEXT
    )''')

    _migrate_import_batches_columns(c)

    c.execute('''CREATE TABLE IF NOT EXISTS batch_sample_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
        row_number INTEGER,
        sample_code TEXT,
        sample_db_id INTEGER REFERENCES samples(id),
        action TEXT NOT NULL CHECK (action IN ('create', 'skip_duplicate', 'skip_error')),
        old_content TEXT,
        new_content TEXT,
        metadata TEXT,
        error_reason TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS batch_annotation_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
        row_number INTEGER,
        sample_code TEXT,
        sample_db_id INTEGER REFERENCES samples(id),
        annotation_id INTEGER REFERENCES annotations(id),
        annotator_id INTEGER REFERENCES users(id),
        action TEXT NOT NULL CHECK (action IN ('create', 'update', 'skip_duplicate', 'skip_error', 'skip_unknown_label', 'skip_missing_sample')),
        old_label_key TEXT,
        old_label_text TEXT,
        old_label_id INTEGER,
        old_comment TEXT,
        new_label_key TEXT,
        new_label_text TEXT,
        new_label_id INTEGER,
        new_comment TEXT,
        error_reason TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS batch_conflict_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
        conflict_id INTEGER REFERENCES conflicts(id),
        sample_db_id INTEGER REFERENCES samples(id),
        action TEXT NOT NULL CHECK (action IN ('created', 'affected')),
        old_status TEXT,
        new_status TEXT
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS batch_review_task_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
        review_task_id INTEGER REFERENCES review_tasks(id),
        conflict_id INTEGER REFERENCES conflicts(id),
        action TEXT NOT NULL CHECK (action IN ('created', 'affected')),
        old_status TEXT,
        new_status TEXT,
        old_reviewer_id INTEGER,
        new_reviewer_id INTEGER
    )''')

    c.execute('''CREATE TABLE IF NOT EXISTS batch_revision_links (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        batch_id INTEGER NOT NULL REFERENCES import_batches(id) ON DELETE CASCADE,
        revision_id INTEGER NOT NULL REFERENCES revision_history(id) ON DELETE CASCADE
    )''')

    c.execute('''CREATE INDEX IF NOT EXISTS idx_annotations_sample ON annotations(sample_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_annotations_annotator ON annotations(annotator_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_conflicts_sample ON conflicts(sample_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_review_tasks_reviewer ON review_tasks(reviewer_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_revision_entity ON revision_history(entity_type, entity_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_import_batches_status ON import_batches(status)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_import_batches_type ON import_batches(batch_type)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_batch_sample_records_batch ON batch_sample_records(batch_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_batch_annotation_records_batch ON batch_annotation_records(batch_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_batch_conflict_records_batch ON batch_conflict_records(batch_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_batch_review_task_records_batch ON batch_review_task_records(batch_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_batch_revision_links_batch ON batch_revision_links(batch_id)''')

    _migrate_scheme_release_tables(c)
    conn.commit()

    admin_exists = c.execute("SELECT COUNT(*) FROM users WHERE username = 'admin'").fetchone()[0]
    if admin_exists == 0:
        c.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
                  ('admin', generate_password_hash('admin123'), 'admin', '系统管理员'))
        c.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
                  ('annotator1', generate_password_hash('anno123'), 'annotator', '标注员甲'))
        c.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
                  ('annotator2', generate_password_hash('anno123'), 'annotator', '标注员乙'))
        c.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
                  ('reviewer1', generate_password_hash('review123'), 'reviewer', '复核员甲'))
        c.execute("INSERT INTO users (username, password_hash, role, display_name) VALUES (?, ?, ?, ?)",
                  ('reviewer2', generate_password_hash('review123'), 'reviewer', '复核员乙'))
        conn.commit()

    conn.close()


def _migrate_import_batches_columns(c):
    """迁移：确保 import_batches 表有最新字段。"""
    existing_cols = [row[1] for row in c.execute("PRAGMA table_info(import_batches)").fetchall()]
    if 'config_snapshot' not in existing_cols:
        c.execute("ALTER TABLE import_batches ADD COLUMN config_snapshot TEXT")
    if 'review_task_affected_count' not in existing_cols:
        c.execute("ALTER TABLE import_batches ADD COLUMN review_task_affected_count INTEGER DEFAULT 0")


class User(UserMixin):
    def __init__(self, id, username, role, display_name=None):
        self.id = id
        self.username = username
        self.role = role
        self.display_name = display_name or username

    def is_admin(self):
        return self.role == 'admin'

    def is_annotator(self):
        return self.role == 'annotator'

    def is_reviewer(self):
        return self.role == 'reviewer'

    @staticmethod
    def get(user_id):
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        conn.close()
        if row:
            return User(row['id'], row['username'], row['role'], row['display_name'])
        return None

    @staticmethod
    def authenticate(username, password):
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        conn.close()
        if row and check_password_hash(row['password_hash'], password):
            return User(row['id'], row['username'], row['role'], row['display_name'])
        return None


def log_revision(entity_type, entity_id, action, old_value=None, new_value=None, user_id=None, comment=None, conn=None):
    """
    记录审计日志。

    关键约定：
    - conn=None（默认）：内部新建连接，写完 commit+close，适用于"单次写、无外层事务"的场景
    - conn 传入：复用该连接写日志，**不执行 commit 也不 close**，调用者需在最后统一 commit
      用于事务内有多次业务写入+多次审计写入的场景，避免同一请求多连接导致 SQLite 锁
    """
    sql = ("INSERT INTO revision_history (entity_type, entity_id, action, old_value, new_value, user_id, comment) "
           "VALUES (?, ?, ?, ?, ?, ?, ?)")
    params = (entity_type, entity_id, action,
              str(old_value) if old_value is not None else None,
              str(new_value) if new_value is not None else None,
              user_id, comment)

    if conn is not None:
        conn.execute(sql, params)
        return

    c = get_db()
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def create_batch(batch_type, scheme_id, file_name, file_hash=None, file_size=None,
                 created_by=None, preview_data=None, config_snapshot=None, conn=None):
    """创建导入批次记录。

    返回批次 ID。
    """
    sql = ("INSERT INTO import_batches "
           "(batch_type, scheme_id, file_name, file_hash, file_size, created_by, preview_data, config_snapshot, status) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'preview')")
    params = (batch_type, scheme_id, file_name, file_hash, file_size, created_by, preview_data, config_snapshot)

    def _do_insert(c):
        cursor = c.execute(sql, params)
        return cursor.lastrowid

    if conn is not None:
        return _do_insert(conn)

    c = get_db()
    try:
        batch_id = _do_insert(c)
        c.commit()
        return batch_id
    finally:
        c.close()


def batch_add_sample_record(batch_id, row_number, sample_code, action,
                           sample_db_id=None, old_content=None, new_content=None,
                           metadata=None, error_reason=None, conn=None):
    """添加样本批次明细。"""
    sql = ("INSERT INTO batch_sample_records "
           "(batch_id, row_number, sample_code, sample_db_id, action, "
           "old_content, new_content, metadata, error_reason) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)")
    params = (batch_id, row_number, sample_code, sample_db_id, action,
              old_content, new_content, metadata, error_reason)

    if conn is not None:
        conn.execute(sql, params)
        return

    c = get_db()
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def batch_add_annotation_record(batch_id, row_number, sample_code, action,
                                 sample_db_id=None, annotation_id=None, annotator_id=None,
                                 old_label_key=None, old_label_text=None, old_label_id=None, old_comment=None,
                                 new_label_key=None, new_label_text=None, new_label_id=None, new_comment=None,
                                 error_reason=None, conn=None):
    """添加标注批次明细。"""
    sql = ("INSERT INTO batch_annotation_records "
           "(batch_id, row_number, sample_code, sample_db_id, annotation_id, annotator_id, action, "
           "old_label_key, old_label_text, old_label_id, old_comment, "
           "new_label_key, new_label_text, new_label_id, new_comment, error_reason) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    params = (batch_id, row_number, sample_code, sample_db_id, annotation_id, annotator_id, action,
              old_label_key, old_label_text, old_label_id, old_comment,
              new_label_key, new_label_text, new_label_id, new_comment, error_reason)

    if conn is not None:
        conn.execute(sql, params)
        return

    c = get_db()
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def batch_add_conflict_record(batch_id, conflict_id, sample_db_id, action,
                               old_status=None, new_status=None, conn=None):
    """添加冲突批次明细。"""
    sql = ("INSERT INTO batch_conflict_records "
           "(batch_id, conflict_id, sample_db_id, action, old_status, new_status) "
           "VALUES (?, ?, ?, ?, ?, ?)")
    params = (batch_id, conflict_id, sample_db_id, action, old_status, new_status)

    if conn is not None:
        conn.execute(sql, params)
        return

    c = get_db()
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def batch_add_review_task_record(batch_id, review_task_id, conflict_id, action,
                                  old_status=None, new_status=None,
                                  old_reviewer_id=None, new_reviewer_id=None, conn=None):
    """添加复核任务批次明细。"""
    sql = ("INSERT INTO batch_review_task_records "
           "(batch_id, review_task_id, conflict_id, action, "
           "old_status, new_status, old_reviewer_id, new_reviewer_id) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
    params = (batch_id, review_task_id, conflict_id, action,
              old_status, new_status, old_reviewer_id, new_reviewer_id)

    if conn is not None:
        conn.execute(sql, params)
        return

    c = get_db()
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def batch_link_revision(batch_id, revision_id, conn=None):
    """关联批次与审计日志。"""
    sql = "INSERT INTO batch_revision_links (batch_id, revision_id) VALUES (?, ?)"

    if conn is not None:
        conn.execute(sql, (batch_id, revision_id))
        return

    c = get_db()
    try:
        c.execute(sql, (batch_id, revision_id))
        c.commit()
    finally:
        c.close()


def update_batch_stats(batch_id, stats, conn=None):
    """更新批次统计信息。"""
    fields = []
    values = []
    for key, val in stats.items():
        fields.append(f"{key} = ?")
        values.append(val)
    values.append(batch_id)
    sql = f"UPDATE import_batches SET {', '.join(fields)} WHERE id = ?"

    if conn is not None:
        conn.execute(sql, values)
        return

    c = get_db()
    try:
        c.execute(sql, values)
        c.commit()
    finally:
        c.close()


def confirm_batch(batch_id, conn=None):
    """确认批次，状态从 preview 变为 confirmed。"""
    sql = ("UPDATE import_batches SET status = 'confirmed', confirmed_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'preview'"
    )

    if conn is not None:
        conn.execute(sql, (batch_id,))
        return

    c = get_db()
    try:
        c.execute(sql, (batch_id,))
        c.commit()
    finally:
        c.close()


def get_batch(batch_id, conn=None):
    """获取批次信息。"""
    sql = "SELECT * FROM import_batches WHERE id = ?"

    def _do_query(c):
        return c.execute(sql, (batch_id,)).fetchone()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def list_batches(batch_type=None, status=None, limit=100, conn=None):
    """列出批次。"""
    sql = ("SELECT ib.*, u.display_name as creator_name, "
           "u2.display_name as reverter_name "
           "FROM import_batches ib "
           "LEFT JOIN users u ON u.id = ib.created_by "
           "LEFT JOIN users u2 ON u2.id = ib.reverted_by "
           "WHERE 1=1")
    params = []
    if batch_type:
        sql += " AND ib.batch_type = ?"
        params.append(batch_type)
    if status:
        sql += " AND ib.status = ?"
        params.append(status)
    sql += " ORDER BY ib.created_at DESC LIMIT ?"
    params.append(limit)

    def _do_query(c):
        return c.execute(sql, params).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def get_batch_sample_records(batch_id, conn=None):
    """获取批次的样本明细。"""
    sql = "SELECT * FROM batch_sample_records WHERE batch_id = ? ORDER BY row_number"

    def _do_query(c):
        return c.execute(sql, (batch_id,)).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def get_batch_annotation_records(batch_id, conn=None):
    """获取批次的标注明细。"""
    sql = "SELECT * FROM batch_annotation_records WHERE batch_id = ? ORDER BY row_number"

    def _do_query(c):
        return c.execute(sql, (batch_id,)).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def get_batch_conflict_records(batch_id, conn=None):
    """获取批次的冲突明细。"""
    sql = "SELECT * FROM batch_conflict_records WHERE batch_id = ?"

    def _do_query(c):
        return c.execute(sql, (batch_id,)).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def get_batch_review_task_records(batch_id, conn=None):
    """获取批次的复核任务明细。"""
    sql = "SELECT * FROM batch_review_task_records WHERE batch_id = ?"

    def _do_query(c):
        return c.execute(sql, (batch_id,)).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def revert_batch(batch_id, reverted_by=None, revert_note=None, conn=None):
    """
    回滚批次。
    
    返回 (success, message)
    
    注意：调用方需在外层开启事务。
    """
    batch = get_batch(batch_id, conn=conn)
    if not batch:
        return False, "批次不存在"
    if batch['status'] != 'confirmed':
        return False, f"批次状态不是 confirmed，无法回滚"

    if batch['batch_type'] == 'sample':
        _revert_sample_batch(batch_id, conn=conn)
    elif batch['batch_type'] == 'annotation':
        _revert_annotation_batch(batch_id, conn=conn)

    sql = ("UPDATE import_batches SET status = 'reverted', reverted_at = CURRENT_TIMESTAMP, "
           "reverted_by = ?, revert_note = ? WHERE id = ?")
    conn.execute(sql, (reverted_by, revert_note, batch_id))

    log_revision('import_batch', batch_id, 'revert',
                 old_value='confirmed', new_value='reverted',
                 user_id=reverted_by, comment=revert_note or '回滚导入批次',
                 conn=conn)

    return True, "回滚成功"


def _revert_sample_batch(batch_id, conn=None):
    """回滚样本批次。需要先删除依赖：引用该批次样本的其他批次产生的标注、冲突等。"""
    records = conn.execute(
        "SELECT * FROM batch_sample_records WHERE batch_id = ? AND action = 'create'",
        (batch_id,)
    ).fetchall()

    sample_db_ids = [r['sample_db_id'] for r in records if r['sample_db_id']]

    if sample_db_ids:
        placeholders = ','.join('?' * len(sample_db_ids))

        conn.execute(
            f"UPDATE batch_sample_records SET sample_db_id = NULL WHERE sample_db_id IN ({placeholders})",
            sample_db_ids
        )
        conn.execute(
            f"UPDATE batch_annotation_records SET sample_db_id = NULL WHERE sample_db_id IN ({placeholders})",
            sample_db_ids
        )
        conn.execute(
            f"UPDATE batch_conflict_records SET sample_db_id = NULL WHERE sample_db_id IN ({placeholders})",
            sample_db_ids
        )

        dependent_annotation_ids = conn.execute(
            f"SELECT id FROM annotations WHERE sample_id IN ({placeholders})",
            sample_db_ids
        ).fetchall()
        ann_ids = [row['id'] for row in dependent_annotation_ids]

        if ann_ids:
            ann_placeholders = ','.join('?' * len(ann_ids))
            conn.execute(
                f"UPDATE batch_annotation_records SET annotation_id = NULL WHERE annotation_id IN ({ann_placeholders})",
                ann_ids
            )
            conn.execute(
                f"DELETE FROM conflict_annotations WHERE annotation_id IN ({ann_placeholders})",
                ann_ids
            )

        dependent_conflict_ids = conn.execute(
            f"SELECT id FROM conflicts WHERE sample_id IN ({placeholders})",
            sample_db_ids
        ).fetchall()
        c_ids = [row['id'] for row in dependent_conflict_ids]
        if c_ids:
            c_placeholders = ','.join('?' * len(c_ids))
            conn.execute(
                f"UPDATE batch_conflict_records SET conflict_id = NULL WHERE conflict_id IN ({c_placeholders})",
                c_ids
            )
            conn.execute(
                f"UPDATE batch_review_task_records SET review_task_id = NULL WHERE conflict_id IN ({c_placeholders})",
                c_ids
            )
            conn.execute(
                f"DELETE FROM review_tasks WHERE conflict_id IN ({c_placeholders})",
                c_ids
            )
            conn.execute(
                f"DELETE FROM conflicts WHERE id IN ({c_placeholders})",
                c_ids
            )

        if ann_ids:
            ann_placeholders = ','.join('?' * len(ann_ids))
            for ann_row in conn.execute(
                f"SELECT id, label_text FROM annotations WHERE id IN ({ann_placeholders})",
                ann_ids
            ).fetchall():
                log_revision('annotation', ann_row['id'], 'delete',
                             old_value=ann_row['label_text'],
                             conn=conn)
            conn.execute(
                f"DELETE FROM annotations WHERE id IN ({ann_placeholders})",
                ann_ids
            )

    for rec in records:
        if rec['sample_db_id']:
            conn.execute(
                "UPDATE batch_sample_records SET sample_db_id = NULL WHERE id = ?",
                (rec['id'],)
            )
            conn.execute("DELETE FROM samples WHERE id = ?", (rec['sample_db_id'],))
            log_revision('sample', rec['sample_db_id'], 'delete',
                         old_value=rec['new_content'],
                         conn=conn)


def _revert_annotation_batch(batch_id, conn=None):
    """回滚标注批次。"""
    review_records = conn.execute(
        "SELECT * FROM batch_review_task_records WHERE batch_id = ?",
        (batch_id,)
    ).fetchall()
    for rr in review_records:
        if rr['action'] == 'created' and rr['review_task_id']:
            conn.execute(
                "UPDATE batch_review_task_records SET review_task_id = NULL WHERE id = ?",
                (rr['id'],)
            )
            conn.execute("DELETE FROM review_tasks WHERE id = ?", (rr['review_task_id'],))
            log_revision('review_task', rr['review_task_id'], 'delete',
                         conn=conn)
        elif rr['action'] == 'affected' and rr['review_task_id']:
            if rr['old_status'] is not None:
                conn.execute("UPDATE review_tasks SET status = ? WHERE id = ?",
                             (rr['old_status'], rr['review_task_id']))
            if rr['old_reviewer_id'] is not None:
                conn.execute("UPDATE review_tasks SET reviewer_id = ? WHERE id = ?",
                             (rr['old_reviewer_id'], rr['review_task_id']))

    conflict_records = conn.execute(
        "SELECT * FROM batch_conflict_records WHERE batch_id = ?",
        (batch_id,)
    ).fetchall()
    for cr in conflict_records:
        if cr['action'] == 'created' and cr['conflict_id']:
            conn.execute(
                "UPDATE batch_conflict_records SET conflict_id = NULL WHERE id = ?",
                (cr['id'],)
            )
            conn.execute("DELETE FROM conflicts WHERE id = ?", (cr['conflict_id'],))
            log_revision('conflict', cr['conflict_id'], 'delete',
                         conn=conn)
        elif cr['action'] == 'affected' and cr['conflict_id'] and cr['old_status']:
            conn.execute("UPDATE conflicts SET status = ? WHERE id = ?",
                         (cr['old_status'], cr['conflict_id']))

    records = conn.execute(
        "SELECT * FROM batch_annotation_records WHERE batch_id = ?",
        (batch_id,)
    ).fetchall()

    for rec in records:
        if rec['action'] == 'create' and rec['annotation_id']:
            conn.execute(
                "UPDATE batch_annotation_records SET annotation_id = NULL WHERE id = ?",
                (rec['id'],)
            )
            conn.execute("DELETE FROM annotations WHERE id = ?", (rec['annotation_id'],))
            log_revision('annotation', rec['annotation_id'], 'delete',
                         old_value=rec['new_label_text'],
                         conn=conn)
        elif rec['action'] == 'update' and rec['annotation_id']:
            conn.execute(
                "UPDATE annotations SET label_id=?, label_key=?, label_text=?, comment=?, updated_at=CURRENT_TIMESTAMP "
                "WHERE id=?",
                (rec['old_label_id'], rec['old_label_key'], rec['old_label_text'],
                 rec['old_comment'], rec['annotation_id'])
            )
            log_revision('annotation', rec['annotation_id'], 'revert_update',
                         old_value=rec['new_label_text'], new_value=rec['old_label_text'],
                         conn=conn)


def _migrate_scheme_release_tables(c):
    """迁移：确保发布沙箱相关表存在且有最新字段。"""
    existing_tables = [row[0] for row in c.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()]

    if 'scheme_release_drafts' not in existing_tables:
        c.execute('''CREATE TABLE IF NOT EXISTS scheme_release_drafts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            old_scheme_id INTEGER NOT NULL REFERENCES label_schemes(id),
            new_scheme_id INTEGER NOT NULL REFERENCES label_schemes(id),
            status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published', 'reverted')),
            mapping_rules TEXT,
            impact_analysis TEXT,
            strategy_decisions TEXT,
            scheme_snapshot_old TEXT,
            scheme_snapshot_new TEXT,
            operator_note TEXT,
            created_by INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            published_at TIMESTAMP,
            published_by INTEGER REFERENCES users(id),
            reverted_at TIMESTAMP,
            reverted_by INTEGER REFERENCES users(id),
            revert_note TEXT
        )''')

    if 'scheme_release_label_mappings' not in existing_tables:
        c.execute('''CREATE TABLE IF NOT EXISTS scheme_release_label_mappings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL REFERENCES scheme_release_drafts(id) ON DELETE CASCADE,
            old_label_id INTEGER REFERENCES labels(id),
            old_label_key TEXT,
            old_label_text TEXT,
            new_label_id INTEGER REFERENCES labels(id),
            new_label_key TEXT,
            new_label_text TEXT,
            mapping_type TEXT NOT NULL CHECK (mapping_type IN (
                'direct', 'unmapped', 'duplicate', 'conflict'
            )),
            strategy TEXT NOT NULL DEFAULT 'prompt' CHECK (strategy IN (
                'prompt', 'keep_old', 'freeze', 'reopen', 'block', 'use_new'
            )),
            affected_count INTEGER DEFAULT 0,
            note TEXT
        )''')

    if 'scheme_release_impact_items' not in existing_tables:
        c.execute('''CREATE TABLE IF NOT EXISTS scheme_release_impact_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL REFERENCES scheme_release_drafts(id) ON DELETE CASCADE,
            item_type TEXT NOT NULL CHECK (item_type IN (
                'sample', 'annotation', 'conflict', 'review_task',
                'stat_caliber', 'export_field'
            )),
            item_id INTEGER,
            item_reference TEXT,
            impact_level TEXT NOT NULL CHECK (impact_level IN ('low', 'medium', 'high', 'critical')),
            description TEXT,
            detail TEXT,
            is_resolved INTEGER DEFAULT 0
        )''')

    if 'scheme_release_audit' not in existing_tables:
        c.execute('''CREATE TABLE IF NOT EXISTS scheme_release_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            draft_id INTEGER NOT NULL REFERENCES scheme_release_drafts(id) ON DELETE CASCADE,
            action TEXT NOT NULL,
            old_status TEXT,
            new_status TEXT,
            user_id INTEGER NOT NULL REFERENCES users(id),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            note TEXT,
            detail TEXT
        )''')

    existing_cols = [row[1] for row in c.execute("PRAGMA table_info(scheme_release_drafts)").fetchall()]
    if 'export_field_changes' not in existing_cols:
        c.execute("ALTER TABLE scheme_release_drafts ADD COLUMN export_field_changes TEXT")
    if 'stat_caliber_changes' not in existing_cols:
        c.execute("ALTER TABLE scheme_release_drafts ADD COLUMN stat_caliber_changes TEXT")


def init_scheme_release_tables():
    """初始化发布沙箱相关表。"""
    conn = get_db()
    c = conn.cursor()
    _migrate_scheme_release_tables(c)
    conn.commit()
    conn.close()


def create_release_draft(name, old_scheme_id, new_scheme_id, created_by,
                         description=None, mapping_rules=None, conn=None):
    """创建发布草稿。"""
    sql = ("INSERT INTO scheme_release_drafts "
           "(name, description, old_scheme_id, new_scheme_id, created_by, mapping_rules) "
           "VALUES (?, ?, ?, ?, ?, ?)")
    params = (name, description, old_scheme_id, new_scheme_id, created_by,
              json.dumps(mapping_rules, ensure_ascii=False) if mapping_rules else None)

    def _do_insert(c):
        cursor = c.execute(sql, params)
        draft_id = cursor.lastrowid
        _log_release_audit(draft_id, 'create', None, 'draft', created_by,
                          '创建发布草稿', conn=c)
        return draft_id

    if conn is not None:
        return _do_insert(conn)

    c = get_db()
    try:
        draft_id = _do_insert(c)
        c.commit()
        return draft_id
    finally:
        c.close()


def get_release_draft(draft_id, conn=None):
    """获取发布草稿。"""
    sql = ("SELECT srd.*, "
           "u1.display_name as creator_name, "
           "u2.display_name as publisher_name, "
           "u3.display_name as reverter_name, "
           "old_s.name as old_scheme_name, old_s.version as old_scheme_version, "
           "new_s.name as new_scheme_name, new_s.version as new_scheme_version "
           "FROM scheme_release_drafts srd "
           "LEFT JOIN users u1 ON u1.id = srd.created_by "
           "LEFT JOIN users u2 ON u2.id = srd.published_by "
           "LEFT JOIN users u3 ON u3.id = srd.reverted_by "
           "LEFT JOIN label_schemes old_s ON old_s.id = srd.old_scheme_id "
           "LEFT JOIN label_schemes new_s ON new_s.id = srd.new_scheme_id "
           "WHERE srd.id = ?")

    def _do_query(c):
        return c.execute(sql, (draft_id,)).fetchone()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def list_release_drafts(status=None, limit=100, conn=None):
    """列出发布草稿。"""
    sql = ("SELECT srd.*, "
           "u1.display_name as creator_name, "
           "old_s.name as old_scheme_name, old_s.version as old_scheme_version, "
           "new_s.name as new_scheme_name, new_s.version as new_scheme_version "
           "FROM scheme_release_drafts srd "
           "LEFT JOIN users u1 ON u1.id = srd.created_by "
           "LEFT JOIN label_schemes old_s ON old_s.id = srd.old_scheme_id "
           "LEFT JOIN label_schemes new_s ON new_s.id = srd.new_scheme_id "
           "WHERE 1=1")
    params = []
    if status:
        sql += " AND srd.status = ?"
        params.append(status)
    sql += " ORDER BY srd.created_at DESC LIMIT ?"
    params.append(limit)

    def _do_query(c):
        return c.execute(sql, params).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def update_release_draft(draft_id, updates, user_id, conn=None):
    """更新发布草稿。"""
    fields = []
    values = []
    for key, val in updates.items():
        if key in ('mapping_rules', 'impact_analysis', 'strategy_decisions',
                   'scheme_snapshot_old', 'scheme_snapshot_new',
                   'export_field_changes', 'stat_caliber_changes'):
            fields.append(f"{key} = ?")
            values.append(json.dumps(val, ensure_ascii=False) if val is not None else None)
        else:
            fields.append(f"{key} = ?")
            values.append(val)
    values.append(draft_id)
    sql = f"UPDATE scheme_release_drafts SET {', '.join(fields)} WHERE id = ?"

    def _do_update(c):
        c.execute(sql, values)
        _log_release_audit(draft_id, 'update', None, None, user_id,
                          '更新草稿内容', detail=str(updates.keys()), conn=c)

    if conn is not None:
        _do_update(conn)
        return

    c = get_db()
    try:
        _do_update(c)
        c.commit()
    finally:
        c.close()


def add_label_mapping(draft_id, mapping_data, conn=None):
    """添加标签映射。"""
    sql = ("INSERT INTO scheme_release_label_mappings "
           "(draft_id, old_label_id, old_label_key, old_label_text, "
           "new_label_id, new_label_key, new_label_text, "
           "mapping_type, strategy, affected_count, note) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)")
    params = (draft_id,
              mapping_data.get('old_label_id'),
              mapping_data.get('old_label_key'),
              mapping_data.get('old_label_text'),
              mapping_data.get('new_label_id'),
              mapping_data.get('new_label_key'),
              mapping_data.get('new_label_text'),
              mapping_data['mapping_type'],
              mapping_data.get('strategy', 'prompt'),
              mapping_data.get('affected_count', 0),
              mapping_data.get('note'))

    if conn is not None:
        conn.execute(sql, params)
        return

    c = get_db()
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def update_label_mapping(mapping_id, updates, conn=None):
    """更新标签映射。"""
    fields = []
    values = []
    for key, val in updates.items():
        fields.append(f"{key} = ?")
        values.append(val)
    values.append(mapping_id)
    sql = f"UPDATE scheme_release_label_mappings SET {', '.join(fields)} WHERE id = ?"

    if conn is not None:
        conn.execute(sql, values)
        return

    c = get_db()
    try:
        c.execute(sql, values)
        c.commit()
    finally:
        c.close()


def get_label_mappings(draft_id, conn=None):
    """获取草稿的所有标签映射。"""
    sql = "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? ORDER BY id"

    def _do_query(c):
        return c.execute(sql, (draft_id,)).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def add_impact_item(draft_id, item_data, conn=None):
    """添加影响分析项。"""
    sql = ("INSERT INTO scheme_release_impact_items "
           "(draft_id, item_type, item_id, item_reference, "
           "impact_level, description, detail, is_resolved) "
           "VALUES (?, ?, ?, ?, ?, ?, ?, ?)")
    params = (draft_id,
              item_data['item_type'],
              item_data.get('item_id'),
              item_data.get('item_reference'),
              item_data['impact_level'],
              item_data.get('description'),
              json.dumps(item_data.get('detail'), ensure_ascii=False) if item_data.get('detail') else None,
              item_data.get('is_resolved', 0))

    if conn is not None:
        conn.execute(sql, params)
        return

    c = get_db()
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def get_impact_items(draft_id, item_type=None, conn=None):
    """获取草稿的影响分析项。"""
    sql = "SELECT * FROM scheme_release_impact_items WHERE draft_id = ?"
    params = [draft_id]
    if item_type:
        sql += " AND item_type = ?"
        params.append(item_type)
    sql += " ORDER BY impact_level DESC, id"

    def _do_query(c):
        return c.execute(sql, params).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def clear_impact_items(draft_id, item_type=None, conn=None):
    """清除草稿的影响分析项（用于重新计算）。"""
    sql = "DELETE FROM scheme_release_impact_items WHERE draft_id = ?"
    params = [draft_id]
    if item_type:
        sql += " AND item_type = ?"
        params.append(item_type)

    def _do_delete(c):
        c.execute(sql, params)

    if conn is not None:
        _do_delete(conn)
        return

    c = get_db()
    try:
        _do_delete(c)
        c.commit()
    finally:
        c.close()


def clear_label_mappings(draft_id, conn=None):
    """清除草稿的标签映射（用于重新计算）。"""
    sql = "DELETE FROM scheme_release_label_mappings WHERE draft_id = ?"

    def _do_delete(c):
        c.execute(sql, (draft_id,))

    if conn is not None:
        _do_delete(conn)
        return

    c = get_db()
    try:
        _do_delete(c)
        c.commit()
    finally:
        c.close()


def publish_release_draft(draft_id, published_by, operator_note=None, conn=None):
    """正式发布草稿。"""
    draft = get_release_draft(draft_id, conn=conn)
    if not draft:
        return False, "草稿不存在"
    if draft['status'] != 'draft':
        return False, f"草稿状态为 {draft['status']}，无法发布"

    mappings = get_label_mappings(draft_id, conn=conn)
    has_block = any(m['strategy'] == 'block' for m in mappings)
    if has_block:
        return False, "存在需拦截的标签映射，请先处理所有拦截项"

    has_prompt = any(m['strategy'] == 'prompt' for m in mappings)
    if has_prompt:
        return False, "存在未明确策略的标签映射，请先为所有映射选择处理策略"

    def _do_publish(c):
        c.execute(
            "UPDATE scheme_release_drafts "
            "SET status = 'published', published_at = CURRENT_TIMESTAMP, "
            "published_by = ?, operator_note = ? WHERE id = ?",
            (published_by, operator_note, draft_id)
        )

        c.execute(
            "UPDATE label_schemes SET is_active = 0 "
            "WHERE name = (SELECT name FROM label_schemes WHERE id = ?) AND id != ?",
            (draft['new_scheme_id'], draft['new_scheme_id'])
        )
        c.execute(
            "UPDATE label_schemes SET is_active = 1 WHERE id = ?",
            (draft['new_scheme_id'],)
        )

        _apply_migration_strategies(draft_id, c)

        _log_release_audit(draft_id, 'publish', 'draft', 'published', published_by,
                          note=operator_note or '正式发布标签方案', conn=c)

        log_revision('label_scheme', draft['new_scheme_id'], 'release_publish',
                     old_value=f"{draft['old_scheme_name']} v{draft['old_scheme_version']}",
                     new_value=f"{draft['new_scheme_name']} v{draft['new_scheme_version']}",
                     user_id=published_by,
                     comment=operator_note or f"通过发布沙箱发布，草稿ID: {draft_id}",
                     conn=c)
        return True, "发布成功"

    if conn is not None:
        return _do_publish(conn)

    c = get_db()
    try:
        result = _do_publish(c)
        c.commit()
        return result
    finally:
        c.close()


def _apply_migration_strategies(draft_id, conn):
    """应用迁移策略，处理标注、冲突、复核任务。"""
    mappings = conn.execute(
        "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ?",
        (draft_id,)
    ).fetchall()

    draft = conn.execute(
        "SELECT * FROM scheme_release_drafts WHERE id = ?",
        (draft_id,)
    ).fetchone()

    for m in mappings:
        if m['strategy'] == 'use_new' and m['old_label_id'] and m['new_label_id']:
            conn.execute(
                "UPDATE annotations SET label_id = ?, label_key = ?, label_text = ?, "
                "scheme_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE label_id = ? AND scheme_id = ?",
                (m['new_label_id'], m['new_label_key'], m['new_label_text'],
                 draft['new_scheme_id'], m['old_label_id'], draft['old_scheme_id'])
            )

        elif m['strategy'] == 'keep_old':
            conn.execute(
                "UPDATE annotations SET scheme_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE label_id = ? AND scheme_id = ?",
                (draft['new_scheme_id'], m['old_label_id'], draft['old_scheme_id'])
            )

        elif m['strategy'] == 'freeze':
            pass

        elif m['strategy'] == 'reopen':
            conflicts = conn.execute(
                "SELECT DISTINCT c.id FROM conflicts c "
                "JOIN conflict_annotations ca ON ca.conflict_id = c.id "
                "JOIN annotations a ON a.id = ca.annotation_id "
                "WHERE a.label_id = ? AND c.scheme_id = ? AND c.status = 'closed'",
                (m['old_label_id'], draft['old_scheme_id'])
            ).fetchall()
            for c_row in conflicts:
                conn.execute(
                    "UPDATE conflicts SET status = 'open', resolved_at = NULL, "
                    "final_label_id = NULL, final_label_key = NULL, "
                    "final_label_text = NULL, resolved_by = NULL, "
                    "resolution_note = NULL WHERE id = ?",
                    (c_row['id'],)
                )
                conn.execute(
                    "UPDATE review_tasks SET status = 'pending' WHERE conflict_id = ?",
                    (c_row['id'],)
                )

        elif m['strategy'] == 'block':
            pass

    conn.execute(
        "UPDATE samples SET scheme_id = ? WHERE scheme_id = ?",
        (draft['new_scheme_id'], draft['old_scheme_id'])
    )

    conn.execute(
        "UPDATE conflicts SET scheme_id = ? WHERE scheme_id = ?",
        (draft['old_scheme_id'], draft['old_scheme_id'])
    )

    conn.execute(
        "UPDATE review_tasks SET conflict_id = conflict_id WHERE conflict_id IN "
        "(SELECT id FROM conflicts WHERE scheme_id = ?)",
        (draft['new_scheme_id'],)
    )


def revert_release_draft(draft_id, reverted_by, revert_note=None, conn=None):
    """撤回发布，恢复旧方案。"""
    draft = get_release_draft(draft_id, conn=conn)
    if not draft:
        return False, "草稿不存在"
    if draft['status'] != 'published':
        return False, f"草稿状态为 {draft['status']}，无法撤回"

    def _do_revert(c):
        _rollback_migration(draft_id, c)

        c.execute(
            "UPDATE scheme_release_drafts "
            "SET status = 'reverted', reverted_at = CURRENT_TIMESTAMP, "
            "reverted_by = ?, revert_note = ? WHERE id = ?",
            (reverted_by, revert_note, draft_id)
        )

        c.execute(
            "UPDATE label_schemes SET is_active = 0 "
            "WHERE name = (SELECT name FROM label_schemes WHERE id = ?) AND id != ?",
            (draft['old_scheme_id'], draft['old_scheme_id'])
        )
        c.execute(
            "UPDATE label_schemes SET is_active = 1 WHERE id = ?",
            (draft['old_scheme_id'],)
        )

        _log_release_audit(draft_id, 'revert', 'published', 'reverted', reverted_by,
                          note=revert_note or '撤回发布，恢复旧方案', conn=c)

        log_revision('label_scheme', draft['old_scheme_id'], 'release_revert',
                     old_value=f"{draft['new_scheme_name']} v{draft['new_scheme_version']}",
                     new_value=f"{draft['old_scheme_name']} v{draft['old_scheme_version']}",
                     user_id=reverted_by,
                     comment=revert_note or f"撤回发布，草稿ID: {draft_id}",
                     conn=c)
        return True, "撤回成功，已恢复旧方案"

    if conn is not None:
        return _do_revert(conn)

    c = get_db()
    try:
        result = _do_revert(c)
        c.commit()
        return result
    finally:
        c.close()


def _rollback_migration(draft_id, conn):
    """回滚迁移操作。"""
    draft = conn.execute(
        "SELECT * FROM scheme_release_drafts WHERE id = ?",
        (draft_id,)
    ).fetchone()

    mappings = conn.execute(
        "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ?",
        (draft_id,)
    ).fetchall()

    for m in mappings:
        if m['strategy'] == 'use_new' and m['old_label_id'] and m['new_label_id']:
            conn.execute(
                "UPDATE annotations SET label_id = ?, label_key = ?, label_text = ?, "
                "scheme_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE label_id = ? AND scheme_id = ?",
                (m['old_label_id'], m['old_label_key'], m['old_label_text'],
                 draft['old_scheme_id'], m['new_label_id'], draft['new_scheme_id'])
            )
        elif m['strategy'] in ('keep_old', 'freeze'):
            conn.execute(
                "UPDATE annotations SET scheme_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE label_id = ? AND scheme_id = ?",
                (draft['old_scheme_id'], m['old_label_id'], draft['new_scheme_id'])
            )

    conn.execute(
        "UPDATE samples SET scheme_id = ? WHERE scheme_id = ?",
        (draft['old_scheme_id'], draft['new_scheme_id'])
    )

    conn.execute(
        "UPDATE conflicts SET scheme_id = ? WHERE scheme_id = ?",
        (draft['new_scheme_id'], draft['new_scheme_id'])
    )


def _log_release_audit(draft_id, action, old_status, new_status, user_id,
                       note=None, detail=None, conn=None):
    """记录发布审计日志。"""
    sql = ("INSERT INTO scheme_release_audit "
           "(draft_id, action, old_status, new_status, user_id, note, detail) "
           "VALUES (?, ?, ?, ?, ?, ?, ?)")
    params = (draft_id, action, old_status, new_status, user_id, note,
              json.dumps(detail, ensure_ascii=False) if detail else None)

    if conn is not None:
        conn.execute(sql, params)
        return

    c = get_db()
    try:
        c.execute(sql, params)
        c.commit()
    finally:
        c.close()


def get_release_audit_log(draft_id, conn=None):
    """获取发布审计日志。"""
    sql = ("SELECT sra.*, u.display_name as user_name "
           "FROM scheme_release_audit sra "
           "LEFT JOIN users u ON u.id = sra.user_id "
           "WHERE sra.draft_id = ? ORDER BY sra.created_at DESC")

    def _do_query(c):
        return c.execute(sql, (draft_id,)).fetchall()

    if conn is not None:
        return _do_query(conn)

    c = get_db()
    try:
        return _do_query(c)
    finally:
        c.close()


def analyze_release_impact(draft_id, conn=None):
    """分析发布影响，生成影响分析项。"""
    draft = get_release_draft(draft_id, conn=conn)
    if not draft:
        return False, "草稿不存在"

    def _do_analyze(c):
        clear_impact_items(draft_id, conn=c)
        clear_label_mappings(draft_id, conn=c)

        old_labels = c.execute(
            "SELECT * FROM labels WHERE scheme_id = ? ORDER BY label_key",
            (draft['old_scheme_id'],)
        ).fetchall()
        new_labels = c.execute(
            "SELECT * FROM labels WHERE scheme_id = ? ORDER BY label_key",
            (draft['new_scheme_id'],)
        ).fetchall()

        new_label_keys = {l['label_key']: l for l in new_labels}
        new_label_texts = {l['label_text']: l for l in new_labels}
        old_label_keys = {l['label_key']: l for l in old_labels}

        for old_lbl in old_labels:
            affected_ann_count = c.execute(
                "SELECT COUNT(*) FROM annotations WHERE label_id = ? AND scheme_id = ?",
                (old_lbl['id'], draft['old_scheme_id'])
            ).fetchone()[0]

            if old_lbl['label_key'] in new_label_keys:
                new_lbl = new_label_keys[old_lbl['label_key']]
                mapping_type = 'duplicate' if old_lbl['label_text'] != new_lbl['label_text'] else 'direct'
                add_label_mapping(draft_id, {
                    'old_label_id': old_lbl['id'],
                    'old_label_key': old_lbl['label_key'],
                    'old_label_text': old_lbl['label_text'],
                    'new_label_id': new_lbl['id'],
                    'new_label_key': new_lbl['label_key'],
                    'new_label_text': new_lbl['label_text'],
                    'mapping_type': mapping_type,
                    'strategy': 'use_new' if mapping_type == 'direct' else 'prompt',
                    'affected_count': affected_ann_count,
                    'note': '键名匹配' if mapping_type == 'direct' else '键名匹配但文本不同，需确认'
                }, conn=c)
            elif old_lbl['label_text'] in new_label_texts:
                new_lbl = new_label_texts[old_lbl['label_text']]
                add_label_mapping(draft_id, {
                    'old_label_id': old_lbl['id'],
                    'old_label_key': old_lbl['label_key'],
                    'old_label_text': old_lbl['label_text'],
                    'new_label_id': new_lbl['id'],
                    'new_label_key': new_lbl['label_key'],
                    'new_label_text': new_lbl['label_text'],
                    'mapping_type': 'duplicate',
                    'strategy': 'prompt',
                    'affected_count': affected_ann_count,
                    'note': '文本匹配但键名不同，需确认映射关系'
                }, conn=c)
            else:
                add_label_mapping(draft_id, {
                    'old_label_id': old_lbl['id'],
                    'old_label_key': old_lbl['label_key'],
                    'old_label_text': old_lbl['label_text'],
                    'new_label_id': None,
                    'new_label_key': None,
                    'new_label_text': None,
                    'mapping_type': 'unmapped',
                    'strategy': 'prompt',
                    'affected_count': affected_ann_count,
                    'note': '新方案中无对应标签，需选择处理策略'
                }, conn=c)

        for new_lbl in new_labels:
            if new_lbl['label_key'] not in old_label_keys and new_lbl['label_text'] not in {l['label_text'] for l in old_labels}:
                add_label_mapping(draft_id, {
                    'old_label_id': None,
                    'old_label_key': None,
                    'old_label_text': None,
                    'new_label_id': new_lbl['id'],
                    'new_label_key': new_lbl['label_key'],
                    'new_label_text': new_lbl['label_text'],
                    'mapping_type': 'unmapped',
                    'strategy': 'use_new',
                    'affected_count': 0,
                    'note': '新方案新增标签'
                }, conn=c)

        sample_count = c.execute(
            "SELECT COUNT(*) FROM samples WHERE scheme_id = ?",
            (draft['old_scheme_id'],)
        ).fetchone()[0]
        if sample_count > 0:
            add_impact_item(draft_id, {
                'item_type': 'sample',
                'item_reference': f'{sample_count}个样本',
                'impact_level': 'high',
                'description': f'将有 {sample_count} 个样本切换到新方案',
                'detail': {'count': sample_count}
            }, conn=c)

        ann_count = c.execute(
            "SELECT COUNT(*) FROM annotations WHERE scheme_id = ?",
            (draft['old_scheme_id'],)
        ).fetchone()[0]
        if ann_count > 0:
            add_impact_item(draft_id, {
                'item_type': 'annotation',
                'item_reference': f'{ann_count}条标注',
                'impact_level': 'high',
                'description': f'将有 {ann_count} 条标注受方案切换影响',
                'detail': {'count': ann_count}
            }, conn=c)

        open_conflicts = c.execute(
            "SELECT COUNT(*) FROM conflicts WHERE scheme_id = ? AND status IN ('open', 'assigned')",
            (draft['old_scheme_id'],)
        ).fetchone()[0]
        if open_conflicts > 0:
            add_impact_item(draft_id, {
                'item_type': 'conflict',
                'item_reference': f'{open_conflicts}个未结案冲突',
                'impact_level': 'critical',
                'description': f'存在 {open_conflicts} 个未结案冲突，需确认处理策略',
                'detail': {'count': open_conflicts, 'status': 'open/assigned'}
            }, conn=c)

        closed_conflicts = c.execute(
            "SELECT COUNT(*) FROM conflicts WHERE scheme_id = ? AND status = 'closed'",
            (draft['old_scheme_id'],)
        ).fetchone()[0]
        if closed_conflicts > 0:
            add_impact_item(draft_id, {
                'item_type': 'conflict',
                'item_reference': f'{closed_conflicts}个已结案冲突',
                'impact_level': 'medium',
                'description': f'存在 {closed_conflicts} 个已结案冲突，需确认是否重开',
                'detail': {'count': closed_conflicts, 'status': 'closed'}
            }, conn=c)

        pending_reviews = c.execute(
            "SELECT COUNT(*) FROM review_tasks rt "
            "JOIN conflicts c ON c.id = rt.conflict_id "
            "WHERE c.scheme_id = ? AND rt.status IN ('pending', 'in_progress')",
            (draft['old_scheme_id'],)
        ).fetchone()[0]
        if pending_reviews > 0:
            add_impact_item(draft_id, {
                'item_type': 'review_task',
                'item_reference': f'{pending_reviews}个待处理复核任务',
                'impact_level': 'critical',
                'description': f'存在 {pending_reviews} 个正在处理的复核任务，需确认处理策略',
                'detail': {'count': pending_reviews}
            }, conn=c)

        add_impact_item(draft_id, {
            'item_type': 'stat_caliber',
            'item_reference': '统计口径变化',
            'impact_level': 'medium',
            'description': '方案切换后，基于标签的统计数据口径将发生变化',
            'detail': {'note': '建议导出切换前的统计数据进行备份'}
        }, conn=c)

        add_impact_item(draft_id, {
            'item_type': 'export_field',
            'item_reference': '导出字段变化',
            'impact_level': 'medium',
            'description': '方案切换后，导出的标签字段将使用新方案定义',
            'detail': {'note': '可通过导出差异功能查看具体变化'}
        }, conn=c)

        impact_items = get_impact_items(draft_id, conn=c)
        mappings = get_label_mappings(draft_id, conn=c)

        old_scheme_data = c.execute(
            "SELECT * FROM label_schemes WHERE id = ?",
            (draft['old_scheme_id'],)
        ).fetchone()
        new_scheme_data = c.execute(
            "SELECT * FROM label_schemes WHERE id = ?",
            (draft['new_scheme_id'],)
        ).fetchone()
        old_labels_data = [dict(l) for l in old_labels]
        new_labels_data = [dict(l) for l in new_labels]

        update_release_draft(draft_id, {
            'impact_analysis': {
                'summary': {
                    'total_samples': sample_count,
                    'total_annotations': ann_count,
                    'open_conflicts': open_conflicts,
                    'closed_conflicts': closed_conflicts,
                    'pending_reviews': pending_reviews,
                    'total_mappings': len(mappings),
                    'unmapped_count': len([m for m in mappings if m['mapping_type'] == 'unmapped' and m['old_label_id']]),
                    'duplicate_count': len([m for m in mappings if m['mapping_type'] == 'duplicate']),
                    'prompt_count': len([m for m in mappings if m['strategy'] == 'prompt']),
                    'block_count': len([m for m in mappings if m['strategy'] == 'block']),
                },
                'items': [dict(i) for i in impact_items]
            },
            'scheme_snapshot_old': {
                'scheme': dict(old_scheme_data),
                'labels': old_labels_data
            },
            'scheme_snapshot_new': {
                'scheme': dict(new_scheme_data),
                'labels': new_labels_data
            },
            'mapping_rules': [dict(m) for m in mappings],
            'export_field_changes': {
                'old_fields': [l['label_key'] for l in old_labels],
                'new_fields': [l['label_key'] for l in new_labels],
                'added': [l['label_key'] for l in new_labels if l['label_key'] not in old_label_keys],
                'removed': [l['label_key'] for l in old_labels if l['label_key'] not in {nl['label_key'] for nl in new_labels}]
            },
            'stat_caliber_changes': {
                'note': '标签定义变化将影响所有基于标签的统计指标',
                'old_label_count': len(old_labels),
                'new_label_count': len(new_labels)
            }
        }, user_id=draft['created_by'], conn=c)

        return True, "影响分析完成"

    if conn is not None:
        return _do_analyze(conn)

    c = get_db()
    try:
        result = _do_analyze(c)
        c.commit()
        return result
    finally:
        c.close()


def generate_diff_export(draft_id, conn=None):
    """生成差异导出数据。"""
    draft = get_release_draft(draft_id, conn=conn)
    if not draft:
        return None

    def _do_export(c):
        mappings = get_label_mappings(draft_id, conn=c)
        impact_items = get_impact_items(draft_id, conn=c)
        audit_log = get_release_audit_log(draft_id, conn=c)

        return {
            'draft': dict(draft),
            'mappings': [dict(m) for m in mappings],
            'impact_items': [dict(i) for i in impact_items],
            'audit_log': [dict(a) for a in audit_log],
            'exported_at': datetime.now().isoformat()
        }

    if conn is not None:
        return _do_export(conn)

    c = get_db()
    try:
        return _do_export(c)
    finally:
        c.close()
