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

    c.execute('''CREATE INDEX IF NOT EXISTS idx_annotations_sample ON annotations(sample_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_annotations_annotator ON annotations(annotator_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_conflicts_sample ON conflicts(sample_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_review_tasks_reviewer ON review_tasks(reviewer_id)''')
    c.execute('''CREATE INDEX IF NOT EXISTS idx_revision_entity ON revision_history(entity_type, entity_id)''')

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


def log_revision(entity_type, entity_id, action, old_value=None, new_value=None, user_id=None, comment=None):
    conn = get_db()
    conn.execute(
        "INSERT INTO revision_history (entity_type, entity_id, action, old_value, new_value, user_id, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity_type, entity_id, action, str(old_value) if old_value else None,
         str(new_value) if new_value else None, user_id, comment)
    )
    conn.commit()
    conn.close()
