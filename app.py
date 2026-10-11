from __future__ import annotations

import argparse
import csv
from contextlib import contextmanager
import io
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import urllib.error
from urllib.parse import parse_qs, urlsplit
import urllib.request
import webbrowser

APP_VERSION = "1.1.0"

# Default monitored UID; the user can change it from the panel and it is persisted.
UID = "1263732318"
UID_PATTERN = re.compile(r"[1-9][0-9]{0,18}")
FEED_PATH = "/x/polymer/web-dynamic/v1/feed/space"
SHANGHAI = timezone(timedelta(hours=8))
STATUSES = {"pending", "reviewed", "dismissed", "handled"}
MODES = {"manual", "auto"}
REPORT_STATUSES = {"not_requested", "queued", "submitting", "submitted", "failed",
                   "needs_details"}
REPORT_REASON = "侵权"

# B 站用稿件的“自制 / 转载”标注限制投币数：转载只能投 1 个币，自制可以投 2 个。
# The flag doubles as a repost tell: 2 coins means the uploader claimed 自制 for it.
COPYRIGHT_SELF = 1
COPYRIGHT_REPOST = 2
COIN_LIMITS = {COPYRIGHT_SELF: 2, COPYRIGHT_REPOST: 1}
VIEW_API = "https://api.bilibili.com/x/web-interface/view?bvid="
# B 站的风控会以 -412 拒绝 Referer 为站点的请求；_probe_headers() 因此带上视频页地址。
PROBE_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Origin": "https://www.bilibili.com",
}
COIN_PROBE_BATCH = 20
COIN_PROBE_PASSES = 4

# Default complaint wording. The user can edit it from the panel; placeholders are
# filled from the archived record before it is typed into B 站's report form.
REPORT_TEXT_DEFAULT = (
    "被举报稿件《{title}》疑似将原视频翻译成中文后未经授权搬运发布，"
    "译文内容、配音或画面与原片存在对应关系，请平台核查处理。{coin}原片出处：{source}")
REPORT_TEXT_LIMIT = 1000
REPORT_FIELD_LIMIT = 400
REPORT_PLACEHOLDERS = {
    "title": "被举报稿件的标题",
    "bvid": "BV 号",
    "url": "被举报稿件的 B 站链接",
    "source": "核实到的原片链接",
    "coin": "投币上限结论（如“该稿件标注为自制、可投 2 个币；”，未知时为空）",
}


def space_url(uid):
    return f"https://space.bilibili.com/{uid}/dynamic"


def normalize_uid(value):
    """Return a validated UID string or raise, so the panel can reject bad input."""
    uid = str(value or "").strip()
    if not UID_PATTERN.fullmatch(uid):
        raise ValueError("UID 需为 B 站数字账号 ID，例如 1263732318")
    return uid


def stamp(ts=None):
    return datetime.fromtimestamp(ts or time.time(), SHANGHAI).isoformat(timespec="seconds")


def source_clue(title, description):
    for raw in re.findall(r"https?://[^\s<>\"'，。]+", description):
        parts = urlsplit(raw.rstrip(".,);]"))
        host = (parts.hostname or "").lower()
        candidate = ""
        if host in {"youtube.com", "www.youtube.com", "m.youtube.com"}:
            candidate = parse_qs(parts.query).get("v", [""])[0]
            if not candidate and parts.path.startswith(("/shorts/", "/embed/")):
                candidate = parts.path.split("/")[2]
        elif host in {"youtu.be", "www.youtu.be"}:
            candidate = parts.path.strip("/").split("/")[0]
        if re.fullmatch(r"[A-Za-z0-9_-]{11}", candidate):
            return "https://www.youtube.com/watch?v=" + candidate, "投稿简介中的链接"
    match = re.search(r"(?:^|\s)([A-Za-z0-9_-]{11})\s*$", title)
    if match:
        return "https://www.youtube.com/watch?v=" + match[1], "根据标题末尾 ID 推测，待核实"
    return "", "尚未找到来源线索"


def coin_limit_for(copyright_value):
    """Coins a viewer can give: 转载 caps at 1, 自制 allows 2."""
    try:
        return COIN_LIMITS.get(int(copyright_value), 0)
    except (TypeError, ValueError):
        return 0


def coin_verdict(copyright_value):
    """Describe one video's 自制/转载 mark and the coin cap it implies."""
    try:
        value = int(copyright_value)
    except (TypeError, ValueError):
        value = 0
    limit = coin_limit_for(value)
    if value == COPYRIGHT_SELF:
        return {"copyright": value, "coin_limit": limit, "mark": "自制",
                "coin_note": "可投 2 币 · 稿件标注为自制",
                "suspicion": "疑似转载投自制",
                "coin_sentence": "该稿件标注为自制、可投 2 个币，请一并核查其原创声明；"}
    if value == COPYRIGHT_REPOST:
        return {"copyright": value, "coin_limit": limit, "mark": "转载",
                "coin_note": "仅可投 1 币 · 稿件标注为转载",
                "suspicion": "已标注转载",
                "coin_sentence": "该稿件标注为转载、仅可投 1 个币；"}
    return {"copyright": 0, "coin_limit": 0, "mark": "未知",
            "coin_note": "尚未检测投币上限", "suspicion": "", "coin_sentence": ""}


def coin_sentence(row):
    """Coin-based evidence sentence for the report text, empty when unknown."""
    if not row or not row.get("copyright"):
        return ""
    return coin_verdict(row.get("copyright"))["coin_sentence"]


def validate_report_text(value):
    """Keep the editable complaint wording usable for B 站's report forms."""
    text = str(value or "").strip()
    if not text:
        raise ValueError("举报词不能为空；可点“恢复默认”取回内置文案")
    if len(text) > REPORT_TEXT_LIMIT:
        raise ValueError(f"举报词过长，最多 {REPORT_TEXT_LIMIT} 字")
    unknown = sorted(set(re.findall(r"\{([^{}]*)\}", text)) - set(REPORT_PLACEHOLDERS))
    if unknown:
        raise ValueError("举报词占位符无效：" + "、".join("{" + name + "}" for name in unknown)
                         + "；可用占位符：" + "、".join("{" + name + "}"
                                                       for name in REPORT_PLACEHOLDERS))
    return text


def report_description(row, template=REPORT_TEXT_DEFAULT):
    """Render the (user editable) complaint statement for one archived record."""
    row = row or {}
    values = {
        "title": str(row.get("title") or row.get("bvid") or "").strip(),
        "bvid": str(row.get("bvid") or "").strip(),
        "url": str(row.get("url") or "").strip(),
        "source": str(row.get("source_url") or "").strip() or "未找到",
        "coin": coin_sentence(row),
    }
    text = str(template or REPORT_TEXT_DEFAULT)
    for name, value in values.items():
        text = text.replace("{" + name + "}", value)
    # The copyright complaint form caps its description field.
    return text.strip()[:REPORT_FIELD_LIMIT]


