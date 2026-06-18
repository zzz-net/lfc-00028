"""
初始化样例数据脚本
运行此脚本可快速填充测试数据，方便验证系统功能
"""
import os
import sys
import csv
import json
import io

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from models import init_db, get_db
from werkzeug.security import generate_password_hash


def _log_revision_conn(conn, entity_type, entity_id, action, old_value=None, new_value=None, user_id=None, comment=None):
    conn.execute(
        "INSERT INTO revision_history (entity_type, entity_id, action, old_value, new_value, user_id, comment) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (entity_type, entity_id, action, str(old_value) if old_value else None,
         str(new_value) if new_value else None, user_id, comment)
    )


def seed_all():
    init_db()
    conn = get_db()
    print("=" * 60)
    print("开始初始化样例数据...")
    print("=" * 60)

    # 1. 创建情感分析标签方案 v1
    print("\n[1/6] 创建标签方案：情感分析 v1")
    scheme_name = "情感分析"
    max_v = conn.execute("SELECT MAX(version) FROM label_schemes WHERE name = ?", (scheme_name,)).fetchone()[0] or 0
    if max_v > 0:
        print(f"    方案已存在 v{max_v}，跳过创建")
        scheme_id = conn.execute(
            "SELECT id FROM label_schemes WHERE name = ? AND is_active = 1", (scheme_name,)
        ).fetchone()['id']
    else:
        cursor = conn.execute(
            "INSERT INTO label_schemes (name, version, description, is_active, created_by) VALUES (?, 1, ?, 1, 1)",
            (scheme_name, "电商评论和APP反馈的情感极性分类")
        )
        scheme_id = cursor.lastrowid
        labels_v1 = [
            ('positive', '正面', '#10b981', '表达正面情绪、满意、赞赏、推荐'),
            ('neutral', '中性', '#f59e0b', '无明显情感倾向或褒贬各半'),
            ('negative', '负面', '#ef4444', '表达负面情绪、不满、抱怨'),
        ]
        for k, t, c, d in labels_v1:
            conn.execute(
                "INSERT INTO labels (scheme_id, label_key, label_text, color, description) VALUES (?, ?, ?, ?, ?)",
                (scheme_id, k, t, c, d)
            )
        conn.commit()
        print(f"    已创建 v1，含 {len(labels_v1)} 个标签")

    # 2. 导入样本
    print("\n[2/6] 导入样本数据")
    samples_file = os.path.join(os.path.dirname(__file__), 'examples', 'samples.csv')
    if os.path.exists(samples_file):
        with open(samples_file, 'r', encoding='utf-8-sig') as f:
            reader = csv.DictReader(f)
            imported = 0
            duplicates = 0
            for row in reader:
                sid = row.get('sample_id', '').strip()
                content = row.get('content', '').strip()
                metadata = {k: v for k, v in row.items() if k not in ('sample_id', 'content')}
                if not sid or not content:
                    continue
                existing = conn.execute("SELECT id FROM samples WHERE sample_id = ?", (sid,)).fetchone()
                if existing:
                    duplicates += 1
                    continue
                conn.execute(
                    "INSERT INTO samples (sample_id, content, scheme_id, imported_by, metadata) VALUES (?, ?, ?, 1, ?)",
                    (sid, content, scheme_id, json.dumps(metadata, ensure_ascii=False) if metadata else None)
                )
                imported += 1
            conn.commit()
            print(f"    成功导入 {imported} 条，跳过重复 {duplicates} 条")
    else:
        print("    samples.csv 不存在，跳过")

    # 3. 导入标注员甲的标注
    print("\n[3/6] 导入 标注员甲(annotator1) 的标注")
    ann1_file = os.path.join(os.path.dirname(__file__), 'examples', 'annotations_annotator1.csv')
    annotator1_id = conn.execute("SELECT id FROM users WHERE username = 'annotator1'").fetchone()['id']
    _import_annotations(conn, ann1_file, scheme_id, annotator1_id)

    # 4. 导入标注员乙的标注（含未知标签和分歧）
    print("\n[4/6] 导入 标注员乙(annotator2) 的标注（含未知标签和分歧）")
    ann2_file = os.path.join(os.path.dirname(__file__), 'examples', 'annotations_annotator2.csv')
    annotator2_id = conn.execute("SELECT id FROM users WHERE username = 'annotator2'").fetchone()['id']
    _import_annotations(conn, ann2_file, scheme_id, annotator2_id)

    # 5. 自动检测冲突
    print("\n[5/6] 检测标注冲突")
    cursor = conn.execute(
        "SELECT sample_id, GROUP_CONCAT(DISTINCT COALESCE(label_key, '')) as labels "
        "FROM annotations WHERE scheme_id = ? AND is_unknown_label = 0 "
        "GROUP BY sample_id HAVING COUNT(DISTINCT COALESCE(label_key, '')) > 1",
        (scheme_id,)
    )
    conflicts = cursor.fetchall()
    new_conflicts = 0
    for pc in conflicts:
        existing = conn.execute(
            "SELECT id FROM conflicts WHERE sample_id = ? AND scheme_id = ? AND status != 'resolved'",
            (pc['sample_id'], scheme_id)
        ).fetchone()
        if existing:
            continue
        cur2 = conn.execute(
            "INSERT INTO conflicts (sample_id, scheme_id, status) VALUES (?, ?, 'open')",
            (pc['sample_id'], scheme_id)
        )
        cid = cur2.lastrowid
        anns = conn.execute(
            "SELECT id FROM annotations WHERE sample_id = ? AND scheme_id = ?",
            (pc['sample_id'], scheme_id)
        ).fetchall()
        for a in anns:
            conn.execute(
                "INSERT INTO conflict_annotations (conflict_id, annotation_id) VALUES (?, ?)",
                (cid, a['id'])
            )
        _log_revision_conn(conn, 'conflict', cid, 'detect', user_id=1, comment=f"初始化脚本自动检测冲突")
        new_conflicts += 1
    conn.commit()
    print(f"    检测到并创建 {new_conflicts} 个冲突")

    # 6. 演示标签方案升级（创建 v2 增加 very_negative 标签）
    print("\n[6/6] 演示标签方案升级：创建 情感分析 v2（增加 very_negative 标签）")
    v2_exists = conn.execute(
        "SELECT COUNT(*) FROM label_schemes WHERE name = ? AND version = 2", (scheme_name,)
    ).fetchone()[0]
    if v2_exists == 0:
        cursor = conn.execute(
            "INSERT INTO label_schemes (name, version, description, is_active, created_by) VALUES (?, 2, ?, 1, 1)",
            (scheme_name, "情感分析 v2：增加强烈负面标签，原 v1 数据保留独立展示，不迁移")
        )
        v2_id = cursor.lastrowid
        conn.execute("UPDATE label_schemes SET is_active = 0 WHERE name = ? AND id != ?", (scheme_name, v2_id))
        labels_v2 = [
            ('positive', '正面', '#10b981', '表达正面情绪、满意、赞赏、推荐'),
            ('neutral', '中性', '#f59e0b', '无明显情感倾向或褒贬各半'),
            ('negative', '负面', '#ef4444', '表达负面情绪、不满、抱怨'),
            ('very_negative', '强烈负面', '#991b1b', '极其不满、愤怒、强烈不推荐'),
        ]
        for k, t, c, d in labels_v2:
            conn.execute(
                "INSERT INTO labels (scheme_id, label_key, label_text, color, description) VALUES (?, ?, ?, ?, ?)",
                (v2_id, k, t, c, d)
            )
        conn.commit()
        print(f"    已创建 v2（id={v2_id}），v1 保留为历史版本")
        print("    [警告] v1 的样本、标注、冲突数据保持独立，不会自动迁移到 v2")
    else:
        print("    v2 已存在，跳过")

    conn.close()
    print("\n" + "=" * 60)
    print("样例数据初始化完成！")
    print("=" * 60)
    print("\n测试账号：")
    print("  管理员:    admin / admin123")
    print("  标注员甲:  annotator1 / anno123")
    print("  标注员乙:  annotator2 / anno123")
    print("  复核员甲:  reviewer1 / review123")
    print("  复核员乙:  reviewer2 / review123")
    print("\n验证建议：")
    print("  1. 以 annotator1/annotator2 登录，确认只能看到自己的标注")
    print("  2. 以 reviewer1 登录，确认只能复核分配给自己的任务")
    print("  3. 以 admin 登录：")
    print("     - 在冲突列表选择方案检测冲突")
    print("     - 进入冲突详情分配复核员（注意：不能选参与标注的人）")
    print("     - 查看标签方案，验证 v1/v2 数据隔离")
    print("     - 在标注列表筛选『仅显示未知标签』")
    print("     - 导出证据 CSV/JSON")
    print("     - 查看修订历史")


