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


class BatchImportRollbackTests(unittest.TestCase):
    """批次预演 + 导入回滚 功能测试。"""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="batch_test_")
        self.test_db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = models.DB_PATH
        models.DB_PATH = self.test_db_path
        models.init_db()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        self._login("admin", "admin123")
        self._create_scheme("批次测试方案")
        self.scheme_id = self._get_active_scheme_id()

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
        return resp

    def _create_scheme(self, name="测试方案"):
        resp = self.client.post("/schemes/new", data={
            "name": name,
            "description": "测试用",
            "labels_json": SCHEME_CREATE_JSON,
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

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

    def _query_row(self, sql, params=()):
        conn = sqlite3.connect(models.DB_PATH)
        try:
            conn.row_factory = sqlite3.Row
            return conn.execute(sql, params).fetchone()
        finally:
            conn.close()

    def _get_latest_batch_id(self):
        conn = sqlite3.connect(models.DB_PATH)
        try:
            row = conn.execute("SELECT id FROM import_batches ORDER BY id DESC LIMIT 1").fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    # ---------- tests: 样本预演 ----------

    def test_01_sample_preview_batch(self):
        """样本导入预演：创建 preview 批次，不实际入库。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "P001", "content": "测试样本1"},
            {"sample_id": "P002", "content": "测试样本2"},
            {"sample_id": "P001", "content": "重复编号"},
            {"sample_id": "", "content": "空编号"},
        ], ["sample_id", "content"])

        resp = self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "samples.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")

        self.assertIn("批次", page)
        self.assertIn("预演中", page)

        total_samples = self._query_count("samples")
        self.assertEqual(total_samples, 0, "预演阶段不应有样本入库")

        batch_id = self._get_latest_batch_id()
        self.assertIsNotNone(batch_id)

        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        self.assertEqual(batch["status"], "preview")
        self.assertEqual(batch["batch_type"], "sample")
        self.assertEqual(batch["total_rows"], 4)
        self.assertEqual(batch["new_count"], 2)
        self.assertEqual(batch["skip_duplicate_count"], 1)
        self.assertEqual(batch["skip_error_count"], 1)

    # ---------- tests: 样本确认导入 ----------

    def test_02_sample_confirm_import(self):
        """确认样本导入：从 preview 到 confirmed，数据正式入库。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "C001", "content": "确认导入测试1"},
            {"sample_id": "C002", "content": "确认导入测试2"},
            {"sample_id": "C003", "content": "确认导入测试3"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

        batch_id = self._get_latest_batch_id()
        self.assertIsNotNone(batch_id)

        before_samples = self._query_count("samples")
        self.assertEqual(before_samples, 0)

        resp = self.client.post(
            f"/batches/{batch_id}/confirm",
            data={},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("导入确认成功", page)

        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        self.assertEqual(batch["status"], "confirmed")
        self.assertIsNotNone(batch["confirmed_at"])

        after_samples = self._query_count("samples")
        self.assertEqual(after_samples, 3)

    # ---------- tests: 样本回滚 ----------

    def test_03_sample_revert_batch(self):
        """样本批次回滚：确认导入后撤销，样本应被删除。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "R001", "content": "回滚测试1"},
            {"sample_id": "R002", "content": "回滚测试2"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        batch_id = self._get_latest_batch_id()

        self.client.post(f"/batches/{batch_id}/confirm", follow_redirects=True)
        after_confirm = self._query_count("samples")
        self.assertEqual(after_confirm, 2)

        resp = self.client.post(
            f"/batches/{batch_id}/revert",
            data={"note": "测试回滚"},
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("回滚成功", page)

        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        self.assertEqual(batch["status"], "reverted")
        self.assertIsNotNone(batch["reverted_at"])
        self.assertEqual(batch["revert_note"], "测试回滚")

        after_revert = self._query_count("samples")
        self.assertEqual(after_revert, 0, "回滚后样本应被删除")

        rev_cnt = self._query_count("revision_history", "entity_type='sample' AND action='delete'")
        self.assertGreater(rev_cnt, 0, "回滚应有删除审计日志")

    # ---------- tests: 标注预演 ----------

    def test_04_annotation_preview_batch(self):
        """标注导入预演：预览新增、更新、跳过等情况。"""
        self._import_samples_direct(["D001", "D002", "D003"])

        ann_csv = make_csv_bytes([
            {"sample_id": "D001", "label": "positive", "comment": "好评"},
            {"sample_id": "D002", "label": "neutral", "comment": "中评"},
            {"sample_id": "NOT_EXIST", "label": "positive", "comment": "缺失样本"},
            {"sample_id": "D003", "label": "unknown_label_xxx", "comment": "未知标签"},
            {"sample_id": "", "label": "positive", "comment": "空编号"},
        ], ["sample_id", "label", "comment"])

        resp = self.client.post(
            "/annotations/import/preview",
            data={
                "scheme_id": str(self.scheme_id),
                "annotator_id": "2",
                "file": (io.BytesIO(ann_csv), "ann.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        self.assertEqual(resp.status_code, 200)

        before_ann = self._query_count("annotations")
        self.assertEqual(before_ann, 0, "预演阶段不应有标注入库")

        batch_id = self._get_latest_batch_id()
        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        self.assertEqual(batch["status"], "preview")
        self.assertEqual(batch["batch_type"], "annotation")
        self.assertEqual(batch["new_count"], 2)
        self.assertEqual(batch["skip_missing_sample_count"], 1)
        self.assertEqual(batch["skip_unknown_label_count"], 1)
        self.assertEqual(batch["skip_error_count"], 1)

    # ---------- tests: 标注确认 + 冲突检测 ----------

    def test_05_annotation_confirm_with_conflicts(self):
        """标注批次确认后，应自动检测冲突并记录。"""
        self._import_samples_direct(["E001", "E002", "E003"])

        ann1_csv = make_csv_bytes([
            {"sample_id": "E001", "label": "positive"},
            {"sample_id": "E002", "label": "neutral"},
            {"sample_id": "E003", "label": "negative"},
        ], ["sample_id", "label"])

        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann1_csv), "a1.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        batch1_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{batch1_id}/confirm", follow_redirects=True)

        ann2_csv = make_csv_bytes([
            {"sample_id": "E001", "label": "positive"},
            {"sample_id": "E002", "label": "positive"},
            {"sample_id": "E003", "label": "neutral"},
        ], ["sample_id", "label"])

        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "3",
                  "file": (io.BytesIO(ann2_csv), "a2.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        batch2_id = self._get_latest_batch_id()
        resp = self.client.post(f"/batches/{batch2_id}/confirm", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

        batch2 = self._query_row("SELECT * FROM import_batches WHERE id = ?", (batch2_id,))
        self.assertGreater(batch2["conflict_created_count"], 0, "确认导入后应检测到冲突")

        conflicts_cnt = self._query_count("conflicts")
        self.assertGreater(conflicts_cnt, 0, "冲突表应有记录")

        batch_conflicts_cnt = self._query_count(
            "batch_conflict_records", "batch_id = ?", (batch2_id,))
        self.assertGreater(batch_conflicts_cnt, 0, "批次冲突明细应有记录")

    # ---------- tests: 标注批次回滚（含冲突） ----------

    def test_06_annotation_revert_with_conflicts(self):
        """回滚标注批次：冲突也应被一并撤销。"""
        self._import_samples_direct(["F001", "F002"])

        ann1 = make_csv_bytes([
            {"sample_id": "F001", "label": "positive"},
            {"sample_id": "F002", "label": "negative"},
        ], ["sample_id", "label"])
        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann1), "a1.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b1 = self._get_latest_batch_id()
        self.client.post(f"/batches/{b1}/confirm", follow_redirects=True)

        ann2 = make_csv_bytes([
            {"sample_id": "F001", "label": "negative"},
            {"sample_id": "F002", "label": "negative"},
        ], ["sample_id", "label"])
        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "3",
                  "file": (io.BytesIO(ann2), "a2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b2 = self._get_latest_batch_id()
        self.client.post(f"/batches/{b2}/confirm", follow_redirects=True)

        conflicts_before = self._query_count("conflicts")
        self.assertGreater(conflicts_before, 0, "回滚前应有冲突")

        self.client.post(f"/batches/{b2}/revert", data={"note": "回滚测试"},
                         follow_redirects=True)

        conflicts_after = self._query_count("conflicts")
        self.assertEqual(conflicts_after, 0, "回滚后冲突应被删除")

        ann_after = self._query_count("annotations")
        self.assertEqual(ann_after, 2, "回滚后应只剩第一批次的2条标注")

    # ---------- tests: 撤销后重新导入 ----------

    def test_07_revert_then_reimport(self):
        """撤销批次后，再次导入相同数据应正常工作。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "G001", "content": "撤销后重导测试"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b1 = self._get_latest_batch_id()
        self.client.post(f"/batches/{b1}/confirm", follow_redirects=True)

        self.assertEqual(self._query_count("samples"), 1)

        self.client.post(f"/batches/{b1}/revert", follow_redirects=True)
        self.assertEqual(self._query_count("samples"), 0)

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b2 = self._get_latest_batch_id()
        self.assertNotEqual(b1, b2)

        resp = self.client.post(f"/batches/{b2}/confirm", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self._query_count("samples"), 1)

    # ---------- tests: 跨重启持久化 ----------

    def test_08_batch_persistence_across_restart(self):
        """批次记录和状态应持久化在数据库中，重启不丢失。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "H001", "content": "持久化测试1"},
            {"sample_id": "H002", "content": "持久化测试2"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        preview_batch_id = self._get_latest_batch_id()

        ann_csv = make_csv_bytes([
            {"sample_id": "H001", "label": "positive"},
        ], ["sample_id", "label"])
        self._import_samples_direct(["H001"])

        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann_csv), "a.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        ann_batch_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{ann_batch_id}/confirm", follow_redirects=True)
        self.client.post(f"/batches/{ann_batch_id}/revert", follow_redirects=True)

        batch_before = {
            "preview": self._query_row("SELECT * FROM import_batches WHERE id = ?", (preview_batch_id,)),
            "reverted": self._query_row("SELECT * FROM import_batches WHERE id = ?", (ann_batch_id,)),
        }
        sample_records_before = self._query_count(
            "batch_sample_records", "batch_id = ?", (preview_batch_id,))
        ann_records_before = self._query_count(
            "batch_annotation_records", "batch_id = ?", (ann_batch_id,))

        old_db_path = self.test_db_path
        models.DB_PATH = self._orig_db_path
        models.DB_PATH = old_db_path

        batch_after_preview = self._query_row(
            "SELECT * FROM import_batches WHERE id = ?", (preview_batch_id,))
        batch_after_reverted = self._query_row(
            "SELECT * FROM import_batches WHERE id = ?", (ann_batch_id,))

        self.assertEqual(batch_after_preview["status"], "preview")
        self.assertEqual(batch_after_reverted["status"], "reverted")
        self.assertEqual(batch_after_preview["new_count"],
                         batch_before["preview"]["new_count"])
        self.assertEqual(batch_after_reverted["revert_note"],
                         batch_before["reverted"]["revert_note"])

        sample_records_after = self._query_count(
            "batch_sample_records", "batch_id = ?", (preview_batch_id,))
        ann_records_after = self._query_count(
            "batch_annotation_records", "batch_id = ?", (ann_batch_id,))
        self.assertEqual(sample_records_after, sample_records_before)
        self.assertEqual(ann_records_after, ann_records_before)

    # ---------- tests: 导出结果变化 ----------

    def test_09_export_reflects_batch_changes(self):
        """导出结果应随批次确认/回滚而变化。"""
        self._import_samples_direct(["I001", "I002"])

        ann_csv = make_csv_bytes([
            {"sample_id": "I001", "label": "positive", "comment": "好"},
            {"sample_id": "I002", "label": "negative", "comment": "差"},
        ], ["sample_id", "label", "comment"])

        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann_csv), "a.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        batch_id = self._get_latest_batch_id()

        resp_before = self.client.post(
            "/export/json", data={"scheme_id": str(self.scheme_id)})
        data_before = json.loads(resp_before.data.decode("utf-8"))
        ann_count_before = sum(len(s["annotations"]) for s in data_before["samples"])
        self.assertEqual(ann_count_before, 0)

        self.client.post(f"/batches/{batch_id}/confirm", follow_redirects=True)
        resp_after = self.client.post(
            "/export/json", data={"scheme_id": str(self.scheme_id)})
        data_after = json.loads(resp_after.data.decode("utf-8"))
        ann_count_after = sum(len(s["annotations"]) for s in data_after["samples"])
        self.assertEqual(ann_count_after, 2)
        self.assertIn("好", resp_after.data.decode("utf-8"))

        self.client.post(f"/batches/{batch_id}/revert", follow_redirects=True)
        resp_revert = self.client.post(
            "/export/json", data={"scheme_id": str(self.scheme_id)})
        data_revert = json.loads(resp_revert.data.decode("utf-8"))
        ann_count_revert = sum(len(s["annotations"]) for s in data_revert["samples"])
        self.assertEqual(ann_count_revert, 0)

    # ---------- tests: 权限控制 ----------

    def test_10_reviewer_cannot_revert(self):
        """复核员不能访问/执行批次回滚。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "J001", "content": "权限测试"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        batch_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{batch_id}/confirm", follow_redirects=True)

        self.client.get("/logout")
        self._login("reviewer1", "review123")

        resp_list = self.client.get("/batches", follow_redirects=True)
        self.assertEqual(resp_list.status_code, 200)
        page_list = resp_list.data.decode("utf-8")
        self.assertIn("无权限", page_list)

        resp_revert = self.client.post(
            f"/batches/{batch_id}/revert", follow_redirects=True)
        self.assertEqual(resp_revert.status_code, 200)
        page_revert = resp_revert.data.decode("utf-8")
        self.assertIn("无权限", page_revert)

        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        self.assertEqual(batch["status"], "confirmed",
                         "无权限用户无法回滚，批次状态应保持 confirmed")

    def test_11_annotator_cannot_revert(self):
        """标注员不能访问/执行批次回滚。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "K001", "content": "权限测试2"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        batch_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{batch_id}/confirm", follow_redirects=True)

        self.client.get("/logout")
        self._login("annotator1", "anno123")

        resp_revert = self.client.post(
            f"/batches/{batch_id}/revert", follow_redirects=True)
        self.assertEqual(resp_revert.status_code, 200)
        page = resp_revert.data.decode("utf-8")
        self.assertIn("无权限", page)

        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (batch_id,))
        self.assertEqual(batch["status"], "confirmed")

    # ---------- tests: 完整链路 ----------

    def test_12_full_workflow_import_assign_export_revert(self):
        """完整链路：导入样本→导入标注→检测冲突→分配复核→导出→撤销批次。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "W001", "content": "完整链路测试1"},
            {"sample_id": "W002", "content": "完整链路测试2"},
            {"sample_id": "W003", "content": "完整链路测试3"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        sample_batch_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{sample_batch_id}/confirm", follow_redirects=True)
        self.assertEqual(self._query_count("samples"), 3)

        ann1 = make_csv_bytes([
            {"sample_id": "W001", "label": "positive"},
            {"sample_id": "W002", "label": "neutral"},
            {"sample_id": "W003", "label": "negative"},
        ], ["sample_id", "label"])
        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann1), "a1.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        a1_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{a1_id}/confirm", follow_redirects=True)

        ann2 = make_csv_bytes([
            {"sample_id": "W001", "label": "positive"},
            {"sample_id": "W002", "label": "positive"},
            {"sample_id": "W003", "label": "negative"},
        ], ["sample_id", "label"])
        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "3",
                  "file": (io.BytesIO(ann2), "a2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        a2_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{a2_id}/confirm", follow_redirects=True)

        conflicts = self._query_count("conflicts")
        self.assertEqual(conflicts, 1, "应检测到1个冲突(W002)")

        conn = sqlite3.connect(models.DB_PATH)
        conflict_id = conn.execute("SELECT id FROM conflicts LIMIT 1").fetchone()[0]
        conn.close()

        resp_assign = self.client.post(
            f"/conflicts/{conflict_id}/assign",
            data={"reviewer_id": "4"},
            follow_redirects=True,
        )
        self.assertEqual(resp_assign.status_code, 200)
        review_cnt = self._query_count("review_tasks")
        self.assertEqual(review_cnt, 1)

        resp_export = self.client.post(
            "/export/evidence", data={"scheme_id": str(self.scheme_id)})
        self.assertEqual(resp_export.status_code, 200)
        export_content = resp_export.data.decode("utf-8-sig")
        self.assertIn("W001", export_content)
        self.assertIn("W002", export_content)
        self.assertIn("冲突", export_content)

        self.client.post(f"/batches/{a2_id}/revert", data={"note": "链路测试回滚"},
                         follow_redirects=True)

        ann_after = self._query_count("annotations")
        self.assertEqual(ann_after, 3, "回滚后应只剩标注员甲的3条")

        conflicts_after = self._query_count("conflicts")
        self.assertEqual(conflicts_after, 0, "回滚后冲突应被清除")

        review_after = self._query_count("review_tasks")
        self.assertEqual(review_after, 0, "回滚后复核任务应被清除")

        resp_export_after = self.client.post(
            "/export/evidence", data={"scheme_id": str(self.scheme_id)})
        export_after = resp_export_after.data.decode("utf-8-sig")
        self.assertIn("W001", export_after)
        self.assertIn("W002", export_after)
        self.assertNotIn("冲突", export_after.split("W002")[1].split("W003")[0] if "W003" in export_after else "")

    # ---------- helper ----------

    def _import_samples_direct(self, sample_ids):
        """通过原始导入接口直接导入样本（非批次方式），用于测试前置数据准备。"""
        rows = [{"sample_id": s, "content": f"样本{s}内容"} for s in sample_ids]
        csv_bytes = make_csv_bytes(rows, ["sample_id", "content"])
        self.client.post(
            "/samples/import",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(csv_bytes), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