def probe_copyright(bvid, opener=None, timeout=15):
    """Read B 站's own 自制/转载 mark for one video and infer the coin cap.

    This is the same public field the site uses to limit coins, so it needs no
    account and no browser session. Failures are reported, never guessed.
    """
    bvid = str(bvid or "").strip()
    if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
        raise ValueError("BV 号无效，无法检测投币上限")
    headers = dict(PROBE_HEADERS, Referer=f"https://www.bilibili.com/video/{bvid}/")
    request = urllib.request.Request(VIEW_API + bvid, headers=headers)
    opener = opener or urllib.request.urlopen
    try:
        with opener(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"B 站接口返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"无法连接 B 站接口：{exc.reason}") from exc
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise RuntimeError("B 站接口返回的内容无法解析") from exc
    if payload.get("code") != 0:
        raise RuntimeError(f"B 站接口返回 {payload.get('code')}：{payload.get('message', '')}"
                           .strip())
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("B 站视频详情结构发生变化")
    verdict = coin_verdict(data.get("copyright"))
    if not verdict["copyright"]:
        raise RuntimeError("B 站未返回该稿件的自制/转载标注")
    return verdict


def probe_coins(store, rows, stop=None, pause=0.4, opener=None):
    """Record the coin verdict for archived videos; never abort the whole scan."""
    done, failed = 0, 0
    for index, row in enumerate(rows):
        if stop and stop.is_set():
            break
        if index and pause:
            time.sleep(pause)
        try:
            verdict = probe_copyright(row.get("bvid"), opener=opener)
        except Exception as exc:
            failed += 1
            logging.info("coin probe failed for %s: %s", row.get("bvid"), exc)
            note = f"检测失败：{exc}（可点“重新检测投币”）"
            store.set_coin(row["bvid"], 0, note)
            continue
        done += 1
        store.set_coin(row["bvid"], verdict["copyright"], verdict["coin_note"])
    return {"checked": done, "failed": failed}


def parse_feed(payload, uid=UID):
    if payload.get("code") != 0:
        raise RuntimeError(f"B 站接口返回 {payload.get('code')}：{payload.get('message', '')}")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise RuntimeError("投稿数据结构发生变化；本次不更新监控基线。")
    records = []
    for item in data["items"]:
        if item.get("type") != "DYNAMIC_TYPE_AV":
            continue
        modules = item.get("modules") or {}
        author = modules.get("module_author") or {}
        if str(author.get("mid")) != str(uid):
            continue
        dynamic = modules.get("module_dynamic") or {}
        archive = (dynamic.get("major") or {}).get("archive") or {}
        bvid = archive.get("bvid", "")
        if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
            continue
        title = str(archive.get("title") or bvid)
        description = str(archive.get("desc") or "")
        source, clue = source_clue(title, description)
        published = int(author.get("pub_ts") or 0)
        records.append({
            "bvid": bvid, "title": title, "description": description,
            "published_at": stamp(published) if published else "",
            "published_ts": published, "url": f"https://www.bilibili.com/video/{bvid}/",
            "source_url": source, "source_kind": clue,
            "pinned": bool((modules.get("module_tag") or {}).get("text") == "置顶"
                           or item.get("is_top")),
            "dynamic_id": str(item.get("id_str", "")),
        })
    return records, bool(data.get("has_more")), str(data.get("offset") or "")


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS videos (
                    bvid TEXT PRIMARY KEY, uid TEXT NOT NULL DEFAULT '',
                    title TEXT NOT NULL, description TEXT NOT NULL,
                    published_at TEXT NOT NULL, published_ts INTEGER NOT NULL,
                    url TEXT NOT NULL, source_url TEXT NOT NULL, source_kind TEXT NOT NULL,
                    dynamic_id TEXT NOT NULL, first_seen TEXT NOT NULL,
                    baseline INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    evidence_url TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '',
                    report_status TEXT NOT NULL DEFAULT 'not_requested',
                    report_message TEXT NOT NULL DEFAULT '', reported_at TEXT NOT NULL DEFAULT ''
                );
                CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL,
                    uid TEXT NOT NULL DEFAULT '', count INTEGER NOT NULL, title TEXT NOT NULL
                );
            """)
            columns = {row[1] for row in db.execute("PRAGMA table_info(videos)")}
            migrations = {
                "uid": "TEXT NOT NULL DEFAULT ''",
                "report_status": "TEXT NOT NULL DEFAULT 'not_requested'",
                "report_message": "TEXT NOT NULL DEFAULT ''",
                "reported_at": "TEXT NOT NULL DEFAULT ''",
                "copyright": "INTEGER NOT NULL DEFAULT 0",
                "coin_limit": "INTEGER NOT NULL DEFAULT 0",
                "coin_note": "TEXT NOT NULL DEFAULT ''",
                "coin_checked_at": "TEXT NOT NULL DEFAULT ''",
            }
            for name, definition in migrations.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE videos ADD COLUMN {name} {definition}")
            if "uid" not in {row[1] for row in db.execute("PRAGMA table_info(events)")}:
                db.execute("ALTER TABLE events ADD COLUMN uid TEXT NOT NULL DEFAULT ''")
            # Attribute records created before multi-UID support to the original target.
            db.execute("UPDATE videos SET uid=? WHERE uid=''", (UID,))
            db.execute("UPDATE events SET uid=? WHERE uid=''", (UID,))

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=""):
        with self.connect() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO state VALUES (?,?)", (key, json.dumps(value)))

    def uid(self):
        """Return the currently monitored UID, falling back to the default."""
        stored = self.get("uid", UID)
        return stored if isinstance(stored, str) and UID_PATTERN.fullmatch(stored) else UID

    def change_uid(self, value):
        """Persist a new target UID and let the monitor build a fresh baseline."""
        uid = normalize_uid(value)
        self.set("uid", uid)
        self.set("initialized", False)
        self.set("baseline_cutoff", 0)
        self.set("backlog", [])
        return uid

    def known(self, bvids):
        if not bvids:
            return set()
        with self.connect() as db:
            rows = db.execute("SELECT bvid FROM videos WHERE bvid IN (" +
                              ",".join("?" for _ in bvids) + ")", bvids).fetchall()
        return {row[0] for row in rows}

    def ingest(self, rows, baseline=False, uid=None):
        added = []
        uid = uid or self.uid()
        cutoff = self.get("baseline_cutoff", 0)
        with self.connect() as db:
            for row in rows:
                values = [row[key] for key in (
                    "bvid", "title", "description", "published_at", "published_ts",
                    "url", "source_url", "source_kind", "dynamic_id")]
                is_baseline = bool(baseline or (cutoff and row["published_ts"] < cutoff))
                cursor = db.execute("""INSERT OR IGNORE INTO videos
                    (bvid,uid,title,description,published_at,published_ts,url,source_url,
                    source_kind,dynamic_id,first_seen,baseline) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                    [values[0], uid] + values[1:] + [stamp(), int(is_baseline)])
                if cursor.rowcount:
                    added.append({**row, "baseline": is_baseline, "uid": uid})
                else:
                    db.execute("""UPDATE videos SET title=?, description=?, source_url=?,
                        source_kind=? WHERE bvid=?""", (row["title"], row["description"],
                        row["source_url"], row["source_kind"], row["bvid"]))
        return added

    def event(self, added):
        if added:
            with self.connect() as db:
                db.execute("INSERT INTO events(created_at,uid,count,title) VALUES (?,?,?,?)",
                           (stamp(), added[0].get("uid") or self.uid(), len(added),
                            added[0]["title"]))

    def list(self, search="", status="", page=1, size=50):
        uid = self.uid()
        filters, args = ["uid=?"], [uid]
        if search:
            filters.append("(title LIKE ? OR bvid LIKE ? OR notes LIKE ?)")
            args.extend(["%" + search + "%"] * 3)
        if status in STATUSES:
            filters.append("status=?")
            args.append(status)
        where = " WHERE " + " AND ".join(filters)
        with self.connect() as db:
            total = db.execute("SELECT COUNT(*) FROM videos" + where, args).fetchone()[0]
            rows = db.execute("SELECT * FROM videos" + where +
                              " ORDER BY published_ts DESC,first_seen DESC LIMIT ? OFFSET ?",
                              args + [size, (page - 1) * size]).fetchall()
            counts = dict(db.execute(
                "SELECT status,COUNT(*) FROM videos WHERE uid=? GROUP BY status", (uid,)))
            new_count = db.execute(
                "SELECT COUNT(*) FROM videos WHERE uid=? AND baseline=0", (uid,)).fetchone()[0]
            report_counts = dict(db.execute(
                "SELECT report_status,COUNT(*) FROM videos WHERE uid=? GROUP BY report_status",
                (uid,)))
            coin_counts = dict(db.execute(
                "SELECT copyright,COUNT(*) FROM videos WHERE uid=? GROUP BY copyright", (uid,)))
            suspected = db.execute(
                "SELECT COUNT(*) FROM videos WHERE uid=? AND copyright=?",
                (uid, COPYRIGHT_SELF)).fetchone()[0]
            event = db.execute(
                "SELECT * FROM events WHERE uid=? ORDER BY id DESC LIMIT 1", (uid,)).fetchone()
        return {"videos": [dict(r) for r in rows], "total": total,
                "counts": counts, "new_count": new_count,
                "report_counts": report_counts,
                "coin_counts": coin_counts, "suspected_count": suspected,
                "event": dict(event) if event else None, "page": page}

    def update(self, bvid, body):
        status = body.get("status", "pending")
        if status not in STATUSES:
            raise ValueError("核实状态无效")
        evidence = str(body.get("evidence_url", "")).strip()
        if evidence and (urlsplit(evidence).scheme not in {"https", "http"} or
                         not urlsplit(evidence).hostname):
            raise ValueError("证据链接需为完整的 http 或 https 地址")
        notes = str(body.get("notes", ""))
        if len(notes) > 20000 or len(evidence) > 2000:
            raise ValueError("记录过长")
        with self.connect() as db:
            cursor = db.execute("UPDATE videos SET status=?,evidence_url=?,notes=? WHERE bvid=?",
                                (status, evidence, notes, bvid))
            if not cursor.rowcount:
                raise ValueError("视频记录不存在")

    def queue_auto_reports(self):
        """Queue only new records with an explicit source link, once."""
        uid = self.uid()
        with self.connect() as db:
            db.execute("""UPDATE videos SET report_status='queued'
                WHERE uid=? AND baseline=0 AND source_url<>''
                AND source_kind='投稿简介中的链接'
                AND status='pending' AND report_status='not_requested'""", (uid,))
            rows = db.execute("""SELECT * FROM videos
                WHERE uid=? AND report_status='queued' ORDER BY published_ts ASC""",
                (uid,)).fetchall()
        return [dict(row) for row in rows]

    def retry_report(self, bvid):
        """Requeue one previously failed report without changing review notes."""
        bvid = str(bvid or "").strip()
        if not bvid:
            raise ValueError("缺少视频 BV 号")
        with self.connect() as db:
            row = db.execute("SELECT report_status,source_url FROM videos WHERE bvid=?",
                             (bvid,)).fetchone()
            if not row:
                raise ValueError("视频记录不存在")
            if row["report_status"] != "failed":
                raise ValueError("只有“提交失败”的记录可以重试")
            if not row["source_url"]:
                raise ValueError("该记录没有原片链接，无法自动重试")
            cursor = db.execute("""UPDATE videos SET report_status='queued',
                report_message='', reported_at='' WHERE bvid=? AND report_status='failed'""",
                                (bvid,))
            if not cursor.rowcount:
                raise ValueError("举报状态已变化，请刷新后重试")

    def set_coin(self, bvid, copyright_value, note):
        """Persist one video's 自制/转载 mark, its implied coin cap, and the verdict text.

        A recorded failure also stamps `coin_checked_at` so a broken B 站 endpoint is
        not hammered every cycle; the panel offers a manual re-check instead.
        """
        limit = coin_limit_for(copyright_value)
        with self.connect() as db:
            cursor = db.execute("""UPDATE videos SET copyright=?, coin_limit=?, coin_note=?,
                coin_checked_at=? WHERE bvid=?""",
                (int(copyright_value or 0), limit, str(note)[:500], stamp(), str(bvid)))
            if not cursor.rowcount:
                raise ValueError("视频记录不存在")

    def coin_pending(self, refresh_all=False, bvid="", offset=0):
        """Videos whose coin cap still needs reading, oldest first so backlog stays orderly."""
        uid = self.uid()
        with self.connect() as db:
            if bvid:
                rows = db.execute("SELECT * FROM videos WHERE bvid=? AND uid=?",
                                  (str(bvid), uid)).fetchall()
            elif refresh_all:
                rows = db.execute("SELECT * FROM videos WHERE uid=?"
                                  " ORDER BY published_ts ASC LIMIT ? OFFSET ?",
                                  (uid, COIN_PROBE_BATCH, max(0, int(offset)))).fetchall()
            else:
                rows = db.execute("""SELECT * FROM videos WHERE uid=? AND baseline=0
                    AND coin_checked_at='' ORDER BY published_ts ASC LIMIT ?""",
                    (uid, COIN_PROBE_BATCH)).fetchall()
        return [dict(row) for row in rows]

    def mark_report(self, bvid, status, message=""):
        if status not in REPORT_STATUSES:
            raise ValueError("举报状态无效")
        with self.connect() as db:
            cursor = db.execute("""UPDATE videos SET report_status=?, report_message=?,
                reported_at=? WHERE bvid=?""",
                (status, str(message)[:2000], stamp() if status == "submitted" else "", bvid))
            if not cursor.rowcount:
                raise ValueError("视频记录不存在")

    def export(self, uid=None):
        uid = uid or self.uid()
        with self.connect() as db:
            rows = db.execute(
                "SELECT * FROM videos WHERE uid=? ORDER BY published_ts DESC",
                (uid,)).fetchall()
        return [dict(row) for row in rows]