def _import_annotations(conn, filepath, scheme_id, annotator_id):
    if not os.path.exists(filepath):
        print(f"    {os.path.basename(filepath)} 不存在，跳过")
        return

    scheme_labels = conn.execute(
        "SELECT id, label_key, label_text FROM labels WHERE scheme_id = ?", (scheme_id,)
    ).fetchall()
    label_map = {}
    for lbl in scheme_labels:
        label_map[lbl['label_key']] = lbl
        label_map[lbl['label_text']] = lbl

    with open(filepath, 'r', encoding='utf-8-sig') as f:
        reader = csv.DictReader(f)
        imported = 0
        unknown = 0
        missing = 0
        for row in reader:
            sid = (row.get('sample_id') or row.get('id') or '').strip()
            label_key = (row.get('label') or row.get('label_key') or '').strip()
            label_text = (row.get('label_text') or '').strip()
            comment = (row.get('comment') or row.get('note') or '').strip()

            if not sid or not label_key:
                continue
            sample = conn.execute("SELECT id FROM samples WHERE sample_id = ?", (sid,)).fetchone()
            if not sample:
                missing += 1
                continue

            matched = label_map.get(label_key) or label_map.get(label_text)
            is_unknown = 0
            lbl_id = None
            final_key = label_key
            final_text = label_text or label_key

            if matched:
                lbl_id = matched['id']
                final_key = matched['label_key']
                final_text = matched['label_text']
            else:
                is_unknown = 1
                unknown += 1

            existing = conn.execute(
                "SELECT id FROM annotations WHERE sample_id = ? AND annotator_id = ? AND scheme_id = ?",
                (sample['id'], annotator_id, scheme_id)
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE annotations SET label_id=?, label_key=?, label_text=?, is_unknown_label=?, "
                    "comment=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (lbl_id, final_key, final_text, is_unknown, comment, existing['id'])
                )
            else:
                conn.execute(
                    "INSERT INTO annotations (sample_id, annotator_id, scheme_id, label_id, label_key, "
                    "label_text, is_unknown_label, comment) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (sample['id'], annotator_id, scheme_id, lbl_id, final_key, final_text, is_unknown, comment)
                )
            imported += 1
        conn.commit()
        print(f"    成功导入/更新 {imported} 条，未知标签 {unknown} 个，缺失样本 {missing} 个")


if __name__ == '__main__':
    seed_all()
