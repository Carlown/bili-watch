import csv
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import app
from app import UID, Store, create_server, parse_feed, scan, source_clue


def item(n, ts=100, pinned=False, mid=UID):
    return {"type": "DYNAMIC_TYPE_AV", "id_str": str(n), "modules": {
        "module_author": {"mid": mid, "pub_ts": ts},
        "module_tag": {"text": "置顶"} if pinned else None,
        "module_dynamic": {"major": {"archive": {
            "bvid": "BV" + str(n).zfill(10), "title": f"视频 {n}",
            "desc": "原片：https://www.youtube.com/watch?v=uE2N9sqM4pQ"}}}}}


def payload(items, more=False, offset=""):
    return {"code": 0, "data": {"items": items, "has_more": more, "offset": offset}}


class ViewResponse:
    """Minimal stand-in for the urllib response of the B 站 view endpoint."""

    def __init__(self, payload):
        self.payload = payload

    def read(self):
        return json.dumps(self.payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def view_opener(marks):
    """Answer `copyright` per BV 号 so probe tests never touch the network."""
    return lambda request, timeout=None: ViewResponse(
        {"code": 0, "data": {"copyright": marks[request.full_url.split("bvid=")[-1]]}})


class Feed:
    def __init__(self, head, pages=None):
        self.head, self.pages = head, pages or {}

    def first(self):
        return self.head

    def next(self, offset):
        value = self.pages[offset]
        if isinstance(value, Exception):
            raise value
        return value


class MonitoringTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.temp.name) / "records.sqlite3")

    def tearDown(self):
        self.temp.cleanup()

    def baseline(self):
        return scan(Feed(payload([item(1)])), self.store)

    def test_real_null_tag_and_filter_other_authors(self):
        rows, _, _ = parse_feed(payload([item(1), item(2, mid="999")]))
        self.assertEqual([r["bvid"] for r in rows], ["BV0000000001"])
        self.assertFalse(rows[0]["pinned"])

    def test_explicit_source_and_title_guess_are_distinct(self):
        self.assertEqual(source_clue("title", "https://youtu.be/uE2N9sqM4pQ?t=3")[0],
                         "https://www.youtube.com/watch?v=uE2N9sqM4pQ")
        self.assertIn("推测", source_clue("title uE2N9sqM4pQ", "")[1])
        self.assertEqual(source_clue("title", "https://youtube.com.evil.test/watch?v=uE2N9sqM4pQ")[0], "")

    def test_baseline_no_alert_new_once_and_restart_deduplicates(self):
        self.assertTrue(self.baseline()["baseline"])
        self.assertIsNone(self.store.list()["event"])
        feed = Feed(payload([item(2, ts=110), item(1)]))
        self.assertEqual(scan(feed, self.store)["added"], 1)
        self.store = Store(self.store.path)
        self.assertEqual(scan(feed, self.store)["added"], 0)
        self.assertEqual(self.store.list()["event"]["count"], 1)

    def test_old_pinned_item_not_counted_as_new(self):
        self.baseline()
        scan(Feed(payload([item(9, ts=50, pinned=True), item(1)])), self.store)
        self.assertEqual(self.store.list()["new_count"], 0)

    def test_limit_preserves_cursor_and_later_resumes(self):
        self.baseline()
        feed = Feed(payload([item(3, ts=120)], True, "older"), {
            "older": payload([item(2, ts=110), item(1)])})
        self.assertEqual(scan(feed, self.store, page_limit=1)["backlog"], 1)
        self.assertEqual(scan(feed, self.store)["added"], 1)
        self.assertEqual(self.store.get("backlog"), [])
        self.assertEqual(self.store.list()["new_count"], 2)

    def test_failed_page_keeps_cursor_records_and_alert(self):
        self.baseline()
        feed = Feed(payload([item(3, ts=120)], True, "older"), {"older": RuntimeError("offline")})
        with self.assertRaisesRegex(RuntimeError, "offline"):
            scan(feed, self.store)
        self.assertEqual(self.store.get("backlog"), ["older"])
        self.assertEqual(self.store.list()["event"]["count"], 1)

    def test_failed_initial_fetch_does_not_initialize(self):
        with self.assertRaises(RuntimeError):
            scan(Feed({"code": -352}), self.store)
        self.assertFalse(self.store.get("initialized", False))

    def test_manual_notes_survive_rescan(self):
        self.baseline()
        self.store.update("BV0000000001", {"status": "reviewed", "notes": "对比 00:15 片段"})
        scan(Feed(payload([item(1)])), self.store)
        self.assertEqual(self.store.list()["videos"][0]["notes"], "对比 00:15 片段")

    def test_auto_queue_requires_explicit_source_link(self):
        self.baseline()
        guessed = item(3, ts=120)
        guessed["modules"]["module_dynamic"]["major"]["archive"]["desc"] = ""
        guessed["modules"]["module_dynamic"]["major"]["archive"]["title"] = "视频 3 uE2N9sqM4pQ"
        scan(Feed(payload([item(2, ts=130), guessed, item(1)])), self.store)
        queued = self.store.queue_auto_reports()
        self.assertEqual([row["bvid"] for row in queued], ["BV0000000002"])
        rows = {row["bvid"]: row for row in self.store.list()["videos"]}
        self.assertEqual(rows["BV0000000002"]["report_status"], "queued")
        self.assertEqual(rows["BV0000000003"]["report_status"], "not_requested")

    def test_auto_report_persists_submit_result(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(1)])), self.store)
        monitor = app.Monitor(self.store, Path(self.temp.name))
        monitor.mode = "auto"
        submitted = []

        class LoggedInFeed:
            login_confirmed = True

        class FakeAutomator:
            def __init__(self, feed, report_text=None):
                self.feed = feed
                self.report_text = report_text

            def submit(self, row):
                submitted.append(row["bvid"])

        original = app.ReportAutomator
        app.ReportAutomator = FakeAutomator
        try:
            result = monitor.auto_report(LoggedInFeed())
        finally:
            app.ReportAutomator = original
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(submitted, ["BV0000000002"])
        self.assertEqual(self.store.list()["videos"][0]["report_status"], "submitted")

    def test_auto_report_keeps_copyright_flow_for_user_details(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(1)])), self.store)
        monitor = app.Monitor(self.store, Path(self.temp.name))
        monitor.mode = "auto"

        class LoggedInFeed:
            login_confirmed = True

        class FakeAutomator:
            def __init__(self, feed, report_text=None):
                self.feed = feed
                self.report_text = report_text

            def submit(self, row):
                raise app.ReportNeedsDetails("已打开版权投诉页面；页面：https://www.bilibili.com/pc/community/copyright")

        original = app.ReportAutomator
        app.ReportAutomator = FakeAutomator
        try:
            result = monitor.auto_report(LoggedInFeed())
        finally:
            app.ReportAutomator = original
        row = self.store.list()["videos"][0]
        self.assertEqual(result["needs_details"], 1)
        self.assertEqual(result["failed"], 0)
        self.assertEqual(row["report_status"], "needs_details")
        self.assertIn("版权投诉页面", row["report_message"])

    def test_auto_report_waits_for_isolated_browser_login(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(1)])), self.store)
        monitor = app.Monitor(self.store, Path(self.temp.name))
        monitor.mode = "auto"
        result = monitor.auto_report(object())
        self.assertTrue(result["waiting_login"])
        self.assertEqual(result["submitted"], 0)
        row = self.store.list()["videos"][0]
        self.assertEqual(row["report_status"], "not_requested")

    def test_failed_report_can_be_requeued_and_clears_old_message(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(1)])), self.store)
        self.store.mark_report("BV0000000002", "failed", "网页中未找到提交按钮")
        self.store.retry_report("BV0000000002")
        row = {row["bvid"]: row for row in self.store.list()["videos"]}["BV0000000002"]
        self.assertEqual(row["report_status"], "queued")
        self.assertEqual(row["report_message"], "")
        self.assertEqual([row["bvid"] for row in self.store.queue_auto_reports()],
                         ["BV0000000002"])

    def test_submitted_report_cannot_be_retried(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(1)])), self.store)
        self.store.mark_report("BV0000000002", "submitted", "已提交")
        with self.assertRaisesRegex(ValueError, "提交失败"):
            self.store.retry_report("BV0000000002")

    def test_parse_feed_uses_requested_uid(self):
        rows, _, _ = parse_feed(payload([item(1), item(2, mid="999")]), "999")
        self.assertEqual([r["bvid"] for r in rows], ["BV0000000002"])

    def test_uid_change_validates_persists_and_resets_baseline(self):
        self.baseline()
        self.assertTrue(self.store.get("initialized"))
        for invalid in ("", "abc", "-1", "0", "12 34"):
            with self.assertRaises(ValueError):
                self.store.change_uid(invalid)
        self.assertEqual(self.store.change_uid(" 778899 "), "778899")
        self.assertEqual(self.store.uid(), "778899")
        self.assertFalse(self.store.get("initialized"))
        self.assertEqual(self.store.get("backlog"), [])
        self.assertEqual(Store(self.store.path).uid(), "778899")

    def test_records_are_scoped_to_the_active_uid(self):
        self.baseline()
        self.assertEqual([r["bvid"] for r in self.store.list()["videos"]],
                         ["BV0000000001"])
        self.store.change_uid("778899")
        self.assertEqual(self.store.list()["total"], 0)
        rows, _, _ = parse_feed(payload([item(5, ts=500, mid="778899")]), self.store.uid())
        self.assertEqual(len(rows), 1)
        self.store.ingest(rows, baseline=True)
        self.assertEqual([r["bvid"] for r in self.store.list()["videos"]],
                         ["BV0000000005"])
        self.store.change_uid(UID)
        self.assertEqual([r["bvid"] for r in self.store.list()["videos"]],
                         ["BV0000000001"])

    def test_legacy_database_is_attributed_to_default_uid(self):
        path = Path(self.temp.name) / "legacy.sqlite3"
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE videos (bvid TEXT PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL, published_at TEXT NOT NULL,
                published_ts INTEGER NOT NULL, url TEXT NOT NULL,
                source_url TEXT NOT NULL, source_kind TEXT NOT NULL,
                dynamic_id TEXT NOT NULL, first_seen TEXT NOT NULL,
                baseline INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                evidence_url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '');
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL, count INTEGER NOT NULL, title TEXT NOT NULL);
        """)
        db.execute("INSERT INTO videos VALUES ('BV0000000009','旧记录','','2026-01-01',"
                   "1,'https://example.test','','','d','2026-01-01',1,'pending','','')")
        db.commit()
        db.close()
        legacy = Store(path)
        self.assertEqual(legacy.uid(), UID)
        self.assertEqual(legacy.list()["total"], 1)
        self.assertEqual(legacy.list()["videos"][0]["uid"], UID)

    def test_retry_report_runs_again_in_auto_mode(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(1)])), self.store)
        self.store.mark_report("BV0000000002", "failed", "第一次失败")
        self.store.retry_report("BV0000000002")
        monitor = app.Monitor(self.store, Path(self.temp.name))
        monitor.mode = "auto"
        submitted = []

        class LoggedInFeed:
            login_confirmed = True

        class FakeAutomator:
            def __init__(self, feed, report_text=None):
                self.feed = feed
                self.report_text = report_text

            def submit(self, row):
                submitted.append(row["bvid"])

        original = app.ReportAutomator
        app.ReportAutomator = FakeAutomator
        try:
            result = monitor.auto_report(LoggedInFeed())
        finally:
            app.ReportAutomator = original
        self.assertEqual(result["submitted"], 1)
        self.assertEqual(submitted, ["BV0000000002"])
        self.assertEqual(self.store.list()["videos"][0]["report_status"], "submitted")

    def test_coin_probe_records_caps_for_new_records_and_reports_failures(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(3, ts=140), item(1)])), self.store)
        # 基线记录不参与自动检测，只检测新投稿。
        self.assertEqual([row["bvid"] for row in self.store.coin_pending()],
                         ["BV0000000002", "BV0000000003"])
        result = app.probe_coins(self.store, self.store.coin_pending(), pause=0,
                                 opener=view_opener({"BV0000000002": 1, "BV0000000003": 2}))
        self.assertEqual(result, {"checked": 2, "failed": 0})
        rows = {row["bvid"]: row for row in self.store.list()["videos"]}
        self.assertEqual((rows["BV0000000002"]["copyright"], rows["BV0000000002"]["coin_limit"]),
                         (1, 2))
        self.assertEqual((rows["BV0000000003"]["copyright"], rows["BV0000000003"]["coin_limit"]),
                         (2, 1))
        self.assertIn("可投 2 币", rows["BV0000000002"]["coin_note"])
        self.assertTrue(rows["BV0000000002"]["coin_checked_at"])
        self.assertEqual(self.store.list()["suspected_count"], 1)
        self.assertEqual(self.store.list()["coin_counts"], {1: 1, 2: 1, 0: 1})
        self.assertEqual(self.store.coin_pending(), [])

        def failing(request, timeout=None):
            raise urllib.error.URLError("network down")

        failed = app.probe_coins(self.store, [{"bvid": "BV0000000002"}], pause=0, opener=failing)
        self.assertEqual(failed, {"checked": 0, "failed": 1})
        row = {r["bvid"]: r for r in self.store.list()["videos"]}["BV0000000002"]
        self.assertIn("检测失败", row["coin_note"])
        # 失败的记录不会被每轮自动重试，但可以手动重测。
        self.assertEqual(self.store.coin_pending(), [])
        self.assertEqual([r["bvid"] for r in self.store.coin_pending(bvid="BV0000000002")],
                         ["BV0000000002"])

    def test_monitor_loads_and_validates_saved_settings(self):
        self.store.set("report_text", "保存的举报词 {title}")
        monitor = app.Monitor(self.store, Path(self.temp.name))
        self.assertEqual(monitor.report_text, "保存的举报词 {title}")
        self.assertTrue(monitor.coin_watch)
        self.store.set("report_text", 123)
        self.assertEqual(app.Monitor(self.store, Path(self.temp.name)).report_text,
                         app.REPORT_TEXT_DEFAULT)
        self.store.set("coin_watch", False)
        self.assertFalse(app.Monitor(self.store, Path(self.temp.name)).coin_watch)

    def test_auto_report_uses_the_configured_report_text(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(1)])), self.store)
        monitor = app.Monitor(self.store, Path(self.temp.name))
        monitor.mode = "auto"
        monitor.report_text = "自定义举报词 {title} {coin}{source}"
        seen = []

        class LoggedInFeed:
            login_confirmed = True

        class FakeAutomator:
            def __init__(self, feed, report_text=None):
                seen.append(report_text)

            def submit(self, row):
                return None

        original = app.ReportAutomator
        app.ReportAutomator = FakeAutomator
        try:
            monitor.auto_report(LoggedInFeed())
        finally:
            app.ReportAutomator = original
        self.assertEqual(seen, ["自定义举报词 {title} {coin}{source}"])

    def test_probe_pass_drains_the_new_record_backlog_in_batches(self):
        self.baseline()
        scan(Feed(payload([item(n, ts=200 + n) for n in range(2, 57)] + [item(1)])),
             self.store)
        self.assertEqual(len(self.store.coin_pending()), 20)
        monitor = app.Monitor(self.store, Path(self.temp.name))
        batches = []

        def fake_probe(store, rows, stop=None, pause=0.4, opener=None):
            batches.append(len(rows))
            for row in rows:
                store.set_coin(row["bvid"], 2, "仅可投 1 币 · 稿件标注为转载")
            return {"checked": len(rows), "failed": 0}

        original = app.probe_coins
        app.probe_coins = fake_probe
        try:
            result = monitor._probe_coins()
        finally:
            app.probe_coins = original
        self.assertEqual(batches, [20, 20, 15])
        self.assertEqual(result, {"checked": 55, "failed": 0})
        self.assertEqual(self.store.coin_pending(), [])
        self.assertEqual(self.store.list()["coin_counts"], {0: 1, 2: 55})

    def test_coin_probe_skips_the_backlog_when_watching_is_off(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(3, ts=140), item(1)])), self.store)
        monitor = app.Monitor(self.store, Path(self.temp.name))
        monitor.coin_watch = False
        monitor.coin_probe_bvid = "BV0000000003"
        batches = []

        def fake_probe(store, rows, stop=None, pause=0.4, opener=None):
            batches.append([row["bvid"] for row in rows])
            return {"checked": len(rows), "failed": 0}

        original = app.probe_coins
        app.probe_coins = fake_probe
        try:
            monitor._probe_coins()
        finally:
            app.probe_coins = original
        self.assertEqual(batches, [["BV0000000003"]])
        self.assertEqual([row["bvid"] for row in self.store.coin_pending()],
                         ["BV0000000002", "BV0000000003"])

    def test_coin_pending_refresh_all_pages_with_offset(self):
        self.baseline()
        scan(Feed(payload([item(n, ts=200 + n) for n in range(2, 45)] + [item(1)])),
             self.store)
        first = self.store.coin_pending(refresh_all=True)
        second = self.store.coin_pending(refresh_all=True, offset=20)
        self.assertEqual((len(first), len(second)), (20, 20))
        self.assertEqual({row["bvid"] for row in first} & {row["bvid"] for row in second},
                         set())
        self.assertEqual(len(self.store.coin_pending(refresh_all=True, offset=40)), 4)

    def test_coin_marks_follow_the_monitored_account(self):
        self.baseline()
        scan(Feed(payload([item(2, ts=130), item(1)])), self.store)
        app.probe_coins(self.store, self.store.coin_pending(), pause=0,
                        opener=view_opener({"BV0000000002": 1}))
        self.assertEqual(self.store.list()["suspected_count"], 1)
        self.assertEqual(self.store.list()["coin_counts"], {0: 1, 1: 1})
        # 换到自定义 UID 后，另一个账号的标注不计入本账号统计。
        self.store.change_uid("778899")
        self.assertEqual(self.store.list()["suspected_count"], 0)
        self.assertEqual(self.store.list()["coin_counts"], {})
        rows, _, _ = parse_feed(payload([item(5, ts=500, mid="778899")]), self.store.uid())
        self.store.ingest(rows, baseline=True)
        self.assertEqual([row["bvid"] for row in self.store.coin_pending(refresh_all=True)],
                         ["BV0000000005"])
        app.probe_coins(self.store, self.store.coin_pending(refresh_all=True), pause=0,
                        opener=view_opener({"BV0000000005": 1}))
        self.assertEqual(self.store.list()["coin_counts"], {1: 1})
        self.assertEqual(self.store.list()["suspected_count"], 1)
        self.store.change_uid(UID)
        self.assertEqual(self.store.list()["coin_counts"], {0: 1, 1: 1})
        self.assertEqual(self.store.list()["suspected_count"], 1)

    def test_coin_probe_and_refresh_all_cover_baseline_records(self):
        self.baseline()
        rows = self.store.coin_pending(refresh_all=True)
        self.assertEqual([row["bvid"] for row in rows], ["BV0000000001"])

    def test_coin_columns_migrate_on_a_legacy_database(self):
        path = Path(self.temp.name) / "legacy-coins.sqlite3"
        db = sqlite3.connect(path)
        db.executescript("""
            CREATE TABLE videos (bvid TEXT PRIMARY KEY, title TEXT NOT NULL,
                description TEXT NOT NULL, published_at TEXT NOT NULL,
                published_ts INTEGER NOT NULL, url TEXT NOT NULL,
                source_url TEXT NOT NULL, source_kind TEXT NOT NULL,
                dynamic_id TEXT NOT NULL, first_seen TEXT NOT NULL,
                baseline INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                evidence_url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '');
            CREATE TABLE state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE events (id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL, count INTEGER NOT NULL, title TEXT NOT NULL);
        """)
        db.execute("INSERT INTO videos VALUES ('BV0000000009','旧记录','','2026-01-01',"
                   "1,'https://example.test','','','d','2026-01-01',1,'pending','','')")
        db.commit()
        db.close()
        legacy = Store(path)
        legacy.set_coin("BV0000000009", 1, "可投 2 币 · 稿件标注为自制")
        row = legacy.list()["videos"][0]
        self.assertEqual((row["copyright"], row["coin_limit"]), (1, 2))
        self.assertIn("可投 2 币", row["coin_note"])


class ReportLocatorTests(unittest.TestCase):
    class EmptyLocator:
        def count(self):
            return 0

    class Candidate:
        def __init__(self, text):
            self.text = text
            self.clicked = False

        def nth(self, index):
            return self

        def is_visible(self):
            return True

        def inner_text(self, timeout=None):
            return self.text

        def get_attribute(self, name):
            return None

        def click(self, **kwargs):
            self.clicked = True

        def wait_for(self, **kwargs):
            return None

    class Scope:
        def __init__(self, candidate):
            self.candidate = candidate

        def get_by_role(self, role, name=None):
            return ReportLocatorTests.EmptyLocator()

        def locator(self, selector):
            if 'role="radio"' in selector:
                return type("RadioLocator", (), {
                    "count": lambda self: 1,
                    "nth": lambda self, index: self._candidate,
                    "_candidate": self.candidate,
                })()
            return ReportLocatorTests.EmptyLocator()

        def inner_text(self, timeout=None):
            return self.candidate.text

    class Page:
        def __init__(self, scope):
            self.scope = scope

        def locator(self, selector):
            if selector == '[role="dialog"]:visible':
                return type("ScopeLocator", (), {
                    "count": lambda self: 1,
                    "nth": lambda self, index: self._scope,
                    "_scope": self.scope,
                })()
            return ReportLocatorTests.EmptyLocator()

        def wait_for_timeout(self, value):
            return None

    def test_reason_matching_allows_dynamic_radio_label(self):
        candidate = self.Candidate("版权问题（视频内容）")
        page = self.Page(self.Scope(candidate))
        app.ReportAutomator(object())._click_text(page, ("侵权", "版权", "其他"))
        self.assertTrue(candidate.clicked)

    def test_reason_matching_normalizes_whitespace(self):
        self.assertTrue(app.ReportAutomator._label_matches("侵权\n/\u00a0版权", "侵权"))
        self.assertTrue(app.ReportAutomator._label_matches("其他原因说明", "其他"))

    def test_copyright_page_is_classified_as_details_flow(self):
        class Body:
            def inner_text(self, timeout=None):
                return "填写我的信息 填写举证材料 账号实名认证"

        class Page:
            url = "https://www.bilibili.com/pc/community/copyright/role?pageType=1"

            def locator(self, selector):
                return Body()

        self.assertTrue(app.ReportAutomator._is_details_page(Page()))

    def test_copyright_form_selects_pedestrian_and_fills_source_and_description(self):
        class Candidate:
            def __init__(self, text, on_click=None):
                self.text = text
                self.clicked = False
                self.value = ""
                self.on_click = on_click

            def count(self):
                return 1

            def nth(self, index):
                return self

            def is_visible(self):
                return True

            def inner_text(self, timeout=None):
                return self.text

            def get_attribute(self, name):
                return None

            def wait_for(self, **kwargs):
                return None

            def click(self, **kwargs):
                self.clicked = True
                if self.on_click:
                    self.on_click()

            def fill(self, value):
                self.value = value

        class Page:
            def __init__(self, evidence_ready=False):
                self.evidence_ready = evidence_ready
                self.form_ready = evidence_ready
                self.role = Candidate("路人")
                self.next = Candidate("下一步", on_click=lambda: setattr(self, "form_ready", True))
                self.source = Candidate("")
                self.description = Candidate("")
                self.submit = Candidate("提交举报")

            def locator(self, selector):
                if selector == ".copyright-role--select .select-container":
                    if self.evidence_ready:
                        raise AssertionError("role step should be skipped")
                    return self.role
                if selector in {"button.button-next, .button-next", "button.button-next, .button-next"}:
                    return self.next
                if selector in {"button.copyright-index__button--submit",
                                'button[class*="copyright-index__button--submit"]'}:
                    return self.submit
                if "首发链接" in selector or "原创作品" in selector:
                    return self.source if self.form_ready else None
                if "具体片段" in selector or "举报说明" in selector:
                    return self.description if self.form_ready else None
                if selector == "body":
                    return Candidate("")
                raise AssertionError(selector)

            def wait_for_timeout(self, value):
                return None

        page = Page()
        automator = app.ReportAutomator(object())
        automator._contexts = lambda current: [current]
        row = {"bvid": "BV1234567890", "title": "测试视频",
               "source_url": "https://www.youtube.com/watch?v=abcdefghijk"}
        automator._submit_copyright(page, row)
        self.assertTrue(page.role.clicked)
        self.assertTrue(page.next.clicked)
        self.assertTrue(page.submit.clicked)
        self.assertEqual(page.source.value, row["source_url"])
        self.assertIn("测试视频", page.description.value)
        self.assertIn(row["source_url"], page.description.value)
        self.assertIn("翻译成中文", page.description.value)

    def test_report_description_names_translation_and_reupload(self):
        row = {"bvid": "BV1234567890", "title": "测试视频",
               "source_url": "https://www.youtube.com/watch?v=abcdefghijk"}
        text = app.report_description(row)
        self.assertIn("翻译成中文", text)
        self.assertIn("搬运", text)
        self.assertIn("测试视频", text)
        self.assertIn(row["source_url"], text)
        self.assertLessEqual(len(text), 400)

    def test_report_description_fills_placeholders_from_a_custom_template(self):
        row = {"bvid": "BV1234567890", "title": "测试视频",
               "url": "https://www.bilibili.com/video/BV1234567890/",
               "source_url": "https://www.youtube.com/watch?v=abcdefghijk", "copyright": 1}
        text = app.report_description(row, "{title}|{bvid}|{url}|{source}|{coin}")
        self.assertEqual(text, "测试视频|BV1234567890|https://www.bilibili.com/video/BV1234567890/|"
                               "https://www.youtube.com/watch?v=abcdefghijk|"
                               "该稿件标注为自制、可投 2 个币，请一并核查其原创声明；")
        self.assertIn("可投 2 个币", app.report_description(row))
        self.assertEqual(app.report_description({**row, "copyright": 2}).count("仅可投 1 个币"), 1)
        # 未检测到投币上限时占位符留空，且超长文案仍受版权投诉页 400 字上限约束。
        self.assertNotIn("币", app.report_description({**row, "copyright": 0}, "{coin}"))
        long_text = app.report_description({**row, "title": "长" * 800})
        self.assertEqual(len(long_text), 400)

    def test_coin_verdict_maps_publish_mark_to_coin_cap(self):
        repost = app.coin_verdict(2)
        self.assertEqual((repost["mark"], repost["coin_limit"]), ("转载", 1))
        self.assertIn("仅可投 1 币", repost["coin_note"])
        self.assertEqual(repost["suspicion"], "已标注转载")
        self_claim = app.coin_verdict(1)
        self.assertEqual((self_claim["mark"], self_claim["coin_limit"]), ("自制", 2))
        self.assertIn("可投 2 币", self_claim["coin_note"])
        self.assertEqual(self_claim["suspicion"], "疑似转载投自制")
        unknown = app.coin_verdict(0)
        self.assertEqual((unknown["coin_limit"], unknown["coin_sentence"]), (0, ""))
        self.assertEqual(app.coin_limit_for(None), 0)

    def test_report_text_validation_keeps_templates_usable(self):
        self.assertEqual(app.validate_report_text("  自定义文案  "), "自定义文案")
        for value, message in (("", "不能为空"), ("{not_a_field}", "占位符无效"),
                               ("字" * 1001, "过长")):
            with self.assertRaisesRegex(ValueError, message):
                app.validate_report_text(value)

    def test_coin_probe_reads_bilibili_view_response(self):
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def read(self):
                return json.dumps(self.payload).encode("utf-8")

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        seen = []

        def opener(request, timeout=None):
            seen.append(request.full_url)
            return Response({"code": 0, "data": {"copyright": 2}})

        verdict = app.probe_copyright("BV1234567890", opener=opener)
        self.assertEqual((verdict["copyright"], verdict["coin_limit"]), (2, 1))
        self.assertIn("bvid=BV1234567890", seen[0])
        with self.assertRaisesRegex(ValueError, "BV 号无效"):
            app.probe_copyright("not-a-bv", opener=opener)
        with self.assertRaisesRegex(RuntimeError, "未返回"):
            app.probe_copyright("BV1234567890",
                                opener=lambda request, timeout=None: Response(
                                    {"code": 0, "data": {}}))
        with self.assertRaisesRegex(RuntimeError, "返回 -404"):
            app.probe_copyright("BV1234567890",
                                opener=lambda request, timeout=None: Response(
                                    {"code": -404, "message": "啥都木有"}))

    def test_copyright_form_skips_role_step_after_same_page_navigation(self):
        class Candidate:
            def __init__(self, text):
                self.text = text
                self.clicked = False
                self.value = ""

            def count(self):
                return 1

            def nth(self, index):
                return self

            def is_visible(self):
                return True

            def inner_text(self, timeout=None):
                return self.text

            def get_attribute(self, name):
                return None

            def wait_for(self, **kwargs):
                return None

            def click(self, **kwargs):
                self.clicked = True

            def fill(self, value):
                self.value = value

        class Page:
            def __init__(self):
                self.source = Candidate("")
                self.description = Candidate("")
                self.submit = Candidate("提交举报")

            def locator(self, selector):
                if "首发链接" in selector or "原创作品" in selector:
                    return self.source
                if "具体片段" in selector or "举报说明" in selector:
                    return self.description
                if selector in {"button.copyright-index__button--submit",
                                'button[class*="copyright-index__button--submit"]'}:
                    return self.submit
                raise AssertionError(selector)

            def wait_for_timeout(self, value):
                return None

        page = Page()
        automator = app.ReportAutomator(object())
        automator._contexts = lambda current: [current]
        automator._click_text = lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("same-page evidence form must not search role text"))
        row = {"bvid": "BV1234567890", "title": "同页测试",
               "source_url": "https://www.youtube.com/watch?v=abcdefghijk"}
        automator._submit_copyright(page, row)
        self.assertTrue(page.submit.clicked)
        self.assertIn("同页测试", page.description.value)


class LocalHTTPTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.server = create_server(self.temp.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        self.server.store.ingest(parse_feed(payload([item(1)]))[0], baseline=True)

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
        self.temp.cleanup()

    def request(self, path, body=None, token=None, **headers):
        req = urllib.request.Request(self.url + path,
              data=json.dumps(body).encode() if body is not None else None,
              headers={"Content-Type": "application/json", **headers})
        if token:
            req.add_header("X-Local-Token", token)
        return urllib.request.urlopen(req, timeout=5)

    def test_real_http_notes_persist_and_csv_defaults_to_utf8_without_bom(self):
        with self.request("/api/status") as r:
            token = json.load(r)["token"]
        with self.request("/api/video/BV0000000001", {"status": "reviewed", "notes": "证据"}, token) as r:
            self.assertTrue(json.load(r)["ok"])
        with self.request("/api/videos") as r:
            self.assertEqual(json.load(r)["videos"][0]["notes"], "证据")
        with self.request("/api/export") as r:
            data = r.read()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
            self.assertEqual(r.headers["Content-Type"], "text/csv; charset=utf-8")
            self.assertIn('filename="bili-evidence.csv"', r.headers["Content-Disposition"])
            rows = list(csv.DictReader(io.StringIO(data.decode("utf-8"))))
            self.assertEqual(rows[0]["bvid"], "BV0000000001")
            self.assertEqual(rows[0]["notes"], "证据")

    def test_excel_csv_opt_in_only_adds_bom_and_changes_filename(self):
        notes = '中文, "引号"\n换行'
        self.server.store.update("BV0000000001", {"notes": notes})
        with self.request("/api/export") as r:
            standard = r.read()
        with self.request("/api/export?excel=1") as r:
            excel = r.read()
            self.assertEqual(r.headers["Content-Type"], "text/csv; charset=utf-8")
            self.assertIn('filename="bili-evidence-excel.csv"', r.headers["Content-Disposition"])
        self.assertEqual(excel, b"\xef\xbb\xbf" + standard)
        rows = list(csv.DictReader(io.StringIO(excel.decode("utf-8-sig"))))
        self.assertEqual(rows[0]["bvid"], "BV0000000001")
        self.assertEqual(rows[0]["notes"], notes)

    def test_csv_bom_requires_explicit_excel_opt_in(self):
        for query in ("excel=0", "excel=", "excel=false", "excel=true"):
            with self.subTest(query=query), self.request("/api/export?" + query) as r:
                self.assertTrue(r.read().startswith(b"bvid,"))

    def test_external_origin_and_missing_token_cannot_mutate(self):
        for token, headers in [(None, {}), (self.server.token, {"Origin": "https://example.com"})]:
            with self.assertRaises(urllib.error.HTTPError) as exc:
                self.request("/api/control", {"action": "pause"}, token, **headers)
            self.assertEqual(exc.exception.code, 403)
        self.assertTrue(self.server.monitor.enabled)

    def test_mode_switch_is_persisted_and_returned(self):
        with self.request("/api/status") as r:
            token = json.load(r)["token"]
        with self.request("/api/control", {"action": "mode", "mode": "auto"}, token) as r:
            self.assertTrue(json.load(r)["ok"])
        with self.request("/api/status") as r:
            status = json.load(r)
        self.assertEqual(status["mode"], "auto")
        self.assertEqual(self.server.store.get("mode"), "auto")

    def test_panel_reports_the_app_version(self):
        with self.request("/api/status") as r:
            self.assertEqual(json.load(r)["version"], app.APP_VERSION)
        with self.request("/") as r:
            html = r.read().decode("utf-8")
        self.assertIn('id="app-version"', html)
        self.assertIn(f"v{app.APP_VERSION}", html)
        self.assertEqual(app.Handler.server_version, f"BiliWatch/{app.APP_VERSION}")

    def test_brand_assets_are_served_locally(self):
        with self.request("/logo.svg") as r:
            self.assertEqual(r.headers.get_content_type(), "image/svg+xml")
            self.assertIn("<svg", r.read().decode("utf-8"))
        with self.request("/favicon.ico") as r:
            self.assertEqual(r.headers.get_content_type(), "image/x-icon")
            self.assertTrue(r.read().startswith(b"\x00\x00\x01\x00"))
        with self.request("/") as r:
            html = r.read().decode("utf-8")
        self.assertIn("/logo.svg", html)

    def test_uid_control_requires_token_persists_and_validates(self):
        with self.request("/api/status") as r:
            token = json.load(r)["token"]
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request("/api/control", {"action": "uid", "uid": "778899"})
        self.assertEqual(exc.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.request("/api/control", {"action": "uid", "uid": "not-a-uid"}, token)
        self.assertEqual(bad.exception.code, 400)
        with self.request("/api/control", {"action": "uid", "uid": "778899"}, token) as r:
            self.assertTrue(json.load(r)["ok"])
        with self.request("/api/status") as r:
            status = json.load(r)
        self.assertEqual(status["uid"], "778899")
        self.assertEqual(status["space_url"], "https://space.bilibili.com/778899/dynamic")
        reopened = Store(Path(self.temp.name) / "records.sqlite3")
        self.assertEqual(reopened.uid(), "778899")
        self.assertEqual(reopened.list()["total"], 0)

    def test_retry_report_control_requires_token_and_requeues(self):
        self.server.store.ingest(parse_feed(payload([item(2, ts=130)]))[0])
        self.server.store.mark_report("BV0000000002", "failed", "第一次失败")
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request("/api/control", {"action": "retry_report", "bvid": "BV0000000002"})
        self.assertEqual(exc.exception.code, 403)
        with self.request("/api/status") as r:
            token = json.load(r)["token"]
        with self.request("/api/control", {"action": "retry_report", "bvid": "BV0000000002"}, token) as r:
            self.assertTrue(json.load(r)["ok"])
        row = {row["bvid"]: row for row in self.server.store.list()["videos"]}["BV0000000002"]
        self.assertEqual(row["report_status"], "queued")

    def test_report_text_and_coin_controls_require_token_and_persist(self):
        with self.request("/api/status") as r:
            token = json.load(r)["token"]
        with self.assertRaises(urllib.error.HTTPError) as exc:
            self.request("/api/control", {"action": "report_text", "text": "偷改"})
        self.assertEqual(exc.exception.code, 403)
        with self.assertRaises(urllib.error.HTTPError) as bad:
            self.request("/api/control", {"action": "report_text", "text": "{没有这占位符}"}, token)
        self.assertEqual(bad.exception.code, 400)
        custom = "举报《{title}》翻译搬运；{coin}{source}"
        with self.request("/api/control", {"action": "report_text", "text": custom}, token) as r:
            self.assertTrue(json.load(r)["ok"])
        with self.request("/api/status") as r:
            status = json.load(r)
        self.assertEqual(status["report_text"], custom)
        self.assertEqual(status["report_text_default"], app.REPORT_TEXT_DEFAULT)
        self.assertIn("coin", status["report_placeholders"])
        reopened = Store(Path(self.temp.name) / "records.sqlite3")
        self.assertEqual(reopened.get("report_text"), custom)
        with self.request("/api/control", {"action": "report_text_reset"}, token) as r:
            self.assertTrue(json.load(r)["ok"])
        self.assertEqual(self.server.monitor.report_text, app.REPORT_TEXT_DEFAULT)

        with self.request("/api/control", {"action": "coin_watch", "enabled": False}, token) as r:
            self.assertTrue(json.load(r)["ok"])
        self.assertFalse(self.server.monitor.coin_watch)
        self.assertFalse(reopened.get("coin_watch"))
        with self.assertRaises(urllib.error.HTTPError) as bad_bv:
            self.request("/api/control", {"action": "coin_probe", "bvid": "不是BV"}, token)
        self.assertEqual(bad_bv.exception.code, 400)
        with self.request("/api/control", {"action": "coin_probe", "bvid": "BV0000000001"}, token) as r:
            self.assertTrue(json.load(r)["ok"])
        self.assertEqual(self.server.monitor.coin_probe_bvid, "BV0000000001")
        self.assertTrue(self.server.monitor.check_requested)

    def test_videos_and_csv_expose_the_coin_columns(self):
        self.server.store.set_coin("BV0000000001", 1, "可投 2 币 · 稿件标注为自制")
        with self.request("/api/videos") as r:
            row = json.load(r)["videos"][0]
        self.assertEqual((row["copyright"], row["coin_limit"]), (1, 2))
        self.assertIn("可投 2 币", row["coin_note"])
        with self.request("/api/export") as r:
            header = r.read().decode("utf-8").splitlines()[0]
        self.assertIn("coin_limit", header)
        self.assertIn("coin_note", header)


if __name__ == "__main__":
    unittest.main(verbosity=2)