class BrowserFeed:
    def __init__(self, profile):
        self.profile = profile
        self.uid = UID
        self.playwright = None
        self.context = None
        self.page = None
        self.feed_url = ""
        self.page_cache = {}
        self.visible = False
        self.login_confirmed = False

    def open(self, visible=False):
        if self.context:
            if self.page and not self.page.is_closed() and (not visible or self.visible):
                return
            self.close()
        from playwright.sync_api import sync_playwright
        self.playwright = sync_playwright().start()
        errors = []
        for channel in ("msedge", "chrome"):
            try:
                self.context = self.playwright.chromium.launch_persistent_context(
                    str(self.profile), channel=channel, headless=not visible,
                    args=["--new-window", "--no-first-run", "--no-default-browser-check"],
                    viewport={"width": 1280, "height": 850})
                break
            except Exception as exc:
                errors.append(str(exc).splitlines()[0])
        if self.context is None:
            self.close()
            raise RuntimeError("未能启动 Edge 或 Chrome：" + "; ".join(errors))
        self.visible = visible
        self.login_confirmed = False
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(20000)

    def set_uid(self, uid):
        self.uid = str(uid)

    def go_to_space(self):
        if not self.context or not self.page or self.page.is_closed():
            self.open(visible=True)
        self.page.goto(space_url(self.uid), wait_until="domcontentloaded", timeout=30000)

    def has_authenticated_session(self):
        if not self.context:
            return False
        try:
            names = {cookie["name"] for cookie in self.context.cookies()}
        except Exception:
            return False
        return bool({"SESSDATA", "DedeUserID", "bili_jct"} & names)

    def confirm_login(self):
        if not self.context or not self.visible or not self.page or self.page.is_closed():
            raise RuntimeError("独立监控浏览器尚未打开")
        if not self.has_authenticated_session():
            raise RuntimeError("尚未检测到 B 站登录状态，请先在独立监控浏览器中登录")
        self.login_confirmed = True

    def browser_open(self):
        open_now = bool(self.context and self.page and not self.page.is_closed())
        if not open_now:
            self.login_confirmed = False
        return open_now

    def first(self):
        if not self.context or self.page.is_closed():
            self.open(self.visible)
        responses = []
        def capture(response):
            if FEED_PATH in response.url:
                responses.append(response)
        self.page.on("response", capture)
        try:
            self.page.goto(space_url(self.uid), wait_until="domcontentloaded", timeout=30000)
            try:
                self.page.locator('a.bili-dyn-card-video').first.wait_for(
                    state="attached", timeout=25000)
            except Exception:
                pass
            failure = "未读到视频列表，网页可能需要登录或验证。"
            for response in reversed(responses):
                if response.status != 200:
                    failure = f"B 站返回 HTTP {response.status}，请打开监控浏览器检查网页。"
                    continue
                payload = response.json()
                try:
                    rows, _, _ = parse_feed(payload, self.uid)
                except RuntimeError as exc:
                    failure = str(exc)
                    continue
                if rows:
                    self.feed_url = response.url
                    self.page_cache = {}
                    return payload
            raise RuntimeError(failure)
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("网页投稿数据读取失败；可打开监控浏览器检查登录或网页验证。 " +
                               str(exc).splitlines()[0]) from exc
        finally:
            self.page.remove_listener("response", capture)

    def next(self, offset):
        if offset in self.page_cache:
            return self.page_cache[offset]
        self.page.locator('a.bili-dyn-card-video').first.wait_for(state="attached")
        # Let the site's normal scroll handler issue and sign its own next-page request.
        for _ in range(80):
            with self.page.expect_response(
                    lambda response: FEED_PATH in response.url, timeout=20000) as event:
                self.page.evaluate("""() => window.scrollTo(0,
                    Math.max(document.body.scrollHeight,document.documentElement.scrollHeight))""")
            response = event.value
            if response.status != 200:
                raise RuntimeError(f"B 站分页返回 HTTP {response.status}；已读到的记录仍保留。")
            result = response.json()
            _, more, _ = parse_feed(result)
            requested = parse_qs(urlsplit(response.url).query).get("offset", [""])[0]
            self.page_cache[requested] = result
            if requested == offset:
                return result
            if not more:
                break
            self.page.wait_for_timeout(400)
        raise RuntimeError("未找到上次分页位置，已有记录仍保留；下一次继续补查。")

    def close(self):
        try:
            if self.context:
                self.context.close()
        finally:
            self.context = self.page = None
            if self.playwright:
                self.playwright.stop()
                self.playwright = None
            self.visible = False
            self.login_confirmed = False


