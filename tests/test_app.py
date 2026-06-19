"""
回归测试：验证数据库锁问题修复 + 未知标签无效行处理。

用 Flask test_client 发送真实 POST 请求（form-data 编码），不调用初始化脚本，
完全模拟浏览器用户真实操作流程。
"""

import os
import io
import csv
import json
import tempfile
import shutil
import unittest
import sys
import sqlite3

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import models
from app import app


SCHEME_CREATE_JSON = json.dumps([
    {"key": "positive", "text": "正面", "color": "#22c55e"},
    {"key": "neutral", "text": "中性", "color": "#3b82f6"},
    {"key": "negative", "text": "负面", "color": "#ef4444"},
])


def make_csv_bytes(rows, fieldnames):
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    for r in rows:
        writer.writerow(r)
    return buf.getvalue().encode("utf-8-sig")


class LockAndUnknownLabelRegressionTests(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="anno_test_")
        self.test_db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = models.DB_PATH
        models.DB_PATH = self.test_db_path
        models.init_db()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()

    def tearDown(self):
        try:
            models.DB_PATH = self._orig_db_path
        except Exception:
            pass
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # ---------- helpers ----------

    def _login(self, username, password):
        resp = self.client.post("/login", data={
            "username": username, "password": password
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("仪表盘", page, f"登录失败? 用户名={username}")
        return resp

    def _create_scheme(self, name="情感分析"):
        resp = self.client.post("/schemes/new", data={
            "name": name,
            "description": "测试用方案",
            "labels_json": SCHEME_CREATE_JSON,
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        return resp

    def _get_active_scheme_id(self):
        conn = sqlite3.connect(models.DB_PATH)
        try:
            row = conn.execute(
                "SELECT id FROM label_schemes WHERE is_active=1 ORDER BY id DESC LIMIT 1"
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _query_count(self, table, where="1=1", params=()):
        conn = sqlite3.connect(models.DB_PATH)
        try:
            return conn.execute(
                f"SELECT COUNT(*) FROM {table} WHERE {where}", params
            ).fetchone()[0]
        finally:
            conn.close()

    # ---------- tests ----------

    def test_01_import_samples_locking_and_duplicates(self):
        """
        导入样本 CSV（含重复编号 + 正常行 + 格式错误行）:
        - 不抛 SQLite database is locked
        - 重复编号被跳过
        - 统计数量正确
        """
        self._login("admin", "admin123")
        self._create_scheme("测试样本")
        scheme_id = self._get_active_scheme_id()
        self.assertIsNotNone(scheme_id)

        samples_csv = make_csv_bytes([
            {"sample_id": "A001", "content": "商品非常好，物流很快", "source": "京东"},
            {"sample_id": "A002", "content": "包装有破损，还好商品没事", "source": "淘宝"},
            {"sample_id": "A001", "content": "这是重复编号应该被跳过", "source": "拼多多"},
            {"sample_id": "", "content": "缺编号", "source": ""},
            {"sample_id": "A003", "content": "整体体验一般", "source": "抖音"},
        ], ["sample_id", "content", "source"])

        data = {
            "scheme_id": str(scheme_id),
            "file": (io.BytesIO(samples_csv), "samples.csv"),
        }
        resp = self.client.post(
            "/samples/import", data=data,
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("样本", page)
        # 不能有锁错误
        self.assertNotIn("database is locked", page.lower())

        total = self._query_count("samples")
        unknown = self._query_count("samples", "sample_id='' OR sample_id IS NULL")
        self.assertEqual(total, 3, f"应导入3条正常样本（2个去重+1个空号跳过），实际={total}")
        self.assertEqual(unknown, 0, "缺编号的行不该入库")

    def test_02_import_annotations_unknown_labels_skipped(self):
        """
        导入标注 CSV（含未知标签 + 缺失样本 + 合法行）:
        - 不抛锁
        - 未知标签行不入库（不产生 is_unknown_label=1 的脏数据）
        - 缺失样本行不入库
        - flash 提示明确包含未知标签的样本编号和标签
        """
        self._login("admin", "admin123")
        self._create_scheme("标注测试")
        scheme_id = self._get_active_scheme_id()

        samples_csv = make_csv_bytes([
            {"sample_id": "B001", "content": "好评"},
            {"sample_id": "B002", "content": "中评"},
            {"sample_id": "B003", "content": "差评"},
            {"sample_id": "B004", "content": "非常不满意"},
        ], ["sample_id", "content"])
        self.client.post(
            "/samples/import",
            data={"scheme_id": str(scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        # 合法行 + 2 条未知标签 + 1 条缺失样本 + 1 条缺字段
        ann_csv = make_csv_bytes([
            {"sample_id": "B001", "label": "positive", "comment": "明确好评"},
            {"sample_id": "B002", "label": "neutral", "comment": "一般般"},
            {"sample_id": "B003", "label": "very_negative", "comment": "使用了未定义标签"},
            {"sample_id": "B004", "label": "unknown_label_xxx", "comment": "又一个未知标签"},
            {"sample_id": "NOT_EXIST", "label": "positive", "comment": "样本不存在"},
            {"sample_id": "", "label": "positive", "comment": "缺 sample_id"},
        ], ["sample_id", "label", "comment"])

        resp = self.client.post(
            "/annotations/import",
            data={
                "scheme_id": str(scheme_id),
                "annotator_id": "2",  # annotator1
                "file": (io.BytesIO(ann_csv), "ann.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertNotIn("database is locked", page.lower())

        total_ann = self._query_count("annotations")
        unknown_ann = self._query_count("annotations", "is_unknown_label=1")
        self.assertEqual(total_ann, 2,
                         f"应只入库2条合法标注(B001,B002)，实际={total_ann}")
        self.assertEqual(unknown_ann, 0,
                         "未知标签行完全不该入库（不应该存在 is_unknown_label=1 的行）")

        # flash 消息必须明确提示未知标签
        self.assertIn("未知标签被跳过", page)
        self.assertIn("B003", page)
        self.assertIn("very_negative", page)
        self.assertIn("缺失样本被跳过", page)
        self.assertIn("NOT_EXIST", page)

        # ---------- 验证标注列表页筛选"仅显示未知标签"结果为空 ----------
        resp = self.client.get(f"/annotations?scheme_id={scheme_id}&unknown_only=1")
        self.assertEqual(resp.status_code, 200)
        page_list = resp.data.decode("utf-8")
        self.assertIn("暂无标注数据", page_list,
                      "筛选未知标签时应显示空状态，因为未知标签不入库")

        # ---------- 验证导入页提示文案与实际行为一致 ----------
        resp = self.client.get("/annotations/import")
        self.assertEqual(resp.status_code, 200)
        page_import = resp.data.decode("utf-8")
        self.assertIn("未知标签不会被导入", page_import,
                      "导入页应明确提示未知标签不入库，而不是'被标记'")
        self.assertIn("该条会被跳过，不入库", page_import)

    def test_03_full_workflow_no_lock_no_unknown(self):
        """
        完整主链路: 造冲突数据 → 检测冲突 → 分配复核(回避+正常) → 完成复核 → 导出
        全程不允许 database is locked，并且未知标注不会进入冲突/导出。
        """
        self._login("admin", "admin123")
        self._create_scheme("完整流程")
        scheme_id = self._get_active_scheme_id()

        # ---------- 导入样本 ----------
        samples_csv = make_csv_bytes([
            {"sample_id": "C001", "content": "商品不错，值得买"},
            {"sample_id": "C002", "content": "一般般吧，还行"},
            {"sample_id": "C003", "content": "太差了，完全不值"},
        ], ["sample_id", "content"])
        self.client.post(
            "/samples/import",
            data={"scheme_id": str(scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        # ---------- annotator1 标注 ----------
        ann1 = make_csv_bytes([
            {"sample_id": "C001", "label": "positive", "comment": "甲:确实好"},
            {"sample_id": "C002", "label": "neutral", "comment": "甲:中性"},
            {"sample_id": "C003", "label": "negative", "comment": "甲:差评"},
        ], ["sample_id", "label", "comment"])
        resp = self.client.post(
            "/annotations/import",
            data={"scheme_id": str(scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann1), "a1.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        page = resp.data.decode("utf-8")
        self.assertNotIn("database is locked", page.lower())

        # ---------- annotator2 标注（制造冲突：C002 打成 positive），并塞1个未知 ----------
        ann2 = make_csv_bytes([
            {"sample_id": "C001", "label": "positive", "comment": "乙:一致好评"},
            {"sample_id": "C002", "label": "positive", "comment": "乙:其实算正面"},  # 冲突
            {"sample_id": "C003", "label": "bad_label_not_in_scheme", "comment": "乙:未知标签故意构造"},
        ], ["sample_id", "label", "comment"])
        resp = self.client.post(
            "/annotations/import",
            data={"scheme_id": str(scheme_id), "annotator_id": "3",
                  "file": (io.BytesIO(ann2), "a2.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        page = resp.data.decode("utf-8")
        self.assertNotIn("database is locked", page.lower())
        self.assertIn("未知标签被跳过", page)
        self.assertIn("C003", page)

        # C003 只有 annotator1 的1条标注（乙的未知标签被跳过了）
        cnt_c003 = self._query_count(
            "annotations",
            "sample_id = (SELECT id FROM samples WHERE sample_id='C003')"
        )
        self.assertEqual(cnt_c003, 1, "C003 应只有甲的1条合法标注（乙的未知标签已被跳过）")

        # ---------- 检测冲突 ----------
        resp = self.client.post(
            "/conflicts/detect",
            data={"scheme_id": str(scheme_id)},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertNotIn("database is locked", page.lower())

        conflicts_total = self._query_count("conflicts")
        # 预期只有 C002 冲突（C001 一致，C003 乙的未知标签被跳过了只剩1条，所以不冲突）
        self.assertEqual(conflicts_total, 1, f"预期只有C002冲突，实际={conflicts_total}")

        # ---------- 回避机制：尝试把 C002 分配给 annotator2 当复核员（他不是reviewer角色，先拿reviewer1测自审回避） ----------
        # 先查冲突 id
        conn = sqlite3.connect(models.DB_PATH)
        conflict_id = conn.execute("SELECT id FROM conflicts LIMIT 1").fetchone()[0]

        # 先把 reviewer1 加入 C002 的标注员（模拟自审场景），验证回避
        c002_sample_id = conn.execute(
            "SELECT id FROM samples WHERE sample_id='C002'"
        ).fetchone()[0]
        # reviewer1 id=4，我们给 C002 加1条 reviewer1 的标注（假装他参与过标注），然后尝试分配
        conn.execute(
            "INSERT INTO annotations (sample_id, annotator_id, scheme_id, label_id, label_key, label_text, is_unknown_label) "
            "SELECT ?, 4, ?, id, label_key, label_text, 0 FROM labels WHERE scheme_id=? AND label_key='neutral' LIMIT 1",
            (c002_sample_id, scheme_id, scheme_id),
        )
        fake_ann_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        conn.execute(
            "INSERT INTO conflict_annotations (conflict_id, annotation_id) VALUES (?, ?)",
            (conflict_id, fake_ann_id),
        )
        conn.commit()
        conn.close()

        resp = self.client.post(
            f"/conflicts/{conflict_id}/assign",
            data={"reviewer_id": "4"},  # reviewer1，他现在也有C002标注
            follow_redirects=True,
        )
        page = resp.data.decode("utf-8")
        self.assertIn("不能复核自己参与标注", page,
                      "回避机制未生效：reviewer1有C002标注，却被允许分配")
        pending_cnt = self._query_count("review_tasks", "conflict_id=?", (conflict_id,))
        self.assertEqual(pending_cnt, 0, "回避失败时不该写入复核任务")

        # ---------- 正常分配（给 reviewer2=id 5，他没参与标注） ----------
        resp = self.client.post(
            f"/conflicts/{conflict_id}/assign",
            data={"reviewer_id": "5"},
            follow_redirects=True,
        )
        page = resp.data.decode("utf-8")
        self.assertNotIn("database is locked", page.lower())
        self.assertIn("已分配给", page)

        # ---------- reviewer2 登录完成复核 ----------
        self.client.get("/logout")
        self._login("reviewer2", "review123")
        task_id = self._query_count("review_tasks")  # 只有1条
        self.assertGreaterEqual(task_id, 1)
        # 查任务 id
        conn = sqlite3.connect(models.DB_PATH)
        task_row = conn.execute(
            "SELECT rt.id, l.id as label_id "
            "FROM review_tasks rt, labels l "
            "WHERE rt.conflict_id=? AND l.scheme_id=? AND l.label_key='neutral' LIMIT 1",
            (conflict_id, scheme_id),
        ).fetchone()
        conn.close()
        self.assertIsNotNone(task_row)
        task_id = task_row[0]
        decision_label_id = task_row[1]

        resp = self.client.post(
            f"/reviews/{task_id}",
            data={"label_id": str(decision_label_id), "comment": "复核决定：中性"},
            follow_redirects=True,
        )
        page = resp.data.decode("utf-8")
        self.assertNotIn("database is locked", page.lower())
        self.assertIn("复核完成", page)

        resolved = self._query_count("conflicts", "status='resolved'")
        self.assertEqual(resolved, 1)

        # ---------- 导出证据 ----------
        self.client.get("/logout")
        self._login("admin", "admin123")

        resp = self.client.post(
            "/export/evidence",
            data={"scheme_id": str(scheme_id)},
        )
        self.assertEqual(resp.status_code, 200)
        content = resp.data.decode("utf-8-sig")
        # 导出里不能出现 bad_label_not_in_scheme 或 is_unknown_label 提示
        self.assertNotIn("bad_label_not_in_scheme", content)
        self.assertNotIn("未知标签", content)
        # 应该有 C001、C002、C003 三行样本，且都有合法标签
        self.assertIn("C001", content)
        self.assertIn("C002", content)
        self.assertIn("C003", content)

        # JSON 导出也验证
        resp = self.client.post(
            "/export/json",
            data={"scheme_id": str(scheme_id)},
        )
        self.assertEqual(resp.status_code, 200)
        data = json.loads(resp.data.decode("utf-8"))
        # 所有 annotation 必须是合法标签（没有 is_unknown_label=1 且 都能匹配到标签）
        for s in data["samples"]:
            for a in s["annotations"]:
                self.assertEqual(a.get("is_unknown_label", 0), 0,
                                 f"导出里不该有未知标签行: {a}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
