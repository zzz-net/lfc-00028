import sqlite3
import os
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