class ReportNeedsDetails(RuntimeError):
    """The site opened a user-completed complaint form instead of a report form."""


class ReportAutomator:
    """Submit a report through the signed-in browser session, never by API."""

    def __init__(self, feed, report_text=None):
        self.feed = feed
        self.report_text = report_text or REPORT_TEXT_DEFAULT
        self.last_failed_page = None
        self.debug_dir = Path(os.getenv("LOCALAPPDATA", str(Path.home()))) / "BiliWatch" / "report-debug"

    @staticmethod
    def _normalize_text(value):
        """Normalize dynamic menu labels without relying on exact text nodes."""
        return re.sub(r"\s+", "", str(value or "")).strip().lower()

    @classmethod
    def _label_matches(cls, text, label):
        target = cls._normalize_text(label)
        return bool(target and target in cls._normalize_text(text))

    @classmethod
    def _first_visible(cls, locator, timeout=2500):
        try:
            count = min(locator.count(), 50)
        except Exception:
            return None
        for index in range(count):
            candidate = locator.nth(index)
            try:
                candidate.wait_for(state="visible", timeout=timeout)
                return candidate
            except Exception:
                continue
        return None

    @classmethod
    def _find_clickable(cls, locator, label):
        """Find a visible clickable element whose own text contains the label."""
        try:
            count = min(locator.count(), 80)
        except Exception:
            return None
        for index in range(count):
            candidate = locator.nth(index)
            try:
                if not candidate.is_visible():
                    continue
                text = candidate.inner_text(timeout=1000)
                aria = candidate.get_attribute("aria-label") or ""
                title = candidate.get_attribute("title") or ""
                if cls._label_matches(" ".join((text, aria, title)), label):
                    return candidate
            except Exception:
                continue
        return None

    @staticmethod
    def _visible(locator, timeout=2500):
        return ReportAutomator._first_visible(locator, timeout)

    @staticmethod
    def _contexts(page):
        contexts = [page]
        try:
            contexts.extend(frame for frame in page.frames if frame != page.main_frame)
        except Exception:
            pass
        return contexts

    @classmethod
    def _first_visible_in_contexts(cls, page, selector, timeout=2500):
        """Find a form control in the page or one of its iframes."""
        for context in cls._contexts(page):
            try:
                candidate = cls._first_visible(context.locator(selector), timeout)
            except Exception:
                candidate = None
            if candidate:
                return candidate
        return None

    @staticmethod
    def _wait_for_new_page(context, known_pages, timeout=6000):
        """Wait for a click-triggered popup without requiring an async event handler."""
        deadline = time.monotonic() + timeout / 1000
        known = {id(item) for item in known_pages}
        while time.monotonic() < deadline:
            try:
                pages = list(context.pages)
            except Exception:
                return None
            for candidate in pages:
                if id(candidate) not in known:
                    try:
                        if not candidate.is_closed():
                            return candidate
                    except Exception:
                        return candidate
            if pages:
                try:
                    pages[0].wait_for_timeout(200)
                except Exception:
                    time.sleep(0.2)
            else:
                time.sleep(0.2)
        return None

    @classmethod
    def _is_details_page(cls, page):
        """Recognize B 站's role-based copyright complaint flow."""
        try:
            url = str(page.url or "").lower()
        except Exception:
            url = ""
        if "/pc/community/copyright" in url:
            return True
        try:
            text = page.locator("body").inner_text(timeout=1500)
        except Exception:
            text = ""
        normalized = cls._normalize_text(text)
        return ("填写我的信息" in normalized and "填写举证材料" in normalized) or (
            "账号实名认证" in normalized and "举证材料" in normalized)

    def _copyright_description(self, row):
        """Render the (user editable) infringement statement for a record.

        The built-in wording names the translate-and-reupload pattern used by the
        monitored account and appends the coin-cap finding when it was detected.
        """
        return report_description(row, self.report_text)

    def _click_copyright_submit(self, page):
        """Click the submit control on Bilibili's full-page copyright form."""
        for context in self._contexts(page):
            for selector in (
                    "button.copyright-index__button--submit",
                    'button[class*="copyright-index__button--submit"]'):
                try:
                    candidate = self._first_visible(context.locator(selector), timeout=1200)
                except Exception:
                    candidate = None
                if candidate:
                    candidate.click()
                    page.wait_for_timeout(250)
                    return
            try:
                candidate = self._find_clickable(
                    context.locator('button, [role="button"]'), "提交举报")
            except Exception:
                candidate = None
            if candidate:
                candidate.click()
                page.wait_for_timeout(250)
                return
        self._click_text(page, ("提交举报",))

    def _submit_copyright(self, page, row):
        """Complete B 站's role-based copyright form from the explicit source link."""
        source = self._first_visible_in_contexts(
            page, 'textarea[placeholder*="首发链接"], textarea[placeholder*="原创作品"]')
        description = self._first_visible_in_contexts(
            page, 'textarea[placeholder*="具体片段"], textarea[placeholder*="举报说明"]')

        # The role and evidence steps can be separate pages or same-page
        # navigations. Skip role selection when the evidence fields are ready.
        if not source or not description:
            role = None
            for context in self._contexts(page):
                try:
                    role = self._find_clickable(
                        context.locator(".copyright-role--select .select-container"), "路人")
                except Exception:
                    role = None
                if role:
                    break
            if not role:
                self._click_text(page, ("路人",))
            else:
                role.click()
                page.wait_for_timeout(300)

            next_button = None
            for context in self._contexts(page):
                try:
                    next_button = self._find_clickable(
                        context.locator("button.button-next, .button-next"), "下一步")
                except Exception:
                    next_button = None
                if next_button:
                    break
            if next_button:
                next_button.click()
            else:
                self._click_text(page, ("下一步",))
            page.wait_for_timeout(700)
            source = self._first_visible_in_contexts(
                page, 'textarea[placeholder*="首发链接"], textarea[placeholder*="原创作品"]')
            description = self._first_visible_in_contexts(
                page, 'textarea[placeholder*="具体片段"], textarea[placeholder*="举报说明"]')

        if not source:
            raise RuntimeError("版权投诉页未找到原创出处输入框")
        source_url = str(row.get("source_url") or "").strip()
        if not source_url:
            raise RuntimeError("该记录没有明确原片链接，无法填写版权出处")
        source.fill(source_url)

        if not description:
            raise RuntimeError("版权投诉页未找到举报说明输入框")
        description.fill(self._copyright_description(row))
        self._click_copyright_submit(page)
        page.wait_for_timeout(900)
        try:
            text = self._normalize_text(page.locator("body").inner_text(timeout=1500))
        except Exception:
            text = ""
        if text and not any(token in text for token in ("提交成功", "举报成功", "投诉成功")):
            logging.info("copyright report clicked; success text not visible for %s", row.get("bvid"))

    @classmethod
    def _requires_details_step(cls, reason):
        """Copyright and rights reasons use a separate complaint workflow."""
        normalized = cls._normalize_text(reason)
        return any(token in normalized for token in (
            "侵权", "知识产权", "人身权益", "企业权益", "版权"))

    def _visible_scopes(self, page):
        selectors = (
            '[role="dialog"]:visible', '[role="menu"]:visible',
            '[class*="dialog"]:visible', '[class*="modal"]:visible',
            '[class*="report"]:visible', '[class*="reason"]:visible',
            '[class*="popup"]:visible', '[class*="menu"]:visible',
        )
        scopes = []
        for context in self._contexts(page):
            matched = False
            for selector in selectors:
                try:
                    locator = context.locator(selector)
                    for index in range(min(locator.count(), 20)):
                        scopes.append(locator.nth(index))
                        matched = True
                except Exception:
                    continue
            # Include the full iframe as well as nested sections: the reason
            # list and its footer buttons live in separate sibling elements.
            if context is not page:
                scopes.append(context)
        return scopes or [page]

    @staticmethod
    def _scope_text(scope, timeout=1000):
        try:
            return scope.inner_text(timeout=timeout)
        except Exception:
            try:
                return scope.locator("body").inner_text(timeout=timeout)
            except Exception:
                return ""

    def _visible_summary(self, page, limit=1400):
        texts = []
        for scope in self._visible_scopes(page):
            text = re.sub(r"\s+", " ", self._scope_text(scope)).strip()
            if text and text not in texts:
                texts.append(text)
        summary = " | ".join(texts)
        return summary[:limit] or "无可见弹窗文本"

    def _save_debug(self, page, bvid, reason):
        """Persist local-only DOM evidence while leaving the failed page open."""
        try:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            safe_bvid = re.sub(r"[^A-Za-z0-9_-]", "_", str(bvid or "unknown"))
            base = self.debug_dir / f"{datetime.now().strftime('%Y%m%d-%H%M%S')}-{safe_bvid}"
            try:
                (base.with_suffix(".html")).write_text(page.content(), encoding="utf-8")
            except Exception:
                pass
            try:
                for index, frame in enumerate(page.frames[1:], start=1):
                    (base.parent / f"{base.name}.frame-{index}.html").write_text(
                        frame.content(), encoding="utf-8")
            except Exception:
                pass
            try:
                body = page.locator("body").inner_text(timeout=2000)
            except Exception:
                body = ""
            details = [f"URL: {getattr(page, 'url', '')}", f"原因: {reason}",
                       "可见弹窗文本: " + self._visible_summary(page), "", body[:12000]]
            (base.with_suffix(".txt")).write_text("\n".join(details), encoding="utf-8")
            try:
                page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
            except Exception:
                pass
            return str(base)
        except Exception:
            return ""

    def _click_in_scope(self, scope, label):
        pattern = re.compile(re.escape(label), re.I)
        for role in ("radio", "button", "menuitem", "option", "listitem"):
            try:
                candidate = self._find_clickable(scope.get_by_role(role, name=pattern), label)
            except Exception:
                candidate = None
            if candidate:
                return candidate
        try:
            candidate = self._find_clickable(
                scope.locator('button, [role="button"], [role="radio"], '
                              '[role="menuitem"], [role="option"], li, label, a'), label)
        except Exception:
            candidate = None
        if candidate:
            return candidate
        try:
            return self._first_visible(scope.get_by_text(pattern, exact=False), timeout=1200)
        except Exception:
            return None

    def _click_text(self, page, labels, timeout=5000):
        deadline = time.monotonic() + timeout / 1000
        while True:
            for label in labels:
                for scope in self._visible_scopes(page):
                    candidate = self._click_in_scope(scope, label)
                    if candidate:
                        candidate.click(timeout=timeout)
                        page.wait_for_timeout(250)
                        return candidate
            if time.monotonic() >= deadline:
                break
            try:
                page.wait_for_timeout(150)
            except Exception:
                time.sleep(0.15)
        raise RuntimeError("网页中未找到：" + "/".join(labels) +
                           "；可见弹窗文本：" + self._visible_summary(page))

    def _click_report(self, page):
        """B 站菜单 DOM 经常变化，优先按语义和可见菜单项定位。"""
        patterns = (
            page.get_by_role("menuitem", name=re.compile("举报|报告", re.I)),
            page.get_by_role("button", name=re.compile("举报|报告", re.I)),
            page.locator('[aria-label*="举报"], [title*="举报"], '
                         '[aria-label*="报告"], [title*="报告"]'),
            page.get_by_text(re.compile("举报|报告", re.I), exact=False),
        )
        for locator in patterns:
            candidate = self._first_visible(locator, timeout=1200)
            if candidate:
                candidate.click()
                page.wait_for_timeout(350)
                return
        # Some B 站 builds render a plain div/li without an ARIA role.
        for scope in self._visible_scopes(page):
            try:
                candidate = self._find_clickable(
                    scope.locator('button, [role="button"], [role="menuitem"], '
                                  '[role="option"], li, label, a, div'), "举报")
            except Exception:
                candidate = None
            if candidate:
                candidate.click()
                page.wait_for_timeout(350)
                return
        for scope in self._visible_scopes(page):
            candidate = self._click_in_scope(scope, "举报")
            if candidate:
                candidate.click()
                page.wait_for_timeout(350)
                return
        raise RuntimeError("举报菜单项未出现；请确认已登录并已打开视频操作菜单；"
                           "可见弹窗文本：" + self._visible_summary(page))

    def _click_more(self, page):
        selectors = (
            'button[aria-label*="更多"]', '[aria-label*="更多"]',
            'button[title*="更多"]', '[title*="更多"]',
            'button:has-text("更多")', '[role="button"]:has-text("更多")',
            '.video-toolbar-right .more',
            '[class*="video-toolbar"] [class*="more"]',
            '[class*="toolbar"] [class*="more"]',
            '.bpx-player-ctrl-more',
            '[class*="bpx-player-ctrl-more"]',
            '[data-name*="more"], [data-action*="more"]',
            'button[class*="more"], [role="button"][class*="more"]',
        )
        for selector in selectors:
            try:
                locator = page.locator(selector)
                for index in range(min(locator.count(), 20)):
                    candidate = locator.nth(index)
                    if candidate.is_visible():
                        candidate.click()
                        page.wait_for_timeout(300)
                        return
            except Exception:
                continue
        raise RuntimeError("视频页面中未找到更多操作菜单；请确认页面已加载且账号已登录")

    def submit(self, row, reason=REPORT_REASON):
        if not self.feed.context or not self.feed.visible:
            raise RuntimeError("请先打开独立监控浏览器")
        if not self.feed.login_confirmed:
            raise RuntimeError("请先在独立监控浏览器中登录并点击“我已登录，开始举报”")
        if self.last_failed_page and not self.last_failed_page.is_closed():
            self.last_failed_page.close()
        self.last_failed_page = None
        page = self.feed.context.new_page()
        active_page = page
        succeeded = False
        try:
            page.set_default_timeout(8000)
            page.goto(row["url"], wait_until="domcontentloaded", timeout=30000)
            page.locator("body").wait_for(state="visible", timeout=10000)
            page.wait_for_timeout(800)
            try:
                # Current B 站 exposes “稿件举报” directly in the toolbar.
                self._click_report(page)
            except RuntimeError:
                self._click_more(page)
                self._click_report(page)
            known_pages = list(self.feed.context.pages)
            self._click_text(page, (reason, "侵犯个人知识产权", "侵犯合法权益", "版权", "其他"))
            if self._requires_details_step(reason):
                self._click_text(page, ("下一步",))
            page.wait_for_timeout(600)
            if self._is_details_page(page):
                active_page = page
                self._submit_copyright(page, row)
                succeeded = True
                return
            popup = self._wait_for_new_page(self.feed.context, known_pages)
            if popup:
                active_page = popup
                try:
                    popup.set_default_timeout(8000)
                    popup.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                popup.wait_for_timeout(600)
                if self._is_details_page(popup):
                    self._submit_copyright(popup, row)
                    active_page = popup
                    succeeded = True
                    return
            description = self._first_visible_in_contexts(active_page,
                'textarea, input[placeholder*="描述"], textarea[placeholder*="说明"]')
            field = description
            if field:
                field.fill(self._copyright_description(row))
            self._click_text(active_page, ("提交举报", "提交", "确认举报", "确定"))
            try:
                active_page.get_by_text("举报成功", exact=False).wait_for(
                    state="visible", timeout=5000)
            except Exception:
                # Some versions close the dialog without rendering a success toast.
                pass
            succeeded = True
        except ReportNeedsDetails:
            # Keep the user-completed copyright form open; it is not a failed report.
            self.last_failed_page = active_page
            raise
        except Exception as exc:
            # Keep the failed page open so the user can inspect login, captcha, or DOM state.
            self.last_failed_page = active_page
            debug_base = self._save_debug(active_page, row.get("bvid", "unknown"), str(exc))
            if debug_base:
                raise RuntimeError(f"{exc}；诊断文件：{debug_base}") from exc
            raise
        finally:
            if succeeded:
                for candidate in (active_page, page):
                    try:
                        if not candidate.is_closed():
                            candidate.close()
                    except Exception:
                        pass


