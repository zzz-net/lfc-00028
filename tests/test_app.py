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


class BatchCenterAdvancedTests(unittest.TestCase):
    """批次核对与回放中心 - 高级回归测试。

    覆盖：
    - 重复预检统计一致性
    - 撤回后重导统计一致
    - 权限拦截（预检、重复预检、撤回、确认均需管理员）
    - 导出摘要变化追踪
    - 跨重启状态恢复
    - skip_duplicate_count 正确统计
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="batch_center_")
        self.test_db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = models.DB_PATH
        models.DB_PATH = self.test_db_path
        models.init_db()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        self._login("admin", "admin123")
        self._create_scheme("高级测试方案")
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

    # ---------- 1. skip_duplicate_count 正确性测试 ----------

    def test_01_skip_duplicate_count_annotation(self):
        """标注预检中 skip_duplicate_count 应正确统计（包括批次内重复 + DB重复）。"""
        self._import_samples_direct(["T001", "T002", "T003"])

        ann_csv = make_csv_bytes([
            {"sample_id": "T001", "label": "positive", "comment": "a"},
            {"sample_id": "T001", "label": "neutral", "comment": "b"},
            {"sample_id": "T002", "label": "negative", "comment": "c"},
        ], ["sample_id", "label", "comment"])
        self.client.post(
            "/annotations/import/preview",
            data={
                "scheme_id": str(self.scheme_id),
                "annotator_id": "2",
                "file": (io.BytesIO(ann_csv), "dup_test.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b1 = self._get_latest_batch_id()
        self.client.post(f"/batches/{b1}/confirm", follow_redirects=True)

        ann_csv2 = make_csv_bytes([
            {"sample_id": "T001", "label": "positive", "comment": "a"},
            {"sample_id": "T001", "label": "positive", "comment": "a"},
            {"sample_id": "T003", "label": "neutral", "comment": "new"},
        ], ["sample_id", "label", "comment"])

        self.client.post(
            "/annotations/import/preview",
            data={
                "scheme_id": str(self.scheme_id),
                "annotator_id": "2",
                "file": (io.BytesIO(ann_csv2), "dup_test2.csv"),
            },
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b2 = self._get_latest_batch_id()
        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b2,))

        self.assertEqual(batch["skip_duplicate_count"], 2,
                         "应有2条重复：1条DB完全相同(T001+positive+a，加上1条批次内重复")
        self.assertEqual(batch["new_count"], 1, "应有1条新增(T003)")

    # ---------- 2. 重复预检功能 ----------

    def test_02_replay_preview_sample_batch(self):
        """对样本批次执行重复预检，应生成新 preview 批次且统计一致。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "R001", "content": "重复预检测试1"},
            {"sample_id": "R002", "content": "重复预检测试2"},
            {"sample_id": "R002", "content": "重复"},
            {"sample_id": "", "content": "空编号"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b1 = self._get_latest_batch_id()
        orig = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b1,))

        resp = self.client.post(f"/batches/{b1}/replay_preview", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("重复预检完成", page)

        b2 = self._get_latest_batch_id()
        self.assertNotEqual(b1, b2)
        replay = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b2,))
        self.assertEqual(replay["status"], "preview")
        self.assertEqual(replay["total_rows"], orig["total_rows"])
        self.assertEqual(replay["new_count"], orig["new_count"])
        self.assertEqual(replay["skip_duplicate_count"], orig["skip_duplicate_count"])
        self.assertEqual(replay["skip_error_count"], orig["skip_error_count"])
        self.assertIn("(重复预检)", replay["file_name"])

        replay_preview = json.loads(replay["preview_data"])
        self.assertEqual(replay_preview.get("replay_of_batch_id"), b1)

    def test_03_replay_preview_annotation_batch(self):
        """对标注批次执行重复预检。"""
        self._import_samples_direct(["X001", "X002"])

        ann_csv = make_csv_bytes([
            {"sample_id": "X001", "label": "positive"},
            {"sample_id": "X002", "label": "neutral"},
            {"sample_id": "NOT_EXIST", "label": "positive"},
            {"sample_id": "X001", "label": "UNKNOWN_LABEL"},
        ], ["sample_id", "label"])

        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann_csv), "a.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b1 = self._get_latest_batch_id()
        orig = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b1,))

        self.client.post(f"/batches/{b1}/replay_preview", follow_redirects=True)
        b2 = self._get_latest_batch_id()
        replay = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b2,))

        self.assertEqual(replay["total_rows"], orig["total_rows"])
        self.assertEqual(replay["new_count"], orig["new_count"])
        self.assertEqual(replay["skip_unknown_label_count"], orig["skip_unknown_label_count"])
        self.assertEqual(replay["skip_missing_sample_count"], orig["skip_missing_sample_count"])

    # ---------- 3. 撤回后重导统计一致性 ----------

    def test_04_revert_then_reimport_stats_consistent(self):
        """撤回批次后重新导入同一文件，预检统计应一致（重复项、跳过项数量一致）。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "Y001", "content": "一致性测试1"},
            {"sample_id": "Y002", "content": "一致性测试2"},
        ], ["sample_id", "content"])

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s1.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b1 = self._get_latest_batch_id()
        self.client.post(f"/batches/{b1}/confirm", follow_redirects=True)
        first = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b1,))

        self.assertEqual(self._query_count("samples"), 2)

        self.client.post(f"/batches/{b1}/revert", follow_redirects=True)
        self.assertEqual(self._query_count("samples"), 0)

        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s2.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b2 = self._get_latest_batch_id()
        second = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b2,))

        self.assertEqual(first["new_count"], second["new_count"],
                    "撤回后重导，new_count应一致")
        self.assertEqual(first["skip_duplicate_count"], second["skip_duplicate_count"],
                         "撤回后重导，skip_duplicate_count应一致")
        self.assertEqual(first["skip_error_count"], second["skip_error_count"],
                         "撤回后重导，skip_error_count应一致")
        self.assertEqual(first["total_rows"], second["total_rows"])

        self.client.post(f"/batches/{b2}/confirm", follow_redirects=True)
        self.assertEqual(self._query_count("samples"), 2)

    def test_05_annotation_revert_reimport_consistency(self):
        """标注：撤回后重导，重复/跳过统计一致。"""
        self._import_samples_direct(["Z001", "Z002"])

        ann_csv = make_csv_bytes([
            {"sample_id": "Z001", "label": "positive", "comment": "test1"},
            {"sample_id": "Z002", "label": "neutral", "comment": "test2"},
            {"sample_id": "NOT_HERE", "label": "positive", "comment": ""},
        ], ["sample_id", "label", "comment"])

        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann_csv), "ann1.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b1 = self._get_latest_batch_id()
        self.client.post(f"/batches/{b1}/confirm", follow_redirects=True)
        first = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b1,))
        self.assertEqual(self._query_count("annotations"), 2)

        self.client.post(f"/batches/{b1}/revert", follow_redirects=True)
        self.assertEqual(self._query_count("annotations"), 0)

        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann_csv), "ann2.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b2 = self._get_latest_batch_id()
        second = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b2,))

        self.assertEqual(first["new_count"], second["new_count"])
        self.assertEqual(first["update_count"], second["update_count"])
        self.assertEqual(first["skip_duplicate_count"], second["skip_duplicate_count"])
        self.assertEqual(first["skip_missing_sample_count"], second["skip_missing_sample_count"])
        self.assertEqual(first["skip_error_count"], second["skip_error_count"])
        self.assertEqual(first["total_rows"], second["total_rows"])

    # ---------- 4. 权限拦截 ----------

    def test_06_annotator_cannot_preview_import(self):
        """标注员不应能访问样本/标注预检接口（收紧后的管理员权限）。"""
        self.client.get("/logout")
        self._login("annotator1", "anno123")

        samples_csv = make_csv_bytes([
            {"sample_id": "P001", "content": "权限测试样本"}], ["sample_id", "content"])
        resp = self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        page = resp.data.decode("utf-8")
        self.assertIn("无权限", page)
        self.assertEqual(self._query_count("import_batches", "batch_type='sample'"), 0)

        self._import_samples_direct(["P001"])

        ann_csv = make_csv_bytes([
            {"sample_id": "P001", "label": "positive"}], ["sample_id", "label"])
        resp = self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann_csv), "a.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        page = resp.data.decode("utf-8")
        self.assertIn("无权限", page)
        self.assertEqual(self._query_count("import_batches", "batch_type='annotation'"), 0)

    def test_07_reviewer_cannot_replay_or_revert(self):
        """复核员不能执行重复预检和撤回。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "PERM001", "content": "权限测试"}], ["sample_id", "content"])
        self._login("admin", "admin123")
        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b = self._get_latest_batch_id()
        self.client.post(f"/batches/{b}/confirm", follow_redirects=True)

        self.client.get("/logout")
        self._login("reviewer1", "review123")

        resp_replay = self.client.post(f"/batches/{b}/replay_preview", follow_redirects=True)
        self.assertIn("无权限", resp_replay.data.decode("utf-8"))

        resp_revert = self.client.post(f"/batches/{b}/revert", follow_redirects=True)
        self.assertIn("无权限", resp_revert.data.decode("utf-8"))

        batch_after = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b,))
        self.assertEqual(batch_after["status"], "confirmed")

    def test_08_annotator_cannot_confirm_or_replay(self):
        """标注员不能确认批次、不能重复预检。"""
        self._login("admin", "admin123")
        samples_csv = make_csv_bytes([
            {"sample_id": "PERM002", "content": "权限"}], ["sample_id", "content"])
        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        b = self._get_latest_batch_id()

        self.client.get("/logout")
        self._login("annotator1", "anno123")

        resp_confirm = self.client.post(f"/batches/{b}/confirm", follow_redirects=True)
        self.assertIn("无权限", resp_confirm.data.decode("utf-8"))

        resp_replay = self.client.post(f"/batches/{b}/replay_preview", follow_redirects=True)
        self.assertIn("无权限", resp_replay.data.decode("utf-8"))

    # ---------- 5. 导出摘要变化 ----------

    def test_09_preview_includes_export_summary(self):
        """预检数据中应包含导入前后导出摘要。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "EXP001", "content": "导出摘要1"},
            {"sample_id": "EXP002", "content": "导出摘要2"},
        ], ["sample_id", "content"])
        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b = self._get_latest_batch_id()
        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b,))
        preview = json.loads(batch["preview_data"])

        self.assertIn("export_summary_before", preview)
        self.assertIn("export_summary_after", preview)

        before = preview["export_summary_before"]
        after = preview["export_summary_after"]
        self.assertEqual(before["total_samples"], 0)
        self.assertEqual(after["total_samples"], 2)

    def test_10_annotation_export_summary_changes(self):
        """标注导入应反映标注数量变化。"""
        self._import_samples_direct(["EXP_A001", "EXP_A002", "EXP_A003"])
        ann1 = make_csv_bytes([
            {"sample_id": "EXP_A001", "label": "positive"},
            {"sample_id": "EXP_A002", "label": "neutral"},
        ], ["sample_id", "label"])
        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "2",
                  "file": (io.BytesIO(ann1), "a1.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b = self._get_latest_batch_id()
        batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b,))
        preview = json.loads(batch["preview_data"])
        self.assertEqual(preview["export_summary_before"]["total_annotations"], 0)
        self.assertEqual(preview["export_summary_after"]["total_annotations"], 2)

        self.client.post(f"/batches/{b}/confirm", follow_redirects=True)

        ann2 = make_csv_bytes([
            {"sample_id": "EXP_A003", "label": "negative"},
        ], ["sample_id", "label"])
        self.client.post(
            "/annotations/import/preview",
            data={"scheme_id": str(self.scheme_id), "annotator_id": "3",
                  "file": (io.BytesIO(ann2), "a2.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b2 = self._get_latest_batch_id()
        batch2 = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b2,))
        prev2 = json.loads(batch2["preview_data"])
        self.assertEqual(prev2["export_summary_before"]["total_annotations"], 2)
        self.assertEqual(prev2["export_summary_after"]["total_annotations"], 3)

    # ---------- 6. 跨重启恢复 ----------

    def test_11_batch_status_persists_and_recoverable(self):
        """批次状态(preview/confirmed/reverted) 跨重启后保持不变。"""
        samples_p = make_csv_bytes([
            {"sample_id": "REST001", "content": "rest1"}], ["sample_id", "content"])
        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_p), "s_p.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        preview_id = self._get_latest_batch_id()

        samples_c = make_csv_bytes([
            {"sample_id": "REST002", "content": "rest2"}], ["sample_id", "content"])
        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_c), "s_c.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        confirmed_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{confirmed_id}/confirm", follow_redirects=True)

        samples_r = make_csv_bytes([
            {"sample_id": "REST003", "content": "rest3"}], ["sample_id", "content"])
        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_r), "s_r.csv")},
            content_type="multipart/form-data", follow_redirects=True,
        )
        reverted_id = self._get_latest_batch_id()
        self.client.post(f"/batches/{reverted_id}/confirm", follow_redirects=True)
        self.client.post(f"/batches/{reverted_id}/revert", follow_redirects=True)

        before_states = {
            preview_id: self._query_row("SELECT * FROM import_batches WHERE id = ?", (preview_id,)),
            confirmed_id: self._query_row("SELECT * FROM import_batches WHERE id = ?", (confirmed_id,)),
            reverted_id: self._query_row("SELECT * FROM import_batches WHERE id = ?", (reverted_id,)),
        }
        before_details = {
            "sample_records_p": self._query_count("batch_sample_records", "batch_id=?", (preview_id,)),
            "sample_records_c": self._query_count("batch_sample_records", "batch_id=?", (confirmed_id,)),
            "sample_records_r": self._query_count("batch_sample_records", "batch_id=?", (reverted_id,)),
        }

        old = self.test_db_path
        models.DB_PATH = self._orig_db_path
        models.DB_PATH = old

        for bid, bstate in before_states.items():
            after = self._query_row("SELECT * FROM import_batches WHERE id = ?", (bid,))
            self.assertEqual(after["status"], bstate["status"])
            self.assertEqual(after["file_hash"], bstate["file_hash"])
            self.assertEqual(after["preview_data"], bstate["preview_data"])
            self.assertEqual(after["config_snapshot"], bstate["config_snapshot"])
            self.assertEqual(after["new_count"], bstate["new_count"])
            self.assertEqual(after["created_by"], bstate["created_by"])

        after_details = {
            "sample_records_p": self._query_count("batch_sample_records", "batch_id=?", (preview_id,)),
            "sample_records_c": self._query_count("batch_sample_records", "batch_id=?", (confirmed_id,)),
            "sample_records_r": self._query_count("batch_sample_records", "batch_id=?", (reverted_id,)),
        }
        self.assertEqual(after_details, before_details)

    # ---------- 7. 完整链路: 确认导入 → 重复预检 → 撤回 → 重导 ----------

    def test_12_full_chain_confirm_replay_revert_reimport(self):
        """完整链路跑通：确认导入→重复预检→撤回→重导。"""
        samples_csv = make_csv_bytes([
            {"sample_id": "FULL001", "content": "完整链路样本1"},
            {"sample_id": "FULL002", "content": "完整链路样本2"},
            {"sample_id": "", "content": "坏行"},
        ], ["sample_id", "content"]
        )

        # 第1步: 预检
        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "full.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b1 = self._get_latest_batch_id()
        self.assertEqual(self._query_count("samples"), 0)
        batch_p = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b1,))
        self.assertEqual(batch_p["status"], "preview")
        self.assertEqual(batch_p["new_count"], 2)
        self.assertEqual(batch_p["skip_error_count"], 1)

        # 第2步: 确认导入
        self.client.post(f"/batches/{b1}/confirm", follow_redirects=True)
        self.assertEqual(self._query_count("samples"), 2)
        batch_c = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b1,))
        self.assertEqual(batch_c["status"], "confirmed")

        # 第3步: 重复预检（基于已确认状态下仍可重复预检）
        self.client.post(f"/batches/{b1}/replay_preview", follow_redirects=True)
        b_replay = self._get_latest_batch_id()
        self.assertNotEqual(b_replay, b1)
        replay_batch = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b_replay,))

        self.assertEqual(replay_batch["new_count"], 0,
                         "已确认导入后重复预检，样本已存在，故new_count应为0")
        self.assertEqual(replay_batch["skip_duplicate_count"], 2,
                         "2条有效样本因已存在DB，应计为重复")
        self.assertEqual(replay_batch["skip_error_count"], 1)
        self.assertEqual(self._query_count("samples"), 2)

        # 第4步: 撤回
        self.client.post(f"/batches/{b1}/revert", follow_redirects=True)
        self.assertEqual(self._query_count("samples"), 0)
        batch_r = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b1,))
        self.assertEqual(batch_r["status"], "reverted")

        # 第5步: 撤回后再重复预检（基于撤回批次）
        self.client.post(f"/batches/{b1}/replay_preview", follow_redirects=True)
        b_replay2 = self._get_latest_batch_id()
        replay2 = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b_replay2,))
        self.assertEqual(replay2["new_count"], 2,
                         "撤回后重复预检，样本已不存在，new_count恢复为2")
        self.assertEqual(replay2["skip_duplicate_count"], 0)
        self.assertEqual(replay2["skip_error_count"], 1)

        # 第6步: 撤回后重导（上传同一文件 → 确认）
        self.client.post(
            "/samples/import/preview",
            data={"scheme_id": str(self.scheme_id),
                  "file": (io.BytesIO(samples_csv), "full_reimport.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )
        b_reimport = self._get_latest_batch_id()
        reimp = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b_reimport,))
        self.assertEqual(reimp["new_count"], 2)
        self.assertEqual(reimp["skip_error_count"], 1)
        self.assertEqual(reimp["total_rows"], 3)

        self.client.post(f"/batches/{b_reimport}/confirm", follow_redirects=True)
        self.assertEqual(self._query_count("samples"), 2)
        final = self._query_row("SELECT * FROM import_batches WHERE id = ?", (b_reimport,))
        self.assertEqual(final["status"], "confirmed")

        # 最终：批次数量应正确：b1(preview→confirmed→reverted), b_replay, b_replay2, b_reimport
        self.assertEqual(self._query_count("import_batches"), 4)


SCHEME_V1_JSON = json.dumps([
    {"key": "positive", "text": "正面", "color": "#22c55e"},
    {"key": "neutral", "text": "中性", "color": "#3b82f6"},
    {"key": "negative", "text": "负面", "color": "#ef4444"},
])

SCHEME_V2_JSON = json.dumps([
    {"key": "positive", "text": "积极", "color": "#22c55e"},
    {"key": "neutral", "text": "中性", "color": "#3b82f6"},
    {"key": "negative", "text": "消极", "color": "#ef4444"},
    {"key": "unknown", "text": "未知", "color": "#6b7280"},
])


class SchemeReleaseSandboxTests(unittest.TestCase):
    """标签方案发布沙箱 - 完整回归测试。

    覆盖：
    - 草稿预览与影响分析
    - 标签映射与策略选择
    - 未映射/重名标签处理
    - 正式发布与方案切换
    - 冲突重开策略
    - 发布后撤回
    - 撤回后按旧方案导出
    - 权限拦截
    - 跨重启状态恢复
    - 导出差异
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="scheme_release_")
        self.test_db_path = os.path.join(self.tmpdir, "test.db")
        self._orig_db_path = models.DB_PATH
        models.DB_PATH = self.test_db_path
        models.init_db()
        app.config["TESTING"] = True
        app.config["SECRET_KEY"] = "test-secret"
        app.config["WTF_CSRF_ENABLED"] = False
        self.client = app.test_client()
        self._login("admin", "admin123")

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

    def _logout(self):
        return self.client.get("/logout", follow_redirects=True)

    def _create_scheme(self, name, labels_json):
        resp = self.client.post("/schemes/new", data={
            "name": name,
            "description": "测试用",
            "labels_json": labels_json,
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)

    def _get_scheme_id_by_name_version(self, name, version):
        conn = sqlite3.connect(models.DB_PATH)
        try:
            row = conn.execute(
                "SELECT id FROM label_schemes WHERE name = ? AND version = ?",
                (name, version)
            ).fetchone()
            return row[0] if row else None
        finally:
            conn.close()

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

    def _get_latest_draft_id(self):
        conn = sqlite3.connect(models.DB_PATH)
        try:
            row = conn.execute("SELECT id FROM scheme_release_drafts ORDER BY id DESC LIMIT 1").fetchone()
            return row[0] if row else None
        finally:
            conn.close()

    def _import_samples(self, scheme_id, sample_ids):
        rows = [{"sample_id": s, "content": f"样本{s}内容"} for s in sample_ids]
        csv_bytes = make_csv_bytes(rows, ["sample_id", "content"])
        self.client.post(
            "/samples/import",
            data={"scheme_id": str(scheme_id),
                  "file": (io.BytesIO(csv_bytes), "s.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def _import_annotations(self, scheme_id, sample_id, label, annotator_id=2):
        ann_csv = make_csv_bytes([
            {"sample_id": sample_id, "label": label, "comment": f"标注{sample_id}"}
        ], ["sample_id", "label", "comment"])
        self.client.post(
            "/annotations/import",
            data={"scheme_id": str(scheme_id), "annotator_id": str(annotator_id),
                  "file": (io.BytesIO(ann_csv), "a.csv")},
            content_type="multipart/form-data",
            follow_redirects=True,
        )

    def _create_test_data(self, scheme_id):
        """创建测试数据：样本、标注、冲突、复核任务。"""
        self._import_samples(scheme_id, ["S001", "S002", "S003", "S004", "S005"])

        self._import_annotations(scheme_id, "S001", "positive", 2)
        self._import_annotations(scheme_id, "S001", "positive", 3)

        self._import_annotations(scheme_id, "S002", "neutral", 2)
        self._import_annotations(scheme_id, "S002", "positive", 3)

        self._import_annotations(scheme_id, "S003", "negative", 2)
        self._import_annotations(scheme_id, "S003", "negative", 3)

        self._import_annotations(scheme_id, "S004", "positive", 2)

        self._import_annotations(scheme_id, "S005", "negative", 2)
        self._import_annotations(scheme_id, "S005", "neutral", 3)

        self.client.post(
            "/conflicts/detect",
            data={"scheme_id": str(scheme_id)},
            follow_redirects=True,
        )

        conflict_id = self._query_row(
            "SELECT id FROM conflicts WHERE status = 'open' LIMIT 1"
        )
        if conflict_id:
            self.client.post(
                f"/conflicts/{conflict_id['id']}/assign",
                data={"reviewer_id": "4"},
                follow_redirects=True,
            )

    # ---------- 1. 草稿创建与影响分析测试 ----------

    def test_01_create_draft_and_analyze(self):
        """测试创建发布草稿并执行影响分析。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)

        self._create_test_data(v1_id)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        resp = self.client.post("/scheme-release/new", data={
            "name": "情感分析v2升级",
            "description": "升级标签方案，调整标签文本",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("发布草稿创建成功", page)

        draft_id = self._get_latest_draft_id()
        self.assertIsNotNone(draft_id)

        draft = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        self.assertEqual(draft["status"], "draft")
        self.assertEqual(draft["old_scheme_id"], v1_id)
        self.assertEqual(draft["new_scheme_id"], v2_id)

        resp = self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("影响分析完成", page)

        mappings = self._query_count("scheme_release_label_mappings", "draft_id=?", (draft_id,))
        self.assertGreater(mappings, 0, "应该生成标签映射")

        impact_items = self._query_count("scheme_release_impact_items", "draft_id=?", (draft_id,))
        self.assertGreater(impact_items, 0, "应该生成影响分析项")

        draft_updated = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        self.assertIsNotNone(draft_updated["impact_analysis"])
        self.assertIsNotNone(draft_updated["scheme_snapshot_old"])
        self.assertIsNotNone(draft_updated["scheme_snapshot_new"])

    # ---------- 2. 标签映射与策略选择测试 ----------

    def test_02_label_mapping_strategies(self):
        """测试标签映射和不同处理策略的保存。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "标签策略测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        mappings = self._query_row(
            "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? ORDER BY id LIMIT 1",
            (draft_id,)
        )
        self.assertIsNotNone(mappings)

        resp = self.client.post(
            f"/scheme-release/{draft_id}/mapping/{mappings['id']}/update",
            data={"strategy": "keep_old", "note": "测试沿用旧标签"},
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("映射策略已更新", page)

        updated = self._query_row(
            "SELECT * FROM scheme_release_label_mappings WHERE id = ?",
            (mappings["id"],)
        )
        self.assertEqual(updated["strategy"], "keep_old")
        self.assertEqual(updated["note"], "测试沿用旧标签")

    # ---------- 3. 未映射/重名标签检测测试 ----------

    def test_03_unmapped_and_duplicate_detection(self):
        """测试未映射标签和重名标签的检测。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        custom_v2 = json.dumps([
            {"key": "positive", "text": "正面评价", "color": "#22c55e"},
            {"key": "very_positive", "text": "非常正面", "color": "#15803d"},
        ])
        self._create_scheme("情感分析", custom_v2)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "未映射检测测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        draft = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        impact_analysis = json.loads(draft["impact_analysis"])

        self.assertGreater(impact_analysis["summary"]["unmapped_count"], 0,
                          "应该检测到未映射标签（neutral, negative）")
        self.assertGreater(impact_analysis["summary"]["duplicate_count"], 0,
                          "应该检测到重名标签（positive键同但文本不同）")
        self.assertGreater(impact_analysis["summary"]["prompt_count"], 0,
                          "应该有待确认策略的映射")

    # ---------- 4. 发布拦截测试（未确认策略不能发布） ----------

    def test_04_publish_blocked_by_pending_strategies(self):
        """测试存在未确认策略时发布被拦截。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "发布拦截测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        resp = self.client.post(
            f"/scheme-release/{draft_id}/publish",
            data={"operator_note": "尝试未确认就发布"},
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("存在未明确策略的标签映射", page)

        draft = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        self.assertEqual(draft["status"], "draft", "状态应该仍为草稿")

    # ---------- 5. 正式发布与方案切换测试 ----------

    def test_05_full_publish_workflow(self):
        """测试完整的发布流程：分析→设置策略→发布→验证切换。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "完整发布测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        mappings = self._query_row(
            "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
            (draft_id,)
        )
        while mappings:
            self.client.post(
                f"/scheme-release/{draft_id}/mapping/{mappings['id']}/update",
                data={"strategy": "use_new", "note": "自动设置"},
                follow_redirects=True
            )
            mappings = self._query_row(
                "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
                (draft_id,)
            )

        resp = self.client.post(
            f"/scheme-release/{draft_id}/publish",
            data={"operator_note": "正式发布v2方案"},
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("发布成功", page)

        draft = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        self.assertEqual(draft["status"], "published")
        self.assertIsNotNone(draft["published_at"])
        self.assertEqual(draft["operator_note"], "正式发布v2方案")

        active_scheme = self._get_active_scheme_id()
        self.assertEqual(active_scheme, v2_id, "新方案应该被激活")

        old_scheme = self._query_row(
            "SELECT is_active FROM label_schemes WHERE id = ?", (v1_id,)
        )
        self.assertEqual(old_scheme["is_active"], 0, "旧方案应该被停用")

        audit_logs = self._query_count("scheme_release_audit", "draft_id=? AND action='publish'", (draft_id,))
        self.assertEqual(audit_logs, 1, "应该有发布审计记录")

        revision_logs = self._query_count(
            "revision_history",
            "entity_type='label_scheme' AND action='release_publish'",
            ()
        )
        self.assertGreater(revision_logs, 0, "应该有修订历史记录")

    # ---------- 6. 冲突重开策略测试 ----------

    def test_06_conflict_reopen_strategy(self):
        """测试重开冲突策略的执行效果。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        conn = sqlite3.connect(models.DB_PATH)
        try:
            conn.execute(
                "UPDATE conflicts SET status = 'closed', resolved_at = CURRENT_TIMESTAMP "
                "WHERE scheme_id = ?",
                (v1_id,)
            )
            conn.commit()
        finally:
            conn.close()

        closed_conflicts_before = self._query_count(
            "conflicts", "scheme_id=? AND status='closed'", (v1_id,)
        )
        self.assertGreater(closed_conflicts_before, 0, "应该有关闭的冲突")

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "冲突重开测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        negative_mapping = self._query_row(
            "SELECT * FROM scheme_release_label_mappings "
            "WHERE draft_id = ? AND old_label_key = 'negative'",
            (draft_id,)
        )
        self.assertIsNotNone(negative_mapping)

        self.client.post(
            f"/scheme-release/{draft_id}/mapping/{negative_mapping['id']}/update",
            data={"strategy": "reopen", "note": "测试重开冲突"},
            follow_redirects=True
        )

        mappings = self._query_row(
            "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
            (draft_id,)
        )
        while mappings:
            self.client.post(
                f"/scheme-release/{draft_id}/mapping/{mappings['id']}/update",
                data={"strategy": "use_new", "note": "自动设置"},
                follow_redirects=True
            )
            mappings = self._query_row(
                "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
                (draft_id,)
            )

        self.client.post(
            f"/scheme-release/{draft_id}/publish",
            data={"operator_note": "发布测试重开"},
            follow_redirects=True
        )

        reopened_conflicts = self._query_count(
            "conflicts", "status='open'", ()
        )
        self.assertGreater(reopened_conflicts, 0, "应该有冲突被重开")

    # ---------- 7. 撤回发布与恢复测试 ----------

    def test_07_revert_release_and_restore(self):
        """测试发布后撤回，恢复旧方案。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "撤回测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        mappings = self._query_row(
            "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
            (draft_id,)
        )
        while mappings:
            self.client.post(
                f"/scheme-release/{draft_id}/mapping/{mappings['id']}/update",
                data={"strategy": "use_new", "note": "自动设置"},
                follow_redirects=True
            )
            mappings = self._query_row(
                "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
                (draft_id,)
            )

        self.client.post(
            f"/scheme-release/{draft_id}/publish",
            data={"operator_note": "先发布再撤回"},
            follow_redirects=True
        )

        resp = self.client.post(
            f"/scheme-release/{draft_id}/revert",
            data={"revert_note": "测试撤回，恢复旧方案"},
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("撤回成功", page)

        draft = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        self.assertEqual(draft["status"], "reverted")
        self.assertIsNotNone(draft["reverted_at"])
        self.assertEqual(draft["revert_note"], "测试撤回，恢复旧方案")

        active_scheme = self._get_active_scheme_id()
        self.assertEqual(active_scheme, v1_id, "撤回后旧方案应该被重新激活")

        audit_logs = self._query_count("scheme_release_audit", "draft_id=? AND action='revert'", (draft_id,))
        self.assertEqual(audit_logs, 1, "应该有撤回审计记录")

    # ---------- 8. 撤回后按旧方案导出测试 ----------

    def test_08_export_old_scheme_after_revert(self):
        """测试撤回后按旧方案导出证据。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "旧方案导出测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        mappings = self._query_row(
            "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
            (draft_id,)
        )
        while mappings:
            self.client.post(
                f"/scheme-release/{draft_id}/mapping/{mappings['id']}/update",
                data={"strategy": "use_new", "note": "自动设置"},
                follow_redirects=True
            )
            mappings = self._query_row(
                "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
                (draft_id,)
            )

        self.client.post(
            f"/scheme-release/{draft_id}/publish",
            data={"operator_note": "发布测试"},
            follow_redirects=True
        )
        self.client.post(
            f"/scheme-release/{draft_id}/revert",
            data={"revert_note": "撤回测试"},
            follow_redirects=True
        )

        resp = self.client.post(
            f"/scheme-release/{draft_id}/export/evidence",
            data={"sample_scheme_filter": "include_migrated"},
            follow_redirects=False
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.content_type)

        content = resp.data.decode("utf-8-sig")
        self.assertIn("旧方案快照", content)
        self.assertIn("情感分析 v1", content)
        self.assertIn("正面", content)
        self.assertIn("S001", content)

    # ---------- 9. 权限拦截测试 ----------

    def test_09_permission_interception(self):
        """测试非管理员用户访问发布沙箱被拦截。"""
        self._logout()
        self._login("annotator1", "anno123")

        resp = self.client.get("/scheme-release", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("无权限", page)

        resp = self.client.get("/scheme-release/new", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("无权限", page)

        self._logout()
        self._login("reviewer1", "review123")

        resp = self.client.get("/scheme-release", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("无权限", page)

        self._logout()
        self._login("admin", "admin123")
        resp = self.client.get("/scheme-release", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("方案发布沙箱", page)

    # ---------- 10. 跨重启状态恢复测试 ----------

    def test_10_state_recovery_after_restart(self):
        """测试服务重启后发布状态不混乱。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "跨重启测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        mappings = self._query_row(
            "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
            (draft_id,)
        )
        while mappings:
            self.client.post(
                f"/scheme-release/{draft_id}/mapping/{mappings['id']}/update",
                data={"strategy": "use_new", "note": "自动设置"},
                follow_redirects=True
            )
            mappings = self._query_row(
                "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
                (draft_id,)
            )

        self.client.post(
            f"/scheme-release/{draft_id}/publish",
            data={"operator_note": "发布测试重启"},
            follow_redirects=True
        )

        before_state = {
            "draft": self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,)),
            "active_scheme": self._get_active_scheme_id(),
            "mappings": self._query_count("scheme_release_label_mappings", "draft_id=?", (draft_id,)),
            "audit_count": self._query_count("scheme_release_audit", "draft_id=?", (draft_id,)),
            "samples_scheme": self._query_row(
                "SELECT scheme_id FROM samples LIMIT 1", ()
            )["scheme_id"] if self._query_count("samples") > 0 else None,
        }

        old = self.test_db_path
        models.DB_PATH = self._orig_db_path
        models.DB_PATH = old

        after_state = {
            "draft": self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,)),
            "active_scheme": self._get_active_scheme_id(),
            "mappings": self._query_count("scheme_release_label_mappings", "draft_id=?", (draft_id,)),
            "audit_count": self._query_count("scheme_release_audit", "draft_id=?", (draft_id,)),
            "samples_scheme": self._query_row(
                "SELECT scheme_id FROM samples LIMIT 1", ()
            )["scheme_id"] if self._query_count("samples") > 0 else None,
        }

        self.assertEqual(after_state["draft"]["status"], before_state["draft"]["status"])
        self.assertEqual(after_state["draft"]["published_at"], before_state["draft"]["published_at"])
        self.assertEqual(after_state["active_scheme"], before_state["active_scheme"])
        self.assertEqual(after_state["mappings"], before_state["mappings"])
        self.assertEqual(after_state["audit_count"], before_state["audit_count"])
        self.assertEqual(after_state["samples_scheme"], before_state["samples_scheme"])

    # ---------- 11. 导出差异测试 ----------

    def test_11_export_diff_report(self):
        """测试导出差异报告功能。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)
        self._create_test_data(v1_id)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        self.client.post("/scheme-release/new", data={
            "name": "差异导出测试",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        draft_id = self._get_latest_draft_id()
        self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)

        resp = self.client.post(
            f"/scheme-release/{draft_id}/export/diff",
            follow_redirects=False
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.content_type)

        content = resp.data.decode("utf-8-sig")
        self.assertIn("标签方案发布差异报告", content)
        self.assertIn("标签映射差异", content)
        self.assertIn("影响分析项", content)
        self.assertIn("审计日志", content)
        self.assertIn("正面", content)
        self.assertIn("负面", content)

    # ---------- 12. 完整链路测试 ----------

    def test_12_full_chain_draft_publish_revert_export(self):
        """完整链路：建草稿 -> 发布 -> 撤回 -> 按旧方案导出。"""
        self._create_scheme("情感分析", SCHEME_V1_JSON)
        v1_id = self._get_scheme_id_by_name_version("情感分析", 1)

        self._import_samples(v1_id, ["CHAIN001", "CHAIN002", "CHAIN003"])
        self._import_annotations(v1_id, "CHAIN001", "positive", 2)
        self._import_annotations(v1_id, "CHAIN001", "positive", 3)
        self._import_annotations(v1_id, "CHAIN002", "neutral", 2)
        self._import_annotations(v1_id, "CHAIN003", "negative", 2)

        self._create_scheme("情感分析", SCHEME_V2_JSON)
        v2_id = self._get_scheme_id_by_name_version("情感分析", 2)

        # 第1步：创建草稿
        resp = self.client.post("/scheme-release/new", data={
            "name": "完整链路测试",
            "description": "测试完整的发布-撤回-导出链路",
            "old_scheme_id": str(v1_id),
            "new_scheme_id": str(v2_id),
        }, follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        draft_id = self._get_latest_draft_id()
        self.assertIsNotNone(draft_id)

        draft = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        self.assertEqual(draft["status"], "draft")

        # 第2步：影响分析
        resp = self.client.post(f"/scheme-release/{draft_id}/analyze", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("影响分析完成", page)

        mappings_count = self._query_count("scheme_release_label_mappings", "draft_id=?", (draft_id,))
        self.assertGreater(mappings_count, 0)

        # 第3步：设置所有映射策略
        mappings = self._query_row(
            "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
            (draft_id,)
        )
        while mappings:
            self.client.post(
                f"/scheme-release/{draft_id}/mapping/{mappings['id']}/update",
                data={"strategy": "use_new", "note": "完整链路测试"},
                follow_redirects=True
            )
            mappings = self._query_row(
                "SELECT * FROM scheme_release_label_mappings WHERE draft_id = ? AND strategy = 'prompt'",
                (draft_id,)
            )

        # 第4步：正式发布
        resp = self.client.post(
            f"/scheme-release/{draft_id}/publish",
            data={"operator_note": "完整链路测试-发布"},
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("发布成功", page)

        draft = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        self.assertEqual(draft["status"], "published")
        self.assertEqual(self._get_active_scheme_id(), v2_id)

        # 第5步：验证数据迁移
        migrated_anns = self._query_count(
            "annotations", "scheme_id=? AND is_unknown_label=0", (v2_id,)
        )
        self.assertGreater(migrated_anns, 0, "标注应该已迁移到新方案")

        # 第6步：撤回发布
        resp = self.client.post(
            f"/scheme-release/{draft_id}/revert",
            data={"revert_note": "完整链路测试-撤回"},
            follow_redirects=True
        )
        self.assertEqual(resp.status_code, 200)
        page = resp.data.decode("utf-8")
        self.assertIn("撤回成功", page)

        draft = self._query_row("SELECT * FROM scheme_release_drafts WHERE id = ?", (draft_id,))
        self.assertEqual(draft["status"], "reverted")
        self.assertEqual(self._get_active_scheme_id(), v1_id)

        # 第7步：验证数据已恢复
        restored_anns = self._query_count(
            "annotations", "scheme_id=? AND is_unknown_label=0", (v1_id,)
        )
        self.assertGreater(restored_anns, 0, "标注应该已恢复到旧方案")

        # 第8步：按旧方案导出证据
        resp = self.client.post(
            f"/scheme-release/{draft_id}/export/evidence",
            data={"sample_scheme_filter": "include_migrated"},
            follow_redirects=False
        )
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.content_type)

        content = resp.data.decode("utf-8-sig")
        self.assertIn("旧方案快照", content)
        self.assertIn("情感分析 v1", content)
        self.assertIn("CHAIN001", content)
        self.assertIn("CHAIN002", content)
        self.assertIn("CHAIN003", content)
        self.assertIn("正面", content)

        # 第9步：验证审计日志完整
        audit_count = self._query_count("scheme_release_audit", "draft_id=?", (draft_id,))
        self.assertGreaterEqual(audit_count, 4, "至少有创建、分析更新、发布、撤回四条审计记录")

        # 第10步：导出差异报告
        resp = self.client.post(
            f"/scheme-release/{draft_id}/export/diff",
            follow_redirects=False
        )
        self.assertEqual(resp.status_code, 200)
        content = resp.data.decode("utf-8-sig")
        self.assertIn("差异报告", content)
        self.assertIn("已撤回", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