def scan(feed, store, stop=None, page_limit=20, uid=None):
    """Check a fresh segment and resume unfinished older segments across scans."""
    uid = uid or store.uid()
    payload = feed.first()
    rows, more, offset = parse_feed(payload, uid)
    if not store.get("initialized", False):
        if not rows:
            raise RuntimeError("未读到可建立基线的视频；本次不标记初始化完成。")
        added = store.ingest(rows, baseline=True)
        normal = [r["published_ts"] for r in rows if not r["pinned"] and r["published_ts"]]
        store.set("baseline_cutoff", min(normal) if normal else 0)
        store.set("initialized", True)
        return {"baseline": True, "added": 0, "seen": len(added), "pages": 1, "backlog": 0}

    pending = store.get("backlog", [])
    added, pages, visited = [], 0, set()
    current = payload
    try:
        while True:
            if stop and stop.is_set():
                break
            rows, more, offset = parse_feed(current, uid)
            known = store.known([row["bvid"] for row in rows])
            hit = any(row["bvid"] in known and not row["pinned"] for row in rows)
            added.extend(r for r in store.ingest(rows) if not r["baseline"])
            pages += 1
            if more and not offset:
                raise RuntimeError("B 站分页缺少游标；已保存读到的记录，后续需补查。")
            if more and not hit:
                if offset in visited:
                    raise RuntimeError("B 站返回重复分页游标；本次停止翻页。")
                if offset not in pending:
                    pending.insert(0, offset)
                if pages >= page_limit or (stop and stop.wait(0.4)):
                    break
                visited.add(offset)
                current = feed.next(offset)
                pending.remove(offset)
                continue
            if not pending or pages >= page_limit:
                break
            next_offset = pending[0]
            current = feed.next(next_offset)
            pending.pop(0)
    finally:
        store.set("backlog", pending)
        store.event(added)
    return {"baseline": False, "added": len(added), "seen": len(added),
            "pages": pages, "backlog": len(pending)}


class Monitor:
    def __init__(self, store, data_dir):
        self.store = store
        self.data_dir = data_dir
        self.lock = threading.RLock()
        self.wake = threading.Event()
        self.stop = threading.Event()
        self.enabled = True
        self.show_browser = False
        self.confirm_browser_login = False
        self.check_requested = True
        self.interval = int(store.get("interval", 600))
        self.uid = store.uid()
        self.mode = store.get("mode", "manual")
        if self.mode not in MODES:
            self.mode = "manual"
        self.report_text = store.get("report_text", REPORT_TEXT_DEFAULT)
        if not isinstance(self.report_text, str) or not self.report_text.strip():
            self.report_text = REPORT_TEXT_DEFAULT
        self.coin_watch = bool(store.get("coin_watch", True))
        self.coin_probe_bvid = ""
        self.coin_probe_all = False
        self.state = {"phase": "starting", "message": "正在启动监控", "last_check": "",
                      "next_check": "", "last_result": None}
        self.browser_open = False
        self.browser_login_ready = False
        self.thread = threading.Thread(target=self.run, name="bili-monitor", daemon=True)

    def status(self):
        with self.lock:
            return {**self.state, "version": APP_VERSION,
                    "enabled": self.enabled, "interval": self.interval,
                    "mode": self.mode,
                    "report_text": self.report_text,
                    "report_text_default": REPORT_TEXT_DEFAULT,
                    "report_placeholders": REPORT_PLACEHOLDERS,
                    "coin_watch": self.coin_watch,
                    "browser_open": self.browser_open,
                    "browser_login_ready": self.browser_login_ready,
                    "uid": self.uid, "space_url": space_url(self.uid),
                    "data_dir": str(self.data_dir)}

    def _publish_browser_state(self, feed):
        with self.lock:
            self.browser_open = feed.browser_open()
            self.browser_login_ready = bool(feed.login_confirmed and self.browser_open)

    def command(self, body):
        action = body.get("action")
        with self.lock:
            if action == "pause":
                self.enabled = False
                self.state.update(phase="paused", message="已暂停自动检查")
            elif action == "resume":
                self.enabled = True
                self.check_requested = True
            elif action == "check":
                self.check_requested = True
            elif action == "mode":
                mode = body.get("mode")
                if mode not in MODES:
                    raise ValueError("处理模式无效")
                self.mode = mode
                self.store.set("mode", mode)
                self.check_requested = True
                self.state.update(message=("已切换到自动举报：仅处理有明确原片链接的新投稿"
                                            if mode == "auto" else "已切换到人工审核"))
            elif action == "uid":
                uid = self.store.change_uid(body.get("uid"))
                self.uid = uid
                self.check_requested = True
                self.state.update(phase="starting", last_result=None,
                                  message=f"已切换到 UID {uid}；将为该账号重新建立监控基线")
            elif action == "browser":
                self.show_browser = True
                self.state.update(phase="waiting_login", message="正在打开独立监控浏览器；请在窗口内完成 B 站登录")
            elif action == "browser_ready":
                self.confirm_browser_login = True
                self.check_requested = True
            elif action == "retry_report":
                bvid = body.get("bvid")
                self.store.retry_report(bvid)
                self.check_requested = True
                self.state.update(message=f"{bvid} 已重新加入举报队列")
            elif action == "report_text":
                value = validate_report_text(body.get("text"))
                self.report_text = value
                self.store.set("report_text", value)
                self.state.update(message=("举报词已保存（内置文案）" if value == REPORT_TEXT_DEFAULT
                                           else "举报词已保存"))
            elif action == "report_text_reset":
                self.report_text = REPORT_TEXT_DEFAULT
                self.store.set("report_text", REPORT_TEXT_DEFAULT)
                self.state.update(message="举报词已恢复为内置文案")
            elif action == "coin_watch":
                self.coin_watch = bool(body.get("enabled", True))
                self.store.set("coin_watch", self.coin_watch)
                self.state.update(message=("已开启新投稿的投币上限自动检测" if self.coin_watch
                                           else "已关闭投币上限自动检测"))
                if self.coin_watch:
                    self.check_requested = True
            elif action == "coin_probe":
                bvid = str(body.get("bvid") or "").strip()
                if not re.fullmatch(r"BV[0-9A-Za-z]{10}", bvid):
                    raise ValueError("BV 号无效，无法检测投币上限")
                self.coin_probe_bvid = bvid
                self.check_requested = True
                self.state.update(phase="checking", message=f"正在检测 {bvid} 的投币上限")
            elif action == "coin_all":
                self.coin_probe_all = True
                self.check_requested = True
                self.state.update(phase="checking", message="正在重新检测已存档投稿的投币上限")
            elif action == "interval":
                interval = int(body.get("seconds", 600))
                if not 120 <= interval <= 86400:
                    raise ValueError("检查间隔需在 2 分钟至 24 小时之间")
                self.interval = interval
                self.store.set("interval", interval)
                self.check_requested = self.enabled
            else:
                raise ValueError("操作无效")
        self.wake.set()

    def _probe_coins(self):
        """Read B 站's 自制/转载 mark for pending or requested videos.

        The video detail endpoint is public, so this needs no browser session and
        still works while automatic reporting waits for a login. A backlog found by
        one scan is drained in batches of COIN_PROBE_BATCH instead of waiting for the
        next scheduled check.
        """
        with self.lock:
            one = self.coin_probe_bvid
            all_records = self.coin_probe_all
            self.coin_probe_bvid = ""
            self.coin_probe_all = False
            watch = self.coin_watch
        if not (watch or one or all_records):
            return None
        totals = {"checked": 0, "failed": 0}

        def drain(rows):
            result = probe_coins(self.store, rows, self.stop)
            totals["checked"] += result["checked"]
            totals["failed"] += result["failed"]

        if one:
            rows = self.store.coin_pending(bvid=one)
            if rows:
                drain(rows)
            if not watch and not all_records:
                return totals
        if all_records:
            for index in range(COIN_PROBE_PASSES):
                rows = self.store.coin_pending(refresh_all=True, offset=index * COIN_PROBE_BATCH)
                if not rows:
                    break
                drain(rows)
                if self.stop.is_set():
                    break
            return totals
        for _ in range(COIN_PROBE_PASSES):
            rows = self.store.coin_pending()
            if not rows:
                break
            drain(rows)
            if self.stop.is_set():
                break
        return totals

    def auto_report(self, feed):
        if self.mode != "auto":
            return {"queued": 0, "submitted": 0, "failed": 0, "needs_details": 0}
        if not getattr(feed, "login_confirmed", False):
            return {"queued": 0, "submitted": 0, "failed": 0, "needs_details": 0,
                    "waiting_login": True}
        candidates = self.store.queue_auto_reports()
        summary = {"queued": len(candidates), "submitted": 0, "failed": 0,
                   "needs_details": 0, "waiting_login": False}
        automator = ReportAutomator(feed, self.report_text)
        for row in candidates:
            if self.stop.is_set():
                break
            self.store.mark_report(row["bvid"], "submitting")
            try:
                automator.submit(row)
            except ReportNeedsDetails as exc:
                summary["needs_details"] += 1
                self.store.mark_report(row["bvid"], "needs_details", str(exc))
                logging.info("report needs user details for %s: %s", row["bvid"], exc)
                # Do not close the open complaint form by starting another report.
                break
            except Exception as exc:
                summary["failed"] += 1
                self.store.mark_report(row["bvid"], "failed", str(exc))
                logging.exception("automatic report failed for %s", row["bvid"])
            else:
                summary["submitted"] += 1
                self.store.mark_report(row["bvid"], "submitted", "已通过监控浏览器提交")
        return summary

    def run(self):
        feed = BrowserFeed(self.data_dir / "EdgeProfile")
        due = 0
        failures = 0
        try:
            while not self.stop.is_set():
                self._publish_browser_state(feed)
                with self.lock:
                    show = self.show_browser
                    self.show_browser = False
                    confirm_login = self.confirm_browser_login
                    self.confirm_browser_login = False
                    requested = self.check_requested
                    run = requested or (self.enabled and time.time() >= due)
                    if run:
                        self.check_requested = False
                feed.set_uid(self.uid)
                if show:
                    try:
                        feed.open(visible=True)
                        feed.go_to_space()
                        self._publish_browser_state(feed)
                        with self.lock:
                            self.state.update(phase="waiting_login",
                                              message="独立监控浏览器已打开；请在该窗口登录 B 站，完成后回到工具点“我已登录，开始举报”。")
                    except Exception as exc:
                        logging.exception("open browser")
                        with self.lock:
                            self.state.update(phase="error", message=str(exc))
                if confirm_login:
                    try:
                        feed.confirm_login()
                        self._publish_browser_state(feed)
                        requested = True
                        run = True
                        with self.lock:
                            self.state.update(phase="checking",
                                              message="已检测到独立浏览器登录状态，开始读取投稿")
                    except Exception as exc:
                        self._publish_browser_state(feed)
                        with self.lock:
                            self.state.update(phase="waiting_login", message=str(exc))
                if run:
                    coins = self._probe_coins()
                    if self.mode == "auto" and (not feed.browser_open() or
                                                  not feed.login_confirmed):
                        self._publish_browser_state(feed)
                        with self.lock:
                            self.state.update(phase="waiting_login",
                                              message="自动举报等待登录：先点“打开登录窗口”，在独立浏览器登录后再点“我已登录，开始举报”。")
                        due = time.time() + self.interval
                        with self.lock:
                            self.state["next_check"] = stamp(due) if self.enabled else ""
                        self.wake.wait(1)
                        self.wake.clear()
                        continue
                    with self.lock:
                        self.state.update(phase="checking", message="正在读取投稿并去重")
                    try:
                        result = scan(feed, self.store, self.stop, uid=self.uid)
                        failures = 0
                        # 本次新抓到的投稿也要立刻看投币上限，不等下一个检查周期。
                        fresh = self._probe_coins()
                        if fresh:
                            coins = {key: (coins or {}).get(key, 0) + fresh[key]
                                     for key in ("checked", "failed")}
                        msg = (f"已建立基线：{result['seen']} 条现有投稿" if result["baseline"]
                               else f"本次发现 {result['added']} 条新增投稿")
                        reports = self.auto_report(feed)
                        if self.mode == "auto" and reports["queued"]:
                            msg += (f"；自动举报提交 {reports['submitted']} 条"
                                    f"，需补充材料 {reports['needs_details']} 条"
                                    f"，失败 {reports['failed']} 条")
                        elif self.mode == "auto" and reports.get("waiting_login"):
                            msg = "已读取投稿；自动举报等待独立浏览器登录确认"
                        if coins and coins["checked"]:
                            msg += f"；已检测 {coins['checked']} 条投稿的投币上限"
                        if coins and coins["failed"]:
                            msg += f"；{coins['failed']} 条投币检测失败，可在记录中重新检测"
                        self._publish_browser_state(feed)
                        if result["backlog"]:
                            msg += "；还有历史分页，下一次继续补查"
                        with self.lock:
                            self.state.update(phase="ready", message=msg,
                                              last_check=stamp(), last_result=result)
                    except Exception as exc:
                        failures += 1
                        logging.exception("scan failed")
                        message = str(exc)
                        if coins and coins["checked"]:
                            message += f"；已检测 {coins['checked']} 条投稿的投币上限"
                        with self.lock:
                            self.state.update(phase="error", message=message,
                                              last_check=stamp())
                    due = time.time() + min(21600, self.interval * (2 ** min(failures, 5)))
                    with self.lock:
                        self.state["next_check"] = stamp(due) if self.enabled else ""
                self.wake.wait(1)
                self.wake.clear()
        finally:
            feed.close()


class Handler(BaseHTTPRequestHandler):
    server_version = f"BiliWatch/{APP_VERSION}"

    def log_message(self, fmt, *args):
        logging.info(fmt, *args)

    def send(self, data, status=200, content_type="application/json; charset=utf-8", filename=""):
        if not isinstance(data, bytes):
            data = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; "
                         "style-src 'self'; img-src 'self' data:; connect-src 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        if filename:
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.end_headers()
        self.wfile.write(data)

    def allowed_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def do_GET(self):
        if not self.allowed_host():
            self.send({"error": "Host rejected"}, 403)
            return
        parts = urlsplit(self.path)
        query = parse_qs(parts.query)
        try:
            if parts.path == "/api/status":
                self.send({**self.server.monitor.status(), "token": self.server.token})
            elif parts.path == "/api/videos":
                page = max(1, min(100000, int(query.get("page", ["1"])[0])))
                self.send(self.server.store.list(query.get("q", [""])[0],
                                                query.get("status", [""])[0], page))
            elif parts.path == "/api/export":
                rows = self.server.store.export()
                output = io.StringIO()
                columns = ["bvid", "uid", "title", "url", "published_at", "first_seen",
                           "baseline", "copyright", "coin_limit", "coin_note", "coin_checked_at",
                           "source_url", "source_kind", "status", "evidence_url",
                           "notes", "description", "report_status", "report_message",
                           "reported_at"]
                writer = csv.DictWriter(output, fieldnames=columns, extrasaction="ignore")
                writer.writeheader()
                for row in rows:
                    # Keep spreadsheet programs from treating untrusted titles as formulas.
                    writer.writerow({k: "'" + v if isinstance(v, str) and v[:1] in
                                     {"=", "+", "-", "@", "\t", "\r"} else v
                                     for k, v in row.items()})
                self.send(output.getvalue().encode("utf-8-sig"),
                          content_type="text/csv; charset=utf-8", filename="bili-evidence.csv")
            elif parts.path in {"/", "/index.html", "/style.css", "/main.js",
                                "/logo.svg", "/logo-512.png", "/logo-128.png",
                                "/icon.ico", "/favicon.ico"}:
                name = "index.html" if parts.path == "/" else parts.path[1:]
                if name == "favicon.ico":
                    name = "icon.ico"
                mime = {"index.html": "text/html; charset=utf-8",
                        "style.css": "text/css; charset=utf-8",
                        "main.js": "text/javascript; charset=utf-8",
                        "logo.svg": "image/svg+xml", "logo-512.png": "image/png",
                        "logo-128.png": "image/png", "icon.ico": "image/x-icon"}[name]
                self.send((self.server.web_dir / name).read_bytes(), content_type=mime)
            else:
                self.send({"error": "Not found"}, 404)
        except (ValueError, TypeError) as exc:
            self.send({"error": str(exc)}, 400)
        except Exception:
            logging.exception("GET failed")
            self.send({"error": "本地读取失败，详情见 monitor.log"}, 500)

    def do_POST(self):
        origin = self.headers.get("Origin")
        expected = f"http://127.0.0.1:{self.server.server_port}"
        if (not self.allowed_host() or origin not in {None, expected} or
                self.headers.get("X-Local-Token") != self.server.token):
            length = int(self.headers.get("Content-Length", "0"))
            if 0 < length <= 65536:
                self.rfile.read(length)
            self.send({"error": "Local request rejected"}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError("请求长度无效")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("请求需为 JSON 对象")
            if self.path == "/api/control":
                self.server.monitor.command(body)
                self.send({"ok": True})
            elif self.path.startswith("/api/video/"):
                self.server.store.update(self.path.removeprefix("/api/video/"), body)
                self.send({"ok": True})
            elif self.path == "/api/shutdown":
                self.send({"ok": True})
                self.server.monitor.stop.set()
                self.server.monitor.wake.set()
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self.send({"error": "Not found"}, 404)
        except (ValueError, TypeError) as exc:
            self.send({"error": str(exc)}, 400)
        except Exception:
            logging.exception("POST failed")
            self.send({"error": "本地保存失败，详情见 monitor.log"}, 500)


def create_server(data_dir, port=0):
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    store = Store(data_dir / "records.sqlite3")
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.token = secrets.token_urlsafe(32)
    server.web_dir = Path(__file__).parent / "web"
    server.store = store
    server.monitor = Monitor(store, data_dir)
    return server


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", default=str(Path(os.environ.get("LOCALAPPDATA", ".")) / "BiliWatch"))
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--no-monitor", action="store_true")
    args = parser.parse_args()
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    instance_lock = open(data_dir / "instance.lock", "a+b")
    if os.name == "nt":
        import msvcrt
        if instance_lock.tell() == 0:
            instance_lock.write(b"0")
            instance_lock.flush()
        instance_lock.seek(0)
        try:
            msvcrt.locking(instance_lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            try:
                running = json.loads((data_dir / "running.json").read_text(encoding="utf-8"))
                if not args.no_browser:
                    webbrowser.open(running["url"])
            except (OSError, ValueError, KeyError):
                pass
            instance_lock.close()
            return
    logging.basicConfig(filename=data_dir / "monitor.log", level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8")
    server = create_server(data_dir, args.port)
    url = f"http://127.0.0.1:{server.server_port}"
    (data_dir / "running.json").write_text(json.dumps({"url": url, "pid": os.getpid()}), encoding="utf-8")
    if not args.no_monitor:
        server.monitor.thread.start()
    if not args.no_browser:
        webbrowser.open(url)
    print(url, flush=True)
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.monitor.stop.set()
        server.monitor.wake.set()
        server.server_close()
        if server.monitor.thread.is_alive():
            server.monitor.thread.join(timeout=8)
        instance_lock.close()


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
