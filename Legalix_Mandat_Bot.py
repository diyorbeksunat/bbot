#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
LEGALIX — OTM MANDAT 2026 Telegram Bot V16

Designed as a clean stateful bot:
- Current result lookup by 7-digit candidate ID
- Does NOT subscribe automatically on lookup
- Explicit application/confirmation flow for mandate monitoring
- Multiple saved candidates per Telegram user
- Current result refresh
- Official-page PDF discovery/download
- Safe final-result detection (doesn't confuse current score with final mandate)
- Automatic notification when a final mandate signal appears
- Ranking discovery hooks: overall + direction/competition ranking when official
  public endpoints are exposed. Never invents a rank.
- Admin stats, monitoring status, broadcast
- SQLite + automatic schema migration

Install:
  pip install aiogram==3.* requests beautifulsoup4

Run:
  python Legalix_Mandat_Bot_V16_SINGLE_MESSAGE.py
"""

import asyncio
import hashlib
import html
import io
import json
import logging
import re
import sqlite3
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup
from aiogram import Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.filters import Command, CommandStart
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton

# ============================================================
# CONFIG
# ============================================================
BOT_TOKEN = "8611100478:AAFV-rx5qI_CpOc4SVFGrY2tZSZniNiavMM"
ADMIN_IDS = {7880323063}

BASE_URL = "https://mandat.uzbmb.uz"
HOME_URL = f"{BASE_URL}/"
DB_PATH = Path("mandat_bot.sqlite3")
CHECK_INTERVAL = 2
REQUEST_TIMEOUT = 8
RANK_SPEC_CACHE_TTL = 1800
RANK_CACHE_TTL = 300
MAX_RETRIES = 1
MAX_BATCH = 500
USER_COOLDOWN = 1

ID_RE = re.compile(r"^\d{7}$")
HASH_RE = re.compile(r"[a-f0-9]{32,128}", re.I)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("legalix-mandat")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def clean(text: Any) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def esc(text: Any) -> str:
    return html.escape(str(text)) if text is not None else ""


def first(patterns: list[str], text: str) -> Optional[str]:
    for p in patterns:
        m = re.search(p, text, re.I)
        if m:
            return clean(m.group(1))
    return None


@dataclass
class RankInfo:
    rank: Optional[int] = None
    total: Optional[int] = None
    scope: Optional[str] = None
    source_url: Optional[str] = None


@dataclass
class Result:
    candidate_id: str
    name: Optional[str] = None
    education_language: Optional[str] = None
    mandatory_correct: Optional[str] = None
    mandatory_score: Optional[str] = None
    specialist_correct: Optional[str] = None
    specialist_score: Optional[str] = None
    second_specialist_correct: Optional[str] = None
    second_specialist_score: Optional[str] = None
    privilege_score: Optional[str] = None
    creative_score: Optional[str] = None
    cefr_score: Optional[str] = None
    national_cert_score: Optional[str] = None
    total_score: Optional[str] = None
    university: Optional[str] = None
    direction: Optional[str] = None
    education_form: Optional[str] = None
    specialist_subject_1: Optional[str] = None
    specialist_subject_2: Optional[str] = None
    rank_overall: Optional[RankInfo] = None
    rank_direction: Optional[RankInfo] = None
    status: Optional[str] = None
    pdf_url: Optional[str] = None
    result_url: Optional[str] = None
    raw_text: str = ""

    @property
    def is_final(self) -> bool:
        # Final mandate is accepted only when an explicit placement/status
        # statement is present or the official final-mandate page is used.
        status_low = clean(self.status).lower() if self.status else ""
        explicit = any(k in status_low for k in (
            "tavsiya etildi",
            "tavsiya etilmadi",
            "davlat granti",
            "to‘lov-kontrakt",
            "to'lov-kontrakt",
            "talabalikka"
        ))
        url_low = (self.result_url or "").lower()
        final_path = any(k in url_low for k in ("mandat2026", "/final/", "/yakuniy/"))
        return explicit or final_path

    @property
    def fingerprint(self) -> str:
        relevant = {
            "candidate_id": self.candidate_id,
            "name": self.name,
            "total_score": self.total_score,
            "status": self.status,
            "university": self.university,
            "direction": self.direction,
            "education_form": self.education_form,
            "rank_overall": asdict(self.rank_overall) if self.rank_overall else None,
            "rank_direction": asdict(self.rank_direction) if self.rank_direction else None,
            "pdf_url": self.pdf_url,
        }
        return hashlib.sha256(json.dumps(relevant, ensure_ascii=False, sort_keys=True).encode()).hexdigest()

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @staticmethod
    def from_json(value: str) -> Optional["Result"]:
        try:
            data = json.loads(value)
            for key in ("rank_overall", "rank_direction"):
                if data.get(key):
                    data[key] = RankInfo(**data[key])
            return Result(**data)
        except Exception:
            return None


# ============================================================
# DATABASE
# ============================================================
class DB:
    def __init__(self, path: Path):
        self.conn = sqlite3.connect(str(path), check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = asyncio.Lock()
        self._init()

    def _init(self):
        c = self.conn.cursor()
        c.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER NOT NULL,
                username TEXT,
                full_name TEXT,
                candidate_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'waiting',
                latest_json TEXT,
                result_hash TEXT,
                last_checked TEXT,
                notified_at TEXT,
                result_url TEXT,
                created_at TEXT NOT NULL,
                UNIQUE(telegram_id, candidate_id)
            )
            """
        )
        cols = {r[1] for r in c.execute("PRAGMA table_info(subscriptions)")}
        migrations = {
            "username": "TEXT",
            "full_name": "TEXT",
            "status": "TEXT NOT NULL DEFAULT 'waiting'",
            "latest_json": "TEXT",
            "result_hash": "TEXT",
            "last_checked": "TEXT",
            "notified_at": "TEXT",
            "result_url": "TEXT",
            "created_at": "TEXT",
        }
        for name, typ in migrations.items():
            if name not in cols:
                try:
                    c.execute(f"ALTER TABLE subscriptions ADD COLUMN {name} {typ}")
                    log.info("DB migration: added %s", name)
                except sqlite3.OperationalError as e:
                    log.warning("DB migration failed %s: %s", name, e)
        c.execute("UPDATE subscriptions SET status='waiting' WHERE status IS NULL OR status='' ")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sub_status ON subscriptions(status)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sub_user ON subscriptions(telegram_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sub_candidate ON subscriptions(candidate_id)")
        self.conn.commit()

    async def set_result_url(self, uid: int, cid: str, result_url: str, result: Result | None = None):
        async with self.lock:
            if result is not None:
                self.conn.execute(
                    """UPDATE subscriptions SET result_url=?, latest_json=?, result_hash=?, last_checked=?
                       WHERE telegram_id=? AND candidate_id=?""",
                    (result_url, result.to_json(), result.fingerprint, now_iso(), uid, cid),
                )
            else:
                self.conn.execute(
                    "UPDATE subscriptions SET result_url=? WHERE telegram_id=? AND candidate_id=?",
                    (result_url, uid, cid),
                )
            self.conn.commit()

    async def add_subscription(self, uid: int, username: str, full_name: str, cid: str, result: Result | None = None):
        async with self.lock:
            self.conn.execute(
                """
                INSERT INTO subscriptions
                (telegram_id, username, full_name, candidate_id, status, latest_json, result_hash, result_url, last_checked, created_at)
                VALUES (?, ?, ?, ?, 'waiting', ?, ?, ?, ?, ?)
                ON CONFLICT(telegram_id, candidate_id) DO UPDATE SET
                    username=excluded.username,
                    full_name=excluded.full_name,
                    status='waiting',
                    latest_json=excluded.latest_json,
                    result_hash=excluded.result_hash,
                    result_url=excluded.result_url,
                    last_checked=excluded.last_checked,
                    notified_at=NULL
                """,
                (uid, username, full_name, cid, result.to_json() if result else None, result.fingerprint if result else None, result.result_url if result else None, now_iso(), now_iso()),
            )
            self.conn.commit()

    async def get(self, uid: int, cid: str):
        async with self.lock:
            return self.conn.execute(
                "SELECT * FROM subscriptions WHERE telegram_id=? AND candidate_id=?",
                (uid, cid),
            ).fetchone()

    async def list_user(self, uid: int):
        async with self.lock:
            return self.conn.execute(
                "SELECT * FROM subscriptions WHERE telegram_id=? ORDER BY id DESC", (uid,)
            ).fetchall()

    async def waiting(self, limit: int):
        async with self.lock:
            return self.conn.execute(
                "SELECT * FROM subscriptions WHERE status='waiting' ORDER BY id ASC LIMIT ?", (limit,)
            ).fetchall()

    async def save_latest(self, row_id: int, result: Result):
        async with self.lock:
            self.conn.execute(
                """
                UPDATE subscriptions
                SET latest_json=?, result_hash=?, result_url=?, last_checked=?
                WHERE id=?
                """,
                (result.to_json(), result.fingerprint, result.result_url, now_iso(), row_id),
            )
            self.conn.commit()

    async def update_result_url(self, row_id: int, result_url: str, result: Result | None = None):
        async with self.lock:
            if result is not None:
                self.conn.execute(
                    """UPDATE subscriptions
                       SET result_url=?, latest_json=?, result_hash=?, last_checked=?
                       WHERE id=?""",
                    (result_url, result.to_json(), result.fingerprint, now_iso(), row_id),
                )
            else:
                self.conn.execute(
                    "UPDATE subscriptions SET result_url=?, last_checked=? WHERE id=?",
                    (result_url, now_iso(), row_id),
                )
            self.conn.commit()

    async def mark_checked(self, row_id: int):
        async with self.lock:
            self.conn.execute("UPDATE subscriptions SET last_checked=? WHERE id=?", (now_iso(), row_id))
            self.conn.commit()

    async def mark_notified(self, row_id: int, result: Result):
        async with self.lock:
            self.conn.execute(
                """
                UPDATE subscriptions
                SET status='notified', latest_json=?, result_hash=?, result_url=?, notified_at=?, last_checked=?
                WHERE id=?
                """,
                (result.to_json(), result.fingerprint, result.result_url, now_iso(), now_iso(), row_id),
            )
            self.conn.commit()

    async def cancel(self, uid: int, cid: str) -> bool:
        async with self.lock:
            cur = self.conn.execute(
                "DELETE FROM subscriptions WHERE telegram_id=? AND candidate_id=?", (uid, cid)
            )
            self.conn.commit()
            return cur.rowcount > 0

    async def stats(self):
        async with self.lock:
            total = self.conn.execute("SELECT COUNT(*) c FROM subscriptions").fetchone()["c"]
            waiting = self.conn.execute("SELECT COUNT(*) c FROM subscriptions WHERE status='waiting'").fetchone()["c"]
            notified = self.conn.execute("SELECT COUNT(*) c FROM subscriptions WHERE status='notified'").fetchone()["c"]
            users = self.conn.execute("SELECT COUNT(DISTINCT telegram_id) c FROM subscriptions").fetchone()["c"]
            known = self.conn.execute("SELECT COUNT(*) c FROM subscriptions WHERE latest_json IS NOT NULL").fetchone()["c"]
            return total, waiting, notified, users, known

    async def user_ids(self):
        async with self.lock:
            return [r[0] for r in self.conn.execute("SELECT DISTINCT telegram_id FROM subscriptions").fetchall()]


db = DB(DB_PATH)


# ============================================================
# OFFICIAL SITE CLIENT
# ============================================================
class MandatClient:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/151.0 Safari/537.36"
            ),
            "Accept-Language": "uz-UZ,uz;q=0.9,en;q=0.8",
        })

    def _get(self, url: str, **kwargs):
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                return self.session.get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True, **kwargs)
            except requests.RequestException:
                if attempt == MAX_RETRIES:
                    raise
                time.sleep(1.2 * attempt)
        raise RuntimeError("request failed")

    @staticmethod
    def _extract_hash_urls(resp_text: str) -> list[str]:
        soup = BeautifulSoup(resp_text, "html.parser")
        urls = []
        # JSON/AJAX responses may carry hashId or details URL without an <a>.
        for m in re.finditer(r"(?:\"|\')hashId(?:\"|\')\s*[:=]\s*(?:\"|\')([a-f0-9]{32,128})(?:\"|\')", resp_text, re.I):
            urls.append(f"{BASE_URL}/Bakalavr/Details?hashId={m.group(1)}")
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "Bakalavr/Details?hashId=" in href:
                urls.append(urljoin(BASE_URL, href))
        for m in re.finditer(r"Bakalavr/Details\?hashId=([a-f0-9]{32,128})", resp_text, re.I):
            urls.append(f"{BASE_URL}/Bakalavr/Details?hashId={m.group(1)}")
        # preserve order, remove duplicates
        out = []
        seen = set()
        for u in urls:
            if u not in seen:
                seen.add(u); out.append(u)
        return out

    @classmethod
    def _extract_hash_url(cls, resp_text: str) -> Optional[str]:
        urls = cls._extract_hash_urls(resp_text)
        return urls[0] if urls else None

    @staticmethod
    def _page_candidate_id(content: str) -> Optional[str]:
        text = clean(BeautifulSoup(content, "html.parser").get_text(" ", strip=True))
        m = re.search(r"Abituriyent\s+ID\s+raqami\s*[:#-]?\s*(\d{7})", text, re.I)
        if m:
            return m.group(1)
        m = re.search(r"ID\s*raqami\s*[:#-]?\s*(\d{7})", text, re.I)
        return m.group(1) if m else None

    def _submit_candidate_form(self, home, soup, cid: str) -> Optional[str]:
        for form in soup.find_all("form"):
            inputs = form.find_all("input")
            if not inputs:
                continue
            data = {}
            id_input = None
            for inp in inputs:
                name = inp.get("name") or inp.get("id")
                if not name:
                    continue
                typ = (inp.get("type") or "text").lower()
                if typ in {"submit", "button", "image", "file"}:
                    continue
                data[name] = inp.get("value", "")
                blob = " ".join([str(name), str(inp.get("id") or ""), str(inp.get("placeholder") or ""), str(inp.get("aria-label") or "")]).lower()
                if any(k in blob for k in ("abituriyent id", "id raqam", "candidate id", "candidateid", "abituriyentid", "search id")):
                    id_input = name
            if not id_input:
                for inp in inputs:
                    typ = (inp.get("type") or "text").lower()
                    blob = " ".join([str(inp.get("placeholder") or ""), str(inp.get("aria-label") or "")]).lower()
                    if typ in {"text", "search", "number", "tel"} and ("id" in blob or "raqam" in blob or "abitur" in blob):
                        id_input = inp.get("name") or inp.get("id")
                        break
            if not id_input:
                continue
            data[id_input] = cid
            action = urljoin(home.url, form.get("action") or home.url)
            method = (form.get("method") or "get").lower()
            headers = {"Referer": home.url, "Origin": BASE_URL, "X-Requested-With": "XMLHttpRequest", "Accept": "text/html,application/xhtml+xml,application/json,text/plain,*/*"}
            try:
                rr = self.session.post(action, data=data, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True) if method == "post" else self.session.get(action, params=data, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                log.info("ID form %s %s -> %s (%s bytes)", method.upper(), action, rr.status_code, len(rr.content))
                for found in self._extract_hash_urls(rr.text):
                    if self._verify_candidate_url(found, cid):
                        return found
                if self._looks_like_result(rr.text, cid) and self._page_candidate_id(rr.text) == cid:
                    return rr.url
            except requests.RequestException as exc:
                log.warning("ID form request failed: %s", exc)
        return None

    def _discover_js_endpoints(self, home, soup, cid: str) -> Optional[str]:
        endpoints = set()
        scripts = "\n".join(s.get_text(" ", strip=True) for s in soup.find_all("script"))
        html_text = str(soup)
        path_re = re.compile(r"[\"'`]((?:https?://[^\"'`\s]+)?/(?:Bakalavr|Home|Search|Result|Results|Candidate|Abituriyent)[A-Za-z0-9_./?=&-]*)[\"'`]", re.I)
        for m in path_re.finditer(scripts + "\n" + html_text):
            endpoints.add(urljoin(BASE_URL, m.group(1)))
        for tag in soup.find_all(True):
            for attr in ("data-url", "data-action", "action", "href"):
                value = tag.get(attr)
                if value and any(k in value.lower() for k in ("search", "result", "candidate", "abituriyent", "bakalavr")) and "details?hashid=" not in value.lower():
                    endpoints.add(urljoin(home.url, value))
        params_sets = [{"id": cid}, {"candidateId": cid}, {"candidateID": cid}, {"abituriyentId": cid}, {"abituriyentID": cid}, {"abituriyent": cid}, {"search": cid}, {"query": cid}, {"q": cid}]
        for endpoint in endpoints:
            for method in ("get", "post"):
                for params in params_sets:
                    try:
                        headers = {"Referer": home.url, "Origin": BASE_URL, "X-Requested-With": "XMLHttpRequest", "Accept": "application/json,text/plain,text/html,*/*"}
                        rr = self.session.post(endpoint, data=params, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True) if method == "post" else self.session.get(endpoint, params=params, headers=headers, timeout=REQUEST_TIMEOUT, allow_redirects=True)
                        for found in self._extract_hash_urls(rr.text):
                            if self._verify_candidate_url(found, cid):
                                log.info("Verified candidate endpoint: %s %s -> %s", method.upper(), rr.url, found)
                                return found
                        if self._looks_like_result(rr.text, cid) and self._page_candidate_id(rr.text) == cid:
                            return rr.url
                    except requests.RequestException:
                        continue
        return None

    def _verify_candidate_url(self, url: str, cid: str) -> bool:
        try:
            page = self._get(url, headers={"Referer": f"{BASE_URL}/Bakalavr/MainSearch"})
            actual = self._page_candidate_id(page.text)
            if actual != cid:
                log.warning("Rejected mismatched result: requested=%s actual=%s url=%s", cid, actual, page.url)
                return False
            return True
        except requests.RequestException as exc:
            log.warning("Candidate verification request failed: %s", exc)
            return False

    def _browser_find_candidate_url(self, cid: str) -> Optional[str]:
        """Find the exact candidate by submitting the visible ID search UI.

        The Mandat page is a JS-driven search screen. We deliberately avoid any
        Details links present in the initial HTML. Only data produced after the
        user's exact ID is submitted is considered.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except Exception:
            log.warning("Playwright is not installed; browser lookup unavailable")
            return None

        try:
            with sync_playwright() as p:
                browser = None
                for kwargs in (
                    {"headless": True, "channel": "chrome"},
                    {"headless": True},
                ):
                    try:
                        browser = p.chromium.launch(**kwargs)
                        break
                    except Exception as exc:
                        log.warning("Browser launch failed: %s", exc)
                if browser is None:
                    return None

                context = browser.new_context(
                    locale="uz-UZ",
                    user_agent=self.session.headers.get("User-Agent"),
                    viewport={"width": 1440, "height": 1000},
                )
                page = context.new_page()
                captured = []

                def on_response(resp):
                    try:
                        # Capture all XHR/fetch/document responses after the submit;
                        # do not rely on endpoint names because the site can rename them.
                        rt = (resp.request.resource_type or "").lower()
                        if rt in {"xhr", "fetch", "document"}:
                            captured.append(resp)
                    except Exception:
                        pass

                page.on("response", on_response)
                page.goto(f"{BASE_URL}/Bakalavr/MainSearch", wait_until="commit", timeout=20000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=12000)
                except PlaywrightTimeoutError:
                    pass
                page.wait_for_timeout(1200)

                # The page exposes an ID search tab. Click it when present.
                for sel in (
                    'text=ID raqami bo\'yicha qidiruv',
                    'text=ID raqami bo‘yicha qidiruv',
                    'button:has-text("ID raqami bo\'yicha")',
                    'a:has-text("ID raqami bo\'yicha")',
                ):
                    try:
                        loc = page.locator(sel).first
                        if loc.count() and loc.is_visible():
                            loc.click()
                            page.wait_for_timeout(700)
                            break
                    except Exception:
                        pass

                # Exact input first: the official page labels it “ID raqamni kiriting”.
                target = None
                candidates = [
                    'input[placeholder*="ID raqam"]',
                    'input[aria-label*="ID"]',
                    'input[name*="id" i]',
                    'input[id*="id" i]',
                    'input[type="text"]',
                    'input[type="search"]',
                    'input[type="number"]',
                    'input[type="tel"]',
                ]
                for sel in candidates:
                    locs = page.locator(sel)
                    for i in range(locs.count()):
                        loc = locs.nth(i)
                        try:
                            if loc.is_visible() and loc.is_enabled():
                                placeholder = clean(loc.get_attribute("placeholder"))
                                aria = clean(loc.get_attribute("aria-label"))
                                meta = f"{placeholder} {aria} {loc.get_attribute('name') or ''} {loc.get_attribute('id') or ''}".lower()
                                if "id raqam" in meta or "abitur" in meta or sel.startswith('input[placeholder'):
                                    target = loc
                                    break
                        except Exception:
                            continue
                    if target is not None:
                        break

                if target is None:
                    log.error("Browser: official ID input not found")
                    browser.close()
                    return None

                awaitable_fill = target.fill(cid)
                del awaitable_fill  # sync API returns None; kept explicit for clarity

                # Collect the visible Qidirish buttons in document order; on the official
                # page the ID-search button is the first Qidirish control.
                qbuttons = page.locator('button:has-text("Qidirish"), input[type="submit"]')
                visible_buttons = []
                for i in range(qbuttons.count()):
                    try:
                        b = qbuttons.nth(i)
                        if b.is_visible() and b.is_enabled():
                            visible_buttons.append(b)
                    except Exception:
                        pass

                submitted = False
                if visible_buttons:
                    try:
                        visible_buttons[0].click()
                        submitted = True
                    except Exception as exc:
                        log.warning("Browser: first Qidirish click failed: %s", exc)

                if not submitted:
                    try:
                        target.press("Enter")
                        submitted = True
                    except Exception:
                        pass

                if not submitted:
                    log.error("Browser: ID search could not be submitted")
                    browser.close()
                    return None

                # Give the JS search enough time; don't require networkidle because
                # analytics/long-polling can keep it open indefinitely.
                page.wait_for_timeout(2000)
                for _ in range(12):
                    body = clean(page.locator("body").inner_text())
                    if cid in body:
                        log.info("Browser: target ID appeared in DOM after submit")
                        break
                    page.wait_for_timeout(500)

                body = clean(page.locator("body").inner_text())
                log.info("Browser search after submit: url=%s target_in_body=%s bytes=%d", page.url, cid in body, len(body))

                # Prefer a Details link that was created/updated by this search.
                detail_links = page.locator('a[href*="/Bakalavr/Details?hashId="]')
                for i in range(detail_links.count()):
                    try:
                        href = detail_links.nth(i).get_attribute("href")
                        if not href:
                            continue
                        full = urljoin(BASE_URL, href)
                        # Only accept it when the current DOM already contains the target ID.
                        if cid not in body:
                            continue
                        if self._verify_candidate_url(full, cid):
                            log.info("Browser verified exact candidate URL: %s", full)
                            browser.close()
                            return full
                    except Exception:
                        continue

                # Inspect every captured response AFTER submission. This handles JSON
                # endpoints where the page itself is not navigated.
                for resp in reversed(captured):
                    try:
                        txt = resp.text()
                    except Exception:
                        continue
                    if not txt:
                        continue
                    if cid not in txt and cid not in body:
                        continue
                    for found in self._extract_hash_urls(txt):
                        if self._verify_candidate_url(found, cid):
                            log.info("Browser verified candidate from XHR/fetch: %s", found)
                            browser.close()
                            return found

                # Last resort: if the page URL itself changed to a Details URL, verify it.
                if "/Bakalavr/Details?hashId=" in page.url and self._verify_candidate_url(page.url, cid):
                    browser.close()
                    return page.url

                # Save a diagnostic snapshot for the local developer.
                try:
                    Path("mandat_debug_last.html").write_text(page.content(), encoding="utf-8")
                except Exception:
                    pass
                log.warning("Browser search produced no verified result for ID=%s; url=%s target_in_body=%s", cid, page.url, cid in body)
                browser.close()
                return None
        except Exception as exc:
            log.error("Browser candidate lookup failed: %s", exc, exc_info=True)
            return None


    @staticmethod
    def _extract_bachelor_list_page(html_text: str, base_url: str) -> dict[str, Any]:
        """Parse the real public /Bakalavr 10-row list and its pagination controls."""
        soup = BeautifulSoup(html_text, "html.parser")
        rows = []
        seen = set()

        # Prefer links that are actual candidate Details links.
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            if "/Bakalavr/Details?hashId=" not in href:
                continue
            full = urljoin(base_url, href)
            container = a.find_parent("tr")
            if container is None:
                # Fall back to the nearest compact card/list item.
                container = a.find_parent(["li", "article"])
            if container is None:
                container = a.parent
            txt = clean(container.get_text(" ", strip=True))
            m = re.search(r"(?<!\d)(\d{7})(?!\d)", txt)
            cid = m.group(1) if m else None
            score = None
            sm = re.search(r"(?:TO['’‘`]?PLANGAN\s+BALL|TO['’‘`]?PLANGAN|UMUMIY\s+BALL|BALL)\s*[:\-]?\s*(\d+(?:[.,]\d+)?)", txt, re.I)
            if sm:
                try:
                    score = float(sm.group(1).replace(",", "."))
                except Exception:
                    pass
            key = (cid, full)
            if key not in seen:
                seen.add(key)
                rows.append({"candidate_id": cid, "href": full, "text": txt, "score": score})

        # Pagination: the real page uses visible "1 2 Keyingi" controls.
        pagination = []
        for el in soup.find_all(["a", "button", "li", "span"]):
            txt = clean(el.get_text(" ", strip=True))
            if not txt:
                continue
            href = el.get("href") or ""
            data_blob = " ".join(
                str(el.get(k, "")) for k in
                ("data-url", "data-href", "data-page", "data-page-number",
                 "data-pageindex", "data-page-number", "value", "onclick",
                 "aria-label", "class", "id")
            )
            low = (txt + " " + href + " " + data_blob).lower()
            if (re.fullmatch(r"\d{1,6}", txt)
                or "keyingi" in low or "next" in low
                or "sahifa" in low or "page" in low or "paginate" in low):
                pagination.append({
                    "tag": el.name,
                    "text": txt,
                    "href": urljoin(base_url, href) if href else "",
                    "data": data_blob[:1000],
                    "html": str(el)[:2500],
                })

        # De-duplicate pagination entries.
        dedup = []
        seenp = set()
        for p in pagination:
            key = (p["text"], p["href"], p["data"])
            if key not in seenp:
                seenp.add(key)
                dedup.append(p)

        # Determine active/current page.
        current_page = None
        for el in soup.find_all(True):
            cls = str(el.get("class", "")).lower()
            aria = str(el.get("aria-current", "")).lower()
            txt = clean(el.get_text(" ", strip=True))
            if ("active" in cls or aria == "page") and re.fullmatch(r"\d{1,6}", txt):
                current_page = int(txt)
                break

        # URL/query hint.
        q = dict(re.findall(r"([A-Za-z]+)=([^&]+)", base_url.split("?",1)[1])) if "?" in base_url else {}
        for k in ("page", "pageNumber", "pageIndex", "currentPage"):
            if k in q and str(q[k]).isdigit():
                current_page = int(q[k])
                break

        return {"rows": rows, "pagination": dedup, "current_page": current_page}


    def _direct_bachelor_list_lookup(self, cid: str) -> Optional[dict[str, Any]]:
        """Official list route discovered from the user's browser capture:
        /Bakalavr?entrantid=<ID>&lang=uz
        """
        url = f"{BASE_URL}/Bakalavr"
        try:
            r = self._get(url, params={"entrantid": cid, "lang": "uz"},
                          headers={"Referer": f"{BASE_URL}/Bakalavr/MainSearch"})
            parsed = self._extract_bachelor_list_page(r.text, r.url)
            for row in parsed["rows"]:
                if row.get("candidate_id") == cid and row.get("href"):
                    return {"url": row["href"], "list_url": r.url, **parsed}
            log.info("Direct bachelor list lookup: target %s not on returned page %s rows=%s",
                     cid, r.url, len(parsed["rows"]))
        except Exception as exc:
            log.warning("Direct bachelor list lookup failed for %s: %s", cid, exc)
        return None


    def _parse_rank_rows(self, html_text: str, base_url: str):
        parsed = self._extract_bachelor_list_page(html_text, base_url)
        rows = parsed.get("rows", [])
        # Keep only rows that have a candidate ID; DOM order is the ranking order.
        rows = [r for r in rows if r.get("candidate_id")]
        return parsed, rows

    def _global_page_request(self, page_no: int, param_name: str):
        """Request the public /Bakalavr ranking list at a specific page.
        The exact paging parameter is discovered by comparing page 1 and page 2.
        """
        try:
            spec = getattr(self, "_rank_page_spec", None)
            if spec and spec.get("kind") == "query":
                params = dict(spec.get("fixed") or {})
                params[spec["param"]] = str(page_no)
                base = spec.get("base") or f"{BASE_URL}/Bakalavr"
            else:
                params = {param_name: str(page_no), "lang": "uz"}
                base = f"{BASE_URL}/Bakalavr"
            r = self._get(
                base, params=params,
                headers={"Referer": f"{BASE_URL}/Bakalavr/MainSearch"},
            )
            if r.status_code >= 400 or len(r.content) < 200:
                return None
            return r
        except Exception:
            return None

    def _browser_discover_global_page_request(self):
        """Use the real site UI once to discover the actual page-2 request.
        This avoids guessing page/pageNumber/pageIndex. Returns a dict with
        method/url/post_data from the request that loaded page 2, or a page-2
        document URL if navigation was direct.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except Exception:
            log.warning("Ranking discovery: Playwright not installed")
            return None
        try:
            with sync_playwright() as p:
                browser = None
                for kwargs in ({"headless": True, "channel": "chrome"}, {"headless": True}):
                    try:
                        browser = p.chromium.launch(**kwargs)
                        break
                    except Exception:
                        continue
                if not browser:
                    return None
                context = browser.new_context(locale="uz-UZ", user_agent=self.session.headers.get("User-Agent"))
                page = context.new_page()
                page.set_default_timeout(7000)
                captured = []
                def on_request(req):
                    try:
                        if req.resource_type in {"document", "xhr", "fetch"}:
                            captured.append({
                                "method": req.method,
                                "url": req.url,
                                "post_data": req.post_data,
                                "resource_type": req.resource_type,
                            })
                    except Exception:
                        pass
                page.on("request", on_request)
                page.goto(f"{BASE_URL}/Bakalavr", wait_until="commit", timeout=20000)
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                page.wait_for_timeout(700)
                before_sig = clean(page.locator("body").inner_text())[:5000]
                # Find a real numeric page 2 control first.
                candidates = [
                    'a:text-is("2")', 'button:text-is("2")', '[role="button"]:text-is("2")',
                    'a:has-text("2")', 'button:has-text("2")'
                ]
                clicked = False
                for sel in candidates:
                    loc = page.locator(sel)
                    for i in range(loc.count()):
                        el = loc.nth(i)
                        try:
                            if not el.is_visible() or not el.is_enabled():
                                continue
                            txt = clean(el.inner_text())
                            if txt != "2":
                                continue
                            el.click()
                            clicked = True
                            break
                        except Exception:
                            continue
                    if clicked:
                        break
                if not clicked:
                    # Fallback: click a visible "Keyingi" control.
                    for sel in ['text=Keyingi', 'text=Next', 'a:has-text("Keyingi")', 'button:has-text("Keyingi")']:
                        loc = page.locator(sel).first
                        try:
                            if loc.count() and loc.is_visible() and loc.is_enabled():
                                loc.click(); clicked = True; break
                        except Exception:
                            continue
                if not clicked:
                    browser.close(); return None
                page.wait_for_timeout(1200)
                after_sig = clean(page.locator("body").inner_text())[:5000]
                new_reqs = []
                for r in captured:
                    u = r["url"]
                    if "/Bakalavr" in u and not u.endswith("/Bakalavr"):
                        new_reqs.append(r)
                # Prefer requests seen after the initial document load.
                if not new_reqs:
                    # de-duplicate and keep the last few likely requests
                    new_reqs = captured[-10:]
                result = {
                    "after_url": page.url,
                    "clicked": clicked,
                    "page_changed": before_sig != after_sig,
                    "requests": new_reqs[-10:],
                }
                browser.close()
                log.info("Ranking page-2 discovery: after_url=%s page_changed=%s requests=%s", page.url, before_sig != after_sig, len(new_reqs))
                return result
        except Exception as exc:
            log.warning("Ranking browser page-2 discovery failed: %s", exc)
            return None

    def _discover_bachelor_page_spec(self):
        """Return a page-request spec discovered from the live pagination UI."""
        disc = self._browser_discover_global_page_request()
        if not disc:
            return None
        after_url = disc.get("after_url") or ""
        # Direct document navigation is easiest: preserve all existing query params,
        # replacing only the page-like parameter when present.
        from urllib.parse import parse_qs, urlsplit, urlunsplit, urlencode
        parts = urlsplit(after_url)
        q = parse_qs(parts.query, keep_blank_values=True)
        for key in ("page", "pageNumber", "pageIndex", "currentPage"):
            if key in q:
                return {"kind":"query", "param":key, "base":f"{BASE_URL}{parts.path}", "fixed":{k:v[-1] for k,v in q.items() if k != key}}
        # Inspect captured request URLs similarly.
        for req in reversed(disc.get("requests", [])):
            u = req.get("url") or ""
            if "/Bakalavr" not in u:
                continue
            parts = urlsplit(u); q=parse_qs(parts.query, keep_blank_values=True)
            for key in ("page", "pageNumber", "pageIndex", "currentPage"):
                if key in q:
                    fixed={k:v[-1] for k,v in q.items() if k != key}
                    return {"kind":"query", "param":key, "base":f"{BASE_URL}{parts.path}", "fixed":fixed}
            # Some sites use route segments /Bakalavr/2.
            m=re.search(r"/Bakalavr/(\d+)(?:\?|$)", u)
            if m:
                return {"kind":"path", "base_prefix":u[:m.start(1)], "suffix":u[m.end(1):], "first_page":int(m.group(1))}
        return None

    def _detect_bachelor_page_param(self):
        # Cache the live pagination specification so every ranking request does not
        # launch Chromium again. This is the main speed optimization.
        cached = getattr(self, "_rank_page_spec_cache", None)
        cached_at = getattr(self, "_rank_page_spec_cache_at", 0.0)
        if cached and (time.time() - cached_at) < RANK_SPEC_CACHE_TTL:
            self._rank_page_spec = cached
            return cached.get("param") if cached.get("kind") == "query" else None
        spec = self._discover_bachelor_page_spec()
        if spec and spec.get("kind") == "query":
            self._rank_page_spec = spec
            self._rank_page_spec_cache = spec
            self._rank_page_spec_cache_at = time.time()
            log.info("Using discovered live pagination spec: %s", spec)
            return spec["param"]
        self._rank_page_spec = None
        # Fallback to parameter probing only if the browser discovery cannot see it.
        base = None
        try:
            base = self._get(f"{BASE_URL}/Bakalavr", params={"lang":"uz"}, headers={"Referer":f"{BASE_URL}/Bakalavr/MainSearch"})
        except Exception:
            return None
        if not base:
            return None
        _, rows1 = self._parse_rank_rows(base.text, base.url)
        sig1 = [(x.get("candidate_id"), x.get("score")) for x in rows1[:10]]
        for name in ("page", "pageNumber", "pageIndex", "currentPage"):
            r = self._global_page_request(2, name)
            if not r:
                continue
            _, rows2 = self._parse_rank_rows(r.text, r.url)
            sig2 = [(x.get("candidate_id"), x.get("score")) for x in rows2[:10]]
            if sig2 and sig2 != sig1:
                self._rank_page_spec = {"kind":"query", "param":name, "base":f"{BASE_URL}/Bakalavr", "fixed":{"lang":"uz"}}
                log.info("Detected /Bakalavr pagination parameter by probing: %s", name)
                return name
        return None

    def _rank_from_global_bachelor_pages(self, result: Result) -> Optional[RankInfo]:
        """Find the exact global ranking position using the site's 10-row /Bakalavr pages.

        We first discover the real paging parameter, then use score-ordered pages to
        locate the target quickly. The candidate ID itself must be present before any
        rank is returned.
        """
        target_id = str(result.candidate_id)
        target_score = self._score_num(result.total_score)
        param = self._detect_bachelor_page_param()
        if not param:
            log.warning("Could not detect a working /Bakalavr page parameter")
            return None

        page_cache = {}
        def get_page(n: int):
            if n in page_cache:
                return page_cache[n]
            r = self._global_page_request(n, param)
            if not r:
                page_cache[n] = None
                return None
            parsed, rows = self._parse_rank_rows(r.text, r.url)
            page_cache[n] = (r, parsed, rows)
            return page_cache[n]

        def find_on_page(n: int):
            data = get_page(n)
            if not data:
                return None
            _, parsed, rows = data
            for idx, row in enumerate(rows, 1):
                if row.get("candidate_id") == target_id:
                    return idx, parsed
            return None

        # Exact hit on first page.
        hit = find_on_page(1)
        if hit:
            idx, parsed = hit
            return RankInfo(rank=idx, total=None, scope="overall", source_url=get_page(1)[0].url)

        # Discover an upper page bound by exponentially increasing page number
        # until the page's score range reaches or falls below the target score.
        # If score is unavailable, fall back to a bounded sequential scan.
        if target_score is None:
            for n in range(2, 301):
                hit = find_on_page(n)
                if hit:
                    idx, _ = hit
                    return RankInfo(rank=(n - 1) * 10 + idx, total=None, scope="overall", source_url=get_page(n)[0].url)
            return None

        def score_range(n: int):
            data = get_page(n)
            if not data:
                return None
            rows = data[2]
            scores = [self._score_num(str(x.get("score"))) for x in rows]
            scores = [x for x in scores if x is not None]
            return (max(scores), min(scores)) if scores else None

        # Probe exponentially until the bottom of a page is <= target score.
        lo, hi = 1, 1
        found_bound = False
        for n in (2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384):
            rng = score_range(n)
            if not rng:
                continue
            hi_score, lo_score = rng
            if target_score >= lo_score:
                # Target is at or above this page's low score; it lies on/before this page.
                hi = n
                found_bound = True
                break
            lo = n
        if not found_bound:
            # Could not establish a score bound; do a bounded forward scan.
            for n in range(max(2, lo + 1), lo + 201):
                hit = find_on_page(n)
                if hit:
                    idx, _ = hit
                    return RankInfo(rank=(n - 1) * 10 + idx, total=None, scope="overall", source_url=get_page(n)[0].url)
            return None

        # Binary search for the first page whose score interval reaches target.
        left = max(1, lo)
        right = hi
        while left < right:
            mid = (left + right) // 2
            rng = score_range(mid)
            if not rng:
                break
            hi_score, lo_score = rng
            if target_score > hi_score:
                right = mid - 1
            elif target_score < lo_score:
                left = mid + 1
            else:
                # We have reached the score band. Search a small neighborhood because
                # tied scores may span many pages.
                left = max(1, mid - 3)
                right = min(hi, mid + 3)
                break

        for n in range(max(1, left - 2), min(hi, right + 4) + 1):
            hit = find_on_page(n)
            if hit:
                idx, _ = hit
                return RankInfo(rank=(n - 1) * 10 + idx, total=None, scope="overall", source_url=get_page(n)[0].url)

        # If the score ties across a wider band, walk outward a little more.
        center = max(1, (left + right) // 2)
        for n in range(max(1, center - 15), center + 16):
            hit = find_on_page(n)
            if hit:
                idx, _ = hit
                return RankInfo(rank=(n - 1) * 10 + idx, total=None, scope="overall", source_url=get_page(n)[0].url)
        return None

    def _rank_from_direct_bachelor_list(self, cid: str, scope: str = "overall") -> Optional[RankInfo]:
        # The entrantid query is a candidate lookup page, not by itself proof of global
        # rank. Only use it to validate that the candidate exists; global rank is found
        # from the unfiltered /Bakalavr pagination.
        if scope != "overall":
            return None
        try:
            r = self._get(
                f"{BASE_URL}/Bakalavr",
                params={"entrantid": cid, "lang": "uz"},
                headers={"Referer": f"{BASE_URL}/Bakalavr/MainSearch"},
            )
            parsed = self._extract_bachelor_list_page(r.text, r.url)
            if any(x.get("candidate_id") == cid for x in parsed.get("rows", [])):
                return self._rank_from_global_bachelor_pages(
                    Result(candidate_id=cid, total_score=None)
                )
        except Exception as exc:
            log.warning("Direct bachelor rank validation failed %s: %s", cid, exc)
        return None

    def find_candidate_url(self, cid: str) -> Optional[str]:
        """Find the exact candidate from the official /Bakalavr list route first.
        This avoids the historical bug where unrelated Details links from MainSearch
        were mistaken for the requested candidate.
        """
        direct = self._direct_bachelor_list_lookup(cid)
        if direct and direct.get("url") and self._verify_candidate_url(direct["url"], cid):
            return direct["url"]

        # Browser form submission is a fallback, not the primary route.
        found = self._browser_find_candidate_url(cid)
        if found:
            return found

        try:
            home = self._get(HOME_URL)
            if home.status_code >= 400:
                return None
            main = self._get(f"{BASE_URL}/Bakalavr/MainSearch", headers={"Referer": home.url})
            soup = BeautifulSoup(main.text, "html.parser")
            found = self._submit_candidate_form(main, soup, cid)
            if found and self._verify_candidate_url(found, cid):
                return found
        except requests.RequestException as exc:
            log.error("Official Mandat request failed: %s", exc)
        except Exception:
            log.exception("Strict candidate lookup failed")
        return None


    @staticmethod
    def _looks_like_result(content: str, cid: str) -> bool:
        text = clean(BeautifulSoup(content, "html.parser").get_text(" ", strip=True))
        low = text.lower()
        return cid in text and ("umumiy ball" in low or "to'g'ri javob" in low or "to‘g‘ri javob" in low)

    @staticmethod
    def _rank_from_text(raw: str, scope: str) -> Optional[RankInfo]:
        patterns = [
            rf"{scope}.{{0,100}}?(\d+)\s*(?:-?o['’‘]?rin|o['’‘]?rni)",
            rf"(\d+)\s*(?:-?o['’‘]?rin|o['’‘]?rni).{{0,100}}?{scope}",
        ]
        rank = first(patterns, raw)
        if rank:
            total = first([
                rf"{scope}.{{0,150}}?(?:ichida|jami|nafar).{{0,40}}?(\d[\d\s]*)",
                rf"(\d[\d\s]*)\s*(?:nafar talabgor|talabgor).{{0,100}}?{scope}",
            ], raw)
            return RankInfo(rank=int(rank), total=int(re.sub(r"\s+", "", total)) if total else None, scope=scope)
        return None


    @staticmethod
    def _score_num(value: Optional[str]) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(str(value).replace(" ", "").replace(",", "."))
        except Exception:
            return None

    @staticmethod
    def _pagination_urls(base_page: str, soup: BeautifulSoup) -> list[str]:
        """Discover pagination-like URLs from links/forms/scripts on the public page."""
        urls = []
        for tag in soup.find_all(True):
            for attr in ("href", "data-url", "data-action", "action"):
                value = tag.get(attr)
                if not value:
                    continue
                low = str(value).lower()
                if any(k in low for k in ("paginate", "page=", "pagenumber", "pageindex", "currentpage")):
                    urls.append(urljoin(base_page, str(value)))
        scripts = "\n".join(s.get_text(" ", strip=True) for s in soup.find_all("script"))
        for m in re.finditer(r"[\"'`]((?:https?://[^\"'`\\s]+)?/[^\"'`\\s]*(?:paginate|page)[^\"'`\\s]*)[\"'`]", scripts, re.I):
            urls.append(urljoin(base_page, m.group(1)))
        # Common ASP.NET-style endpoint seen on UZBMB ranking pages.
        urls.extend([
            urljoin(BASE_URL, "/Bakalavr/Paginate"),
            urljoin(BASE_URL, "/Bakalavr/Results/Paginate"),
            urljoin(BASE_URL, "/Bakalavr/Ranking/Paginate"),
            urljoin(BASE_URL, "/Bakalavr/Rank/Paginate"),
        ])
        out, seen = [], set()
        for u in urls:
            u = u.split("#", 1)[0]
            if u not in seen:
                seen.add(u); out.append(u)
        return out

    @staticmethod
    def _extract_total_pages(text: str) -> Optional[int]:
        patterns = [
            r"Sahifa\s*\d+\s*/\s*(\d+)",
            r"Page\s*\d+\s*/\s*(\d+)",
            r"Jami\s*:\s*\d+.*?Sahifa\s*\d+\s*/\s*(\d+)",
            r"totalPages?\s*[:=]\s*(\d+)",
            r"TotalPages\s*[:=]\s*(\d+)",
        ]
        for ptn in patterns:
            m = re.search(ptn, text, re.I | re.S)
            if m:
                try:
                    n = int(m.group(1))
                    if 1 <= n <= 200000:
                        return n
                except Exception:
                    pass
        return None

    @staticmethod
    def _extract_page_candidates(text: str, target_id: Optional[str] = None) -> tuple[list[str], list[float]]:
        soup = BeautifulSoup(text, "html.parser")
        clean_text = clean(soup.get_text(" ", strip=True))
        # Prefer explicit 7-digit IDs; de-duplicate while preserving order.
        ids = []
        for m in re.finditer(r"(?<!\d)(\d{7})(?!\d)", clean_text):
            cid = m.group(1)
            if cid not in ids:
                ids.append(cid)
        scores = []
        # Ranking pages normally repeat an overall score once per row.
        for m in re.finditer(r"(?:Umumiy\s+ball|To['’‘`]?plagan\s+ball|Test\s+bali)\s*[:\-]?\s*(\d+(?:[\.,]\d+)?)", clean_text, re.I):
            try:
                scores.append(float(m.group(1).replace(",", ".")))
            except Exception:
                pass
        return ids, scores

    def _page_request(self, endpoint: str, page: int, extra_params: Optional[dict[str, str]] = None):
        params_list = []
        extra_params = dict(extra_params or {})
        for key in ("page", "pageNumber", "pageIndex", "currentPage"):
            pp = dict(extra_params); pp[key] = str(page); params_list.append(pp)
        headers = {
            "Referer": f"{BASE_URL}/Bakalavr/MainSearch",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "text/html,application/json,text/plain,*/*",
        }
        for params in params_list:
            try:
                r = self.session.get(endpoint, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
                if r.status_code < 500 and len(r.content) > 100:
                    return r
            except requests.RequestException:
                continue
        return None


    def _browser_rank(self, result: Result, scope: str = "overall") -> Optional[RankInfo]:
        """Find the exact candidate position in the live 10-row UZBMB result list.

        We do NOT trust arbitrary 7-digit numbers in page text. Instead, we collect
        the visible Details links for the 10 rows, then verify candidate IDs by
        opening those Details pages. Pagination is driven by the actual visible
        numbered controls/buttons generated by the site.
        """
        try:
            from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
        except Exception:
            log.warning("Browser ranking unavailable: Playwright not installed")
            return None

        target_id = str(result.candidate_id)
        target_score = self._score_num(result.total_score)

        def parse_num(s: str) -> Optional[float]:
            try:
                return float(s.replace(" ", "").replace("\u00a0", "").replace(",", "."))
            except Exception:
                return None

        def row_text(locator) -> str:
            try:
                txt = clean(locator.inner_text())
                return txt
            except Exception:
                return ""

        def rank_total_from_body(body: str) -> tuple[Optional[int], Optional[int]]:
            m = re.search(r"Sahifa\s+(\d+)\s*/\s*(\d+)", body, re.I)
            if m:
                return int(m.group(1)), int(m.group(2))
            return None, None

        def detail_links(page):
            """Return rows in DOM order. Each row has href, text and best-effort score."""
            links = page.locator('a[href*="/Bakalavr/Details?hashId="]')
            out = []
            seen = set()
            for i in range(links.count()):
                try:
                    a = links.nth(i)
                    if not a.is_visible():
                        continue
                    href = a.get_attribute("href")
                    if not href:
                        continue
                    href = urljoin(page.url, href)
                    if href in seen:
                        continue
                    seen.add(href)
                    # Prefer table row, then nearest reasonably-sized container.
                    container = a.locator("xpath=ancestor::tr[1]")
                    if not container.count():
                        container = a.locator("xpath=ancestor::*[self::li or self::article or contains(@class,'card')][1]")
                    if not container.count():
                        container = a.locator("xpath=..")
                    txt = row_text(container.first if container.count() else a)
                    score = None
                    m = re.search(r"(?:Umumiy\s+ball|To['’‘`]?plagan\s+ball|Ball)\s*[:\-]?\s*(\d+(?:[\.,]\d+)?)", txt, re.I)
                    if m:
                        score = parse_num(m.group(1))
                    out.append((href, txt, score))
                except Exception:
                    continue
            return out

        def verify_detail(page, href: str) -> Optional[str]:
            try:
                page.goto(href, wait_until="commit", timeout=15000)
                try:
                    page.locator("body").wait_for(state="visible", timeout=5000)
                except Exception:
                    pass
                page.wait_for_timeout(250)
                txt = clean(page.locator("body").inner_text())
                m = re.search(r"Abituriyent\s+ID\s+raqami\s*[:\-]?\s*(\d{7})", txt, re.I)
                if m:
                    return m.group(1)
                # fallback: exact phrase nearby
                m = re.search(r"ID\s+raqami\s*[:\-]?\s*(\d{7})", txt, re.I)
                return m.group(1) if m else None
            except Exception:
                return None

        def open_search(page):
            page.goto(f"{BASE_URL}/Bakalavr/MainSearch", wait_until="commit", timeout=20000)
            try:
                page.locator('input').first.wait_for(state="visible", timeout=8000)
            except Exception:
                pass
            page.wait_for_timeout(500)

        def activate_scope(page):
            if scope != "direction":
                return
            for sel in ('text=Kengaytirilgan qidiruv', 'button:has-text("Kengaytirilgan")', 'a:has-text("Kengaytirilgan")'):
                try:
                    loc = page.locator(sel).first
                    if loc.count() and loc.is_visible():
                        loc.click(); page.wait_for_timeout(500); break
                except Exception:
                    pass
            # Try to align the available selects with candidate data; unlike before,
            # do not guess by option index. Only select exact/strong text matches.
            desired = []
            if result.specialist_subject_1: desired.append(clean(result.specialist_subject_1).lower())
            if result.specialist_subject_2: desired.append(clean(result.specialist_subject_2).lower())
            if result.education_language: desired.append(clean(result.education_language).lower())
            selects = page.locator('select')
            for i in range(selects.count()):
                try:
                    sel = selects.nth(i)
                    opts = sel.locator('option')
                    best = None
                    for j in range(opts.count()):
                        o = opts.nth(j)
                        txt = clean(o.inner_text()).lower()
                        val = o.get_attribute('value')
                        if not txt or not val:
                            continue
                        if any(d and (d == txt or d in txt or txt in d) for d in desired):
                            best = val; break
                    if best:
                        sel.select_option(best)
                        page.wait_for_timeout(100)
                except Exception:
                    continue

        def submit_search(page):
            # Only use the form/input associated with the extended search, never
            # arbitrary Details links present in MainSearch HTML.
            inputs = page.locator('input')
            candidate_inputs = []
            for i in range(inputs.count()):
                try:
                    inp = inputs.nth(i)
                    if not inp.is_visible() or not inp.is_editable():
                        continue
                    placeholder = (inp.get_attribute('placeholder') or '').lower()
                    name = (inp.get_attribute('name') or '').lower()
                    iid = (inp.get_attribute('id') or '').lower()
                    if any(k in (placeholder + ' ' + name + ' ' + iid) for k in ('id raqam', 'abituriyent', 'candidate', 'id')):
                        candidate_inputs.append(inp)
                except Exception:
                    pass
            # Direction ranking does not need candidate ID as search input; simply
            # submit the current extended search form. Overall ranking also benefits
            # from starting with the list page rather than ID lookup.
            if scope == "direction":
                for sel in ('button:has-text("Qidirish")', 'input[type="submit"]'):
                    try:
                        btn = page.locator(sel).last
                        if btn.count() and btn.is_visible() and btn.is_enabled():
                            btn.click(); return True
                    except Exception:
                        pass
                return False
            # Overall ranking: submit the extended search list without putting an ID
            # into the search field. The list order is what defines the ranking.
            for sel in ('button:has-text("Qidirish")', 'input[type="submit"]'):
                try:
                    btns = page.locator(sel)
                    for k in range(btns.count() - 1, -1, -1):
                        btn = btns.nth(k)
                        if btn.is_visible() and btn.is_enabled():
                            btn.click(); return True
                except Exception:
                    pass
            return False

        def extract_total_pages(page) -> tuple[Optional[int], Optional[int]]:
            body = clean(page.locator('body').inner_text())
            return rank_total_from_body(body)

        def click_page(page, n: int) -> bool:
            # Prefer exact numeric button/anchor, then links containing page param.
            patterns = [
                f'button:text-is("{n}")',
                f'a:text-is("{n}")',
                f'button:has-text("{n}")',
                f'a:has-text("{n}")',
            ]
            for sel in patterns:
                try:
                    loc = page.locator(sel)
                    for i in range(loc.count()):
                        el = loc.nth(i)
                        if not el.is_visible() or not el.is_enabled():
                            continue
                        before = page.url
                        el.click()
                        page.wait_for_timeout(350)
                        try:
                            page.wait_for_load_state("domcontentloaded", timeout=4000)
                        except Exception:
                            pass
                        return True
                except Exception:
                    continue
            # JS fallback: find pagination controls with text/aria matching n.
            try:
                ok = page.evaluate("""(n) => {
                    const els=[...document.querySelectorAll('button,a,[role="button"]')];
                    const e=els.find(x => x.offsetParent !== null && (x.textContent||'').trim()===String(n));
                    if(e){ e.click(); return true; }
                    return false;
                }""", n)
                if ok:
                    page.wait_for_timeout(500); return True
            except Exception:
                pass
            return False

        try:
            with sync_playwright() as p:
                browser = None
                for kwargs in ({"headless": True, "channel": "chrome"}, {"headless": True}):
                    try:
                        browser = p.chromium.launch(**kwargs)
                        break
                    except Exception:
                        continue
                if not browser:
                    return None
                context = browser.new_context(locale="uz-UZ", user_agent=self.session.headers.get("User-Agent"))
                page = context.new_page()
                page.set_default_timeout(7000)

                open_search(page)
                activate_scope(page)
                if not submit_search(page):
                    log.warning("Browser ranking: ranking list submit failed scope=%s", scope)
                    browser.close(); return None
                try:
                    page.wait_for_load_state("domcontentloaded", timeout=6000)
                except Exception:
                    pass
                page.wait_for_timeout(800)

                total_pages = extract_total_pages(page)[1]
                log.info("Browser ranking page after submit: url=%s total_pages=%s", page.url, total_pages)

                # For a small/unknown number of pages, walk pages using visible controls.
                # Each page is exactly the site's visible 10-row list; the candidate's
                # rank is page_index*10 + row_index after ID verification.
                visited_pages = set()
                max_pages = min(total_pages or 300, 300)
                if max_pages is None or max_pages <= 0:
                    max_pages = 300

                # Helper to inspect one list page. We verify each Details link only as
                # needed, preferentially rows whose displayed score equals target score.
                def inspect_page(page_no: int) -> Optional[RankInfo]:
                    links = detail_links(page)
                    log.info("Browser ranking inspect page=%s rows=%s", page_no, len(links))
                    # First pass: if target score is known, prioritize matching rows.
                    order = list(range(len(links)))
                    if target_score is not None:
                        order.sort(key=lambda i: 0 if links[i][2] is not None and abs(links[i][2]-target_score) < 0.0001 else 1)
                    for idx in order:
                        href, txt, row_score = links[idx]
                        if target_score is not None and row_score is not None and abs(row_score-target_score) > 0.0001:
                            continue
                        actual = verify_detail(page, href)
                        if actual == target_id:
                            rank = (page_no - 1) * 10 + (idx + 1)
                            return RankInfo(rank=rank, total=None, scope=scope, source_url=href)
                    return None

                # Candidate might already be on initial page.
                ri = inspect_page(1)
                if ri:
                    browser.close(); return ri

                # Re-open list after visiting details, then paginate.
                open_search(page); activate_scope(page); submit_search(page)
                page.wait_for_timeout(600)

                for n in range(2, max_pages + 1):
                    if n in visited_pages:
                        continue
                    if not click_page(page, n):
                        break
                    visited_pages.add(n)
                    ri = inspect_page(n)
                    if ri:
                        browser.close(); return ri

                log.warning("Browser ranking not found scope=%s target=%s pages_scanned=%s", scope, target_id, len(visited_pages)+1)
                browser.close()
                return None
        except Exception as exc:
            log.error("Browser ranking failed scope=%s: %s", scope, exc, exc_info=True)
            return None

    def _pagination_rank(self, result: Result, scope: str = "overall") -> Optional[RankInfo]:
        """Find the candidate in public 10-row paginated ranking pages.

        The method first discovers the public pagination endpoint, then uses the
        target score to narrow the search when pages expose ordered scores. It
        never returns an estimate: the candidate ID itself must be found on a
        ranking page.
        """
        try:
            main = self._get(f"{BASE_URL}/Bakalavr/MainSearch")
            soup = BeautifulSoup(main.text, "html.parser")
            endpoints = self._pagination_urls(main.url, soup)
        except Exception as exc:
            log.warning("Pagination discovery failed: %s", exc)
            endpoints = [urljoin(BASE_URL, "/Bakalavr/Paginate")]

        target_score = self._score_num(result.total_score)
        target_id = result.candidate_id
        candidate_scope_params: list[dict[str, str]] = [{}]
        if scope == "direction":
            # Try the subject/language filters when the Details page exposes them.
            if result.specialist_subject_1:
                candidate_scope_params.append({"specialist1": result.specialist_subject_1})
                candidate_scope_params.append({"firstSpecialty": result.specialist_subject_1})
            if result.specialist_subject_2:
                candidate_scope_params.append({"specialist2": result.specialist_subject_2})
                candidate_scope_params.append({"secondSpecialty": result.specialist_subject_2})
            if result.education_language:
                candidate_scope_params.append({"educationLanguage": result.education_language})
                candidate_scope_params.append({"language": result.education_language})

        for endpoint in endpoints:
            for base_params in candidate_scope_params[:6]:
                first_page = self._page_request(endpoint, 1, base_params)
                if not first_page:
                    continue
                first_text = clean(BeautifulSoup(first_page.text, "html.parser").get_text(" ", strip=True))
                if target_id in first_text:
                    ids, _ = self._extract_page_candidates(first_page.text, target_id)
                    if target_id in ids:
                        pos = ids.index(target_id) + 1
                        return RankInfo(pos, self._extract_total_pages(first_text) and None, scope, first_page.url)
                total_pages = self._extract_total_pages(first_text)
                if not total_pages:
                    continue

                # Binary-search the score-sorted list when possible.
                lo, hi = 1, total_pages
                candidate_pages = set()
                if target_score is not None:
                    for _ in range(18):
                        if lo > hi:
                            break
                        mid = (lo + hi) // 2
                        rr = self._page_request(endpoint, mid, base_params)
                        if not rr:
                            break
                        ids, scores = self._extract_page_candidates(rr.text, target_id)
                        txt = clean(BeautifulSoup(rr.text, "html.parser").get_text(" ", strip=True))
                        if target_id in ids:
                            candidate_pages.add(mid)
                            break
                        nums = [x for x in scores if x is not None]
                        if not nums:
                            break
                        hi_score, lo_score = max(nums), min(nums)
                        if target_score > hi_score:
                            hi = mid - 1
                        elif target_score < lo_score:
                            lo = mid + 1
                        else:
                            candidate_pages.update(range(max(1, mid-2), min(total_pages, mid+2)+1))
                            break
                # If score search is unavailable, inspect a bounded window first,
                # then expand only as needed. This avoids hammering the official site.
                if not candidate_pages:
                    candidate_pages.update(range(1, min(total_pages, 20) + 1))
                else:
                    expanded = set(candidate_pages)
                    for pg in list(candidate_pages):
                        for d in (-2, -1, 0, 1, 2):
                            if 1 <= pg + d <= total_pages:
                                expanded.add(pg + d)
                    candidate_pages = expanded

                for pg in sorted(candidate_pages):
                    rr = self._page_request(endpoint, pg, base_params)
                    if not rr:
                        continue
                    ids, scores = self._extract_page_candidates(rr.text, target_id)
                    if target_id not in ids:
                        continue
                    pos = ids.index(target_id) + 1
                    # 10 results/page as observed on the site; if the page exposes
                    # an explicit ordinal we prefer it.
                    body = clean(BeautifulSoup(rr.text, "html.parser").get_text(" ", strip=True))
                    ordinal = None
                    m = re.search(r"(?:^|\s)(\d+)\s+[^\d]{1,80}" + re.escape(target_id), body)
                    if m:
                        try: ordinal = int(m.group(1))
                        except Exception: ordinal = None
                    rank = ordinal or ((pg - 1) * 10 + pos)
                    return RankInfo(rank, None, scope, rr.url)
        return None

    def _discover_rank_from_public_pages(self, result: Result) -> tuple[Optional[RankInfo], Optional[RankInfo]]:
        # Search only official pages that explicitly contain the candidate ID.
        # We also scan links/scripts for ranking-like endpoints rather than
        # guessing a rank from score alone.
        candidates = [
            "/Bakalavr/MainSearch", "/Bakalavr/Ranking", "/Bakalavr/Rank",
            "/Bakalavr/Statistics", "/Bakalavr/Paginate", "/Bakalavr/Results",
            "/Bakalavr/Search", "/Bakalavr/Rating", "/Bakalavr/Reyting",
        ]
        urls = set(BASE_URL + p for p in candidates)
        try:
            main = self._get(f"{BASE_URL}/Bakalavr/MainSearch")
            soup = BeautifulSoup(main.text, "html.parser")
            for tag in soup.find_all(True):
                for attr in ("href", "action", "data-url", "data-action"):
                    v = tag.get(attr)
                    if v and any(k in v.lower() for k in ("rank", "reyting", "rating", "paginate", "result", "search")):
                        urls.add(urljoin(main.url, v))
            scripts = "\n".join(s.get_text(" ", strip=True) for s in soup.find_all("script"))
            for m in re.finditer(r"[\"'`]((?:https?://[^\"'`\s]+)?/[^\"'`\s]*(?:rank|reyting|rating|paginate|result)[^\"'`\s]*)[\"'`]", scripts, re.I):
                urls.add(urljoin(BASE_URL, m.group(1)))
        except Exception:
            pass

        for url in urls:
            for params in (
                {"id": result.candidate_id},
                {"candidateId": result.candidate_id},
                {"abituriyentId": result.candidate_id},
                {"search": result.candidate_id},
                {"query": result.candidate_id},
            ):
                try:
                    r = self.session.get(url, params=params, headers={"Referer": f"{BASE_URL}/Bakalavr/MainSearch", "X-Requested-With": "XMLHttpRequest", "Accept": "text/html,application/json,text/plain,*/*"}, timeout=REQUEST_TIMEOUT)
                    text = clean(BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True))
                    if result.candidate_id not in text:
                        continue
                    overall = self._rank_from_text(text, "umumiy") or self._rank_from_text(text, "reyting")
                    direction = self._rank_from_text(text, "yo['’‘]?nalish") or self._rank_from_text(text, "tanlangan")
                    if overall or direction:
                        return overall, direction
                except requests.RequestException:
                    continue
        return None, None

    def parse_result(self, content: str, result_url: str, cid: str) -> Result:
        soup = BeautifulSoup(content, "html.parser")
        raw = clean(soup.get_text(" ", strip=True))
        lines = [clean(x) for x in soup.stripped_strings if clean(x)]

        # Name: inspect labels around the ID, but ignore navigation/title/notice text.
        name = None
        forbidden = re.compile(r"(Bakalavr|Mandat|Izoh|O‘zbekiston Respublikasi|Bilim va malakalarni|Agentligi|UZ|RU|Ma’lumotlar)", re.I)
        for i, line in enumerate(lines):
            if re.search(r"Abituriyent\s+ID\s+raqami", line, re.I):
                for j in range(i - 1, max(-1, i - 6), -1):
                    candidate = lines[j]
                    if candidate and not forbidden.search(candidate) and len(candidate) < 120:
                        if re.search(r"[A-ZА-ЯЎҚҒҲ][A-ZА-ЯЎҚҒҲ'’`.-]+", candidate, re.I):
                            name = candidate
                            break
                break
        if name and len(name) > 80:
            name = None

        education_language = first([r"Ta['’‘]lim tili\s*[:\-]\s*([^\n]+?)(?:Majburiy|📚|Fanlar|$)"], raw)
        scores = re.findall(r"Ball\s*[:\-]?\s*([0-9]+(?:[,.][0-9]+)?)", raw, re.I)
        corrects = re.findall(r"To['’‘’`]?g['’‘’`]ri javoblar soni\s*[:\-]?\s*(\d+)", raw, re.I)
        mandatory_score = scores[0] if len(scores) > 0 else None
        specialist_score = scores[1] if len(scores) > 1 else None
        second_specialist_score = scores[2] if len(scores) > 2 else None
        mandatory_correct = corrects[0] if len(corrects) > 0 else None
        specialist_correct = corrects[1] if len(corrects) > 1 else None
        second_specialist_correct = corrects[2] if len(corrects) > 2 else None

        privilege_score = first([r"Imtiyoz ball\s*[:\-]\s*([0-9]+(?:[,.][0-9]+)?)"], raw)
        creative_score = first([r"Ijodiy ball\s*[:\-]\s*([0-9]+(?:[,.][0-9]+)?)"], raw)
        cefr_score = first([r"CEFR ball\s*[:\-]\s*([0-9]+(?:[,.][0-9]+)?)"], raw)
        national_cert_score = first([r"Milliy sertifikat\s*/\s*Olimpiada\s*[:\-]\s*([0-9]+(?:[,.][0-9]+)?)"], raw)
        total_score = first([r"Umumiy ball\s*[:\-]\s*([0-9]+(?:[,.][0-9]+)?)"], raw)

        university = first([
            r"OTM\s*[:\-]\s*(.+?)(?=Yo['’‘]nalish|Ta['’‘]lim shakli|$)",
            r"Universitet\s*[:\-]\s*(.+?)(?=Yo['’‘]nalish|Ta['’‘]lim shakli|$)",
        ], raw)
        direction = first([r"Yo['’‘]nalish\s*[:\-]\s*(.+?)(?=Ta['’‘]lim shakli|$)"], raw)
        education_form = first([r"Ta['’‘]lim shakli\s*[:\-]\s*([^|]+)"], raw)
        specialist_subject_1 = first([r"1[- ]mutaxassislik fani\s*[:\-]?\s*([^|]+?)(?=2[- ]mutaxassislik|Ta['’‘]lim tili|$)"], raw)
        specialist_subject_2 = first([r"2[- ]mutaxassislik fani\s*[:\-]?\s*([^|]+?)(?=Ta['’‘]lim tili|$)"], raw)

        status = None
        for phrase in [
            "Davlat granti asosida tavsiya etildi",
            "To‘lov-kontrakt asosida tavsiya etildi",
            "To'lov-kontrakt asosida tavsiya etildi",
            "Talabalikka tavsiya etildi",
            "Tavsiya etilmadi",
        ]:
            if phrase.lower() in raw.lower():
                status = phrase
                break

        pdf_url = None
        for a in soup.find_all("a", href=True):
            href = a.get("href", "")
            label = clean(a.get_text(" ", strip=True)).lower()
            full = urljoin(result_url, href)
            if ".pdf" in urlparse(full).path.lower() or "pdf" in label or "yuklab" in label:
                pdf_url = full
                break

        result = Result(
            candidate_id=cid,
            name=name,
            education_language=education_language,
            mandatory_correct=mandatory_correct,
            mandatory_score=mandatory_score,
            specialist_correct=specialist_correct,
            specialist_score=specialist_score,
            second_specialist_correct=second_specialist_correct,
            second_specialist_score=second_specialist_score,
            privilege_score=privilege_score,
            creative_score=creative_score,
            cefr_score=cefr_score,
            national_cert_score=national_cert_score,
            total_score=total_score,
            university=university,
            direction=direction,
            education_form=education_form,
            specialist_subject_1=specialist_subject_1,
            specialist_subject_2=specialist_subject_2,
            status=status,
            pdf_url=pdf_url,
            result_url=result_url,
            raw_text=raw,
        )

        # IMPORTANT: ranking is intentionally NOT calculated here. Current-result
        # lookup must stay fast and must not wait for pagination/browser crawling.
        return result

    def fetch_rankings(self, result: Result) -> Result:
        """Heavy ranking lookup, intentionally separated from current-result lookup."""
        overall = None
        direction_rank = None

        # The user's browser capture proved that the official /Bakalavr route
        # returns a real 10-row ordered list. Use that first.
        try:
            overall = self._rank_from_global_bachelor_pages(result)
        except Exception:
            log.exception("Direct global ranking failed for %s", result.candidate_id)

        # Keep the stronger browser/pagination mechanisms as fallbacks.
        if not overall:
            try:
                overall = self._browser_rank(result, "overall")
            except Exception:
                log.exception("Browser overall ranking failed for %s", result.candidate_id)

        # Direction ranking is not yet exposed by the captured endpoint. Do not
        # fabricate it; use existing strict discovery only as a fallback.
        if not direction_rank:
            try:
                direction_rank = self._browser_rank(result, "direction")
            except Exception:
                log.exception("Browser direction ranking failed for %s", result.candidate_id)

        if not overall or not direction_rank:
            try:
                o2, d2 = self._discover_rank_from_public_pages(result)
                overall = overall or o2
                direction_rank = direction_rank or d2
            except Exception:
                log.exception("Public ranking discovery failed for %s", result.candidate_id)

        if not overall:
            try:
                overall = self._pagination_rank(result, "overall")
            except Exception:
                log.exception("Overall pagination ranking failed for %s", result.candidate_id)
        if not direction_rank:
            try:
                direction_rank = self._pagination_rank(result, "direction")
            except Exception:
                log.exception("Direction pagination ranking failed for %s", result.candidate_id)

        result.rank_overall = overall
        result.rank_direction = direction_rank
        return result

    def fetch_result(self, cid: str) -> Optional[Result]:
        url = self.find_candidate_url(cid)
        if not url:
            return None
        page = self._get(url, headers={"Referer": f"{BASE_URL}/Bakalavr/MainSearch"})
        actual = self._page_candidate_id(page.text)
        if actual != cid:
            log.error("Safety check blocked mismatched candidate: requested=%s actual=%s url=%s", cid, actual, page.url)
            return None
        return self.parse_result(page.text, page.url, cid)

    def download_pdf(self, url: str) -> Optional[bytes]:
        r = self._get(url)
        ctype = (r.headers.get("content-type") or "").lower()
        if "pdf" in ctype or r.content[:4] == b"%PDF":
            return r.content
        return None


client = MandatClient()
MONITOR_CONCURRENCY = 30
monitor_sem = asyncio.Semaphore(MONITOR_CONCURRENCY)


# ============================================================
# USER SESSION STATE
# ============================================================
dp = Dispatcher()
last_request: dict[int, float] = {}
pending_candidate: dict[int, str] = {}
broadcast_waiting: set[int] = set()
monitor_enabled = True
current_cache: dict[str, tuple[float, Result]] = {}
rank_cache: dict[str, tuple[float, Result]] = {}
CACHE_TTL = 45
REDISCOVERY_INTERVAL = 10
rediscovery_last: dict[str, float] = {}
rediscovery_locks: dict[str, asyncio.Lock] = {}


def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS


def throttled(uid: int) -> bool:
    t = time.time()
    old = last_request.get(uid, 0)
    if t - old < USER_COOLDOWN:
        return True
    last_request[uid] = t
    return False


# ============================================================
# UI
# ============================================================
def main_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Hozirgi natijam", callback_data="menu:current")],
        [InlineKeyboardButton(text="🏆 Reytingim", callback_data="menu:rank")],
        [InlineKeyboardButton(text="🔔 Mandatni kuzatish", callback_data="menu:watch")],
        [InlineKeyboardButton(text="📋 Mening arizalarim", callback_data="menu:apps")],
        [InlineKeyboardButton(text="ℹ️ Yordam", callback_data="menu:help")],
    ])


def result_kb(cid: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Natijani yangilash", callback_data=f"refresh:{cid}")],
        [InlineKeyboardButton(text="🔔 Mandatga ariza berish", callback_data=f"watch_confirm:{cid}")],
        [InlineKeyboardButton(text="🏆 Reytingim", callback_data=f"rank:{cid}")],
    ])


def application_confirm_kb(cid: str):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Tasdiqlayman", callback_data=f"watch_do:{cid}")],
        [InlineKeyboardButton(text="↩️ Bekor qilish", callback_data="menu:back")],
    ])


def apps_kb(rows):
    buttons = []
    for row in rows[:10]:
        icon = "⏳" if row["status"] == "waiting" else "✅"
        buttons.append([InlineKeyboardButton(text=f"{icon} {row['candidate_id']}", callback_data=f"app:{row['candidate_id']}")])
    buttons.append([InlineKeyboardButton(text="➕ Yangi ID", callback_data="menu:current")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def admin_kb():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📊 Statistika", callback_data="admin:stats")],
        [InlineKeyboardButton(text="🟢 Monitoring", callback_data="admin:monitor")],
        [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast")],
    ])


def result_text(r: Result, final: bool = False) -> str:
    title = "🎉 <b>Yakuniy mandat natijangiz</b>" if final else "📊 <b>Hozirgi natijangiz</b>"
    out = [title, "", f"🆔 ID: <b>{esc(r.candidate_id)}</b>"]
    if r.name:
        out.append(f"👤 F.I.Sh.: <b>{esc(r.name)}</b>")
    if r.education_language:
        out.append(f"🗣 Ta’lim tili: <b>{esc(r.education_language)}</b>")
    if r.education_form:
        out.append(f"🏫 Ta’lim shakli: <b>{esc(r.education_form)}</b>")

    out += ["", "<b>📚 Fanlar bo‘yicha natijalar</b>"]
    for label, correct, score in [
        ("Majburiy fanlar", r.mandatory_correct, r.mandatory_score),
        ("1-mutaxassislik", r.specialist_correct, r.specialist_score),
        ("2-mutaxassislik", r.second_specialist_correct, r.second_specialist_score),
    ]:
        if correct is not None or score is not None:
            out.append(f"• {label}: <b>{esc(correct or '—')}</b> ta to‘g‘ri, <b>{esc(score or '—')}</b> ball")

    extras = [
        ("Imtiyoz ball", r.privilege_score),
        ("Ijodiy ball", r.creative_score),
        ("CEFR ball", r.cefr_score),
        ("Milliy sertifikat / Olimpiada", r.national_cert_score),
    ]
    if any(v for _, v in extras) or r.total_score:
        out += ["", "<b>📈 Qo‘shimcha natijalar</b>"]
        for label, value in extras:
            if value is not None:
                out.append(f"• {label}: <b>{esc(value)}</b>")
        if r.total_score:
            out.append(f"• 🎯 Umumiy ball: <b>{esc(r.total_score)}</b>")

    # Ranking is intentionally omitted from the fast current-result message.
    # It is available from the separate “🏆 Reytingim” action.

    if r.university:
        out.extend(["", "<b>🎓 Mandat</b>"])
        out.append(f"• OTM: <b>{esc(r.university)}</b>")
    if r.direction:
        out.append(f"• Yo‘nalish: <b>{esc(r.direction)}</b>")
    if r.status:
        out.append(f"• Holat: <b>{esc(r.status)}</b>")

    if final:
        out.extend(["", "📌 Yakuniy natija rasmiy tizimdan olindi."])
    else:
        out.extend(["", "ℹ️ Bu rasmiy tizimdagi hozirgi natija. Yakuniy mandat hali alohida e’lon qilinishi mumkin."])
    return "\n".join(out)


# ============================================================
# CORE OPERATIONS
# ============================================================
async def fetch_exact_url(cid: str, url: str) -> Optional[Result]:
    try:
        page = await asyncio.to_thread(client._get, url, headers={"Referer": f"{BASE_URL}/Bakalavr/MainSearch"})
        actual = client._page_candidate_id(page.text)
        if actual != cid:
            log.warning("Exact URL safety mismatch: requested=%s actual=%s url=%s", cid, actual, page.url)
            return None
        return client.parse_result(page.text, page.url, cid)
    except Exception:
        log.exception("Exact URL fetch failed: %s", cid)
        return None

async def fetch_current(cid: str) -> Optional[Result]:
    # Fast path + short cache so repeated refreshes do not hit UZBMB again.
    cached = current_cache.get(cid)
    if cached and (time.time() - cached[0]) < CACHE_TTL:
        return cached[1]
    result = await asyncio.to_thread(client.fetch_result, cid)
    if result:
        current_cache[cid] = (time.time(), result)
    return result


async def fetch_rankings(cid: str) -> Optional[Result]:
    # Heavy path: ranking may crawl pagination/browser and is deliberately
    # separated from the fast current-result response.
    result = await fetch_current(cid)
    if not result:
        return None
    return await asyncio.to_thread(client.fetch_rankings, result)


async def send_current(message: Message, cid: str):
    if throttled(message.from_user.id):
        await message.answer("⏳ Bir necha soniya kutib, qayta urinib ko‘ring.")
        return
    try:
        result = await fetch_current(cid)
        if not result:
            await message.answer(
                "⚠️ <b>Natija topilmadi.</b>\n\nID raqam to‘g‘riligini tekshiring yoki birozdan keyin qayta urinib ko‘ring.",
                reply_markup=main_kb(),
            )
            return
        await message.answer(result_text(result), reply_markup=result_kb(cid))
    except Exception:
        log.exception("Current result failed: %s", cid)
        await message.answer("⚠️ Rasmiy tizimga ulanishda vaqtinchalik xatolik yuz berdi. Keyinroq qayta urinib ko‘ring.")


# ============================================================
# COMMANDS / MESSAGE HANDLERS
# ============================================================
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "<b>🎓 LEGALIX — OTM MANDAT 2026</b>\n\n"
        "Abituriyent ID raqamingizni yuboring. Avval hozirgi rasmiy natijangizni ko‘rsataman.\n\n"
        "Shundan keyin istasangiz mandat e’lon qilinganda natijangizni avtomatik yuborish uchun alohida ariza qoldirasiz.\n\n"
        "🆔 ID: 7 xonali raqam",
        reply_markup=main_kb(),
    )


@dp.message(Command("help"))
async def help_cmd(message: Message):
    await message.answer(
        "<b>ℹ️ Foydalanish</b>\n\n"
        "1. 7 xonali ID yuboring.\n"
        "2. Hozirgi natijani ko‘ring.\n"
        "3. Xohlasangiz ‘Mandatga ariza berish’ni tasdiqlang.\n"
        "4. Mandat yakuniy natijasi chiqqanda bot avtomatik xabar yuboradi.\n\n"
        "/result 4751608 — natijani tekshirish\n"
        "/my — arizalarim\n"
        "/cancel 4751608 — kuzatuvni bekor qilish",
        reply_markup=main_kb(),
    )


@dp.message(Command("result"))
async def result_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not ID_RE.fullmatch(parts[1].strip()):
        await message.answer("Foydalanish: <code>/result 4751608</code>")
        return
    await send_current(message, parts[1].strip())


@dp.message(Command("my"))
async def my_cmd(message: Message):
    rows = await db.list_user(message.from_user.id)
    if not rows:
        await message.answer("📋 Sizda hozircha mandat kuzatuvi bo‘yicha ariza yo‘q.", reply_markup=main_kb())
        return
    lines = ["<b>📋 Mening arizalarim</b>", ""]
    for row in rows:
        status = "⏳ Kuzatilmoqda" if row["status"] == "waiting" else "✅ Natija yuborilgan"
        r = Result.from_json(row["latest_json"]) if row["latest_json"] else None
        score = f" — {esc(r.total_score)} ball" if r and r.total_score else ""
        lines.append(f"• <b>{row['candidate_id']}</b> — {status}{score}")
    await message.answer("\n".join(lines), reply_markup=apps_kb(rows))


@dp.message(Command("cancel"))
async def cancel_cmd(message: Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2 or not ID_RE.fullmatch(parts[1].strip()):
        await message.answer("Foydalanish: <code>/cancel 4751608</code>")
        return
    ok = await db.cancel(message.from_user.id, parts[1].strip())
    await message.answer("✅ Kuzatuv bekor qilindi." if ok else "ℹ️ Bu ID bo‘yicha faol ariza topilmadi.", reply_markup=main_kb())


@dp.message(F.text)
async def text_handler(message: Message):
    uid = message.from_user.id

    if is_admin(uid) and uid in broadcast_waiting and not message.text.startswith("/"):
        broadcast_waiting.discard(uid)
        ids = await db.user_ids()
        sent = failed = 0
        for target in ids:
            try:
                await message.bot.send_message(target, message.text)
                sent += 1
            except Exception:
                failed += 1
            await asyncio.sleep(0.05)
        await message.answer(f"📢 Broadcast yakunlandi.\n✅ {sent}\n❌ {failed}", reply_markup=admin_kb())
        return

    text = re.sub(r"\s+", "", message.text or "")
    if text.startswith("/"):
        return
    if not ID_RE.fullmatch(text):
        await message.answer("❗️ 7 xonali abituriyent ID raqamingizni yuboring.", reply_markup=main_kb())
        return

    pending_candidate[uid] = text
    await send_current(message, text)


# ============================================================
# CALLBACKS
# ============================================================
@dp.callback_query(F.data == "menu:current")
async def menu_current(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("🆔 7 xonali abituriyent ID raqamingizni yuboring.")


@dp.callback_query(F.data == "menu:back")
async def menu_back(cb: CallbackQuery):
    pending_candidate.pop(cb.from_user.id, None)
    await cb.answer()
    await cb.message.answer("Bosh menyu.", reply_markup=main_kb())


@dp.callback_query(F.data == "menu:help")
async def menu_help(cb: CallbackQuery):
    await cb.answer()
    await help_cmd(cb.message)


@dp.callback_query(F.data == "menu:apps")
async def menu_apps(cb: CallbackQuery):
    await cb.answer()
    rows = await db.list_user(cb.from_user.id)
    if not rows:
        await cb.message.answer("📋 Sizda hozircha ariza yo‘q.", reply_markup=main_kb())
        return
    await cb.message.answer("<b>📋 Mening arizalarim</b>", reply_markup=apps_kb(rows))


@dp.callback_query(F.data == "menu:rank")
async def menu_rank(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("🏆 Reytingni ko‘rish uchun avval 7 xonali ID raqamingizni yuboring.")


@dp.callback_query(F.data == "menu:watch")
async def menu_watch(cb: CallbackQuery):
    await cb.answer()
    await cb.message.answer("🔔 Mandat kuzatuvini yoqish uchun avval 7 xonali ID raqamingizni yuboring.")


@dp.callback_query(F.data.startswith("refresh:"))
async def refresh_cb(cb: CallbackQuery):
    cid = cb.data.split(":", 1)[1]
    if not ID_RE.fullmatch(cid):
        await cb.answer("ID noto‘g‘ri", show_alert=True)
        return
    await cb.answer("🔄 Yangilanmoqda...")
    try:
        result = await fetch_current(cid)
        if not result:
            await cb.message.answer("⚠️ Natija topilmadi.")
            return
        await cb.message.answer(result_text(result), reply_markup=result_kb(cid))
    except Exception:
        log.exception("Refresh failed")
        await cb.message.answer("⚠️ Rasmiy tizimga ulanishda vaqtinchalik xatolik yuz berdi.")


@dp.callback_query(F.data.startswith("watch_confirm:"))
async def watch_confirm_cb(cb: CallbackQuery):
    cid = cb.data.split(":", 1)[1]
    await cb.answer()
    row = await db.get(cb.from_user.id, cid)
    if row and row["status"] == "waiting":
        await cb.message.answer(f"✅ <b>{cid}</b> ID bo‘yicha mandat kuzatuvi allaqachon yoqilgan.")
        return
    pending_candidate[cb.from_user.id] = cid
    await cb.message.answer(
        "<b>🔔 Mandat kuzatuviga ariza</b>\n\n"
        f"🆔 ID: <b>{cid}</b>\n\n"
        "Tasdiqlaganingizdan so‘ng bot ushbu ID bo‘yicha mandat yakuniy natijasini kuzatadi va natija e’lon qilinganda Telegram orqali avtomatik yuboradi.\n\n"
        "<i>Eslatma: hozirgi natijani ko‘rishning o‘zi ariza yuborilganini anglatmaydi.</i>",
        reply_markup=application_confirm_kb(cid),
    )


@dp.callback_query(F.data.startswith("watch_do:"))
async def watch_do_cb(cb: CallbackQuery):
    cid = cb.data.split(":", 1)[1]
    try:
        result = await fetch_current(cid)
        if not result:
            await cb.answer("Hozircha natija topilmadi", show_alert=True)
            return
        await db.add_subscription(
            cb.from_user.id,
            cb.from_user.username or "",
            " ".join(x for x in [cb.from_user.first_name, cb.from_user.last_name] if x),
            cid,
            result,
        )
        pending_candidate.pop(cb.from_user.id, None)
        await cb.answer("✅ Ariza qabul qilindi")
        await cb.message.answer(
            f"✅ <b>Ariza qabul qilindi.</b>\n\n"
            f"🆔 ID: <b>{cid}</b>\n"
            f"🎯 Hozirgi ball: <b>{esc(result.total_score or '—')}</b>\n\n"
            "🔔 Mandat natijasi e’lon qilinishi bilan sizga avtomatik yuboriladi. Kuzatuv tezkor rejimda ishlaydi.",
            reply_markup=main_kb(),
        )
    except Exception:
        log.exception("Watch subscribe failed")
        await cb.answer("Xatolik yuz berdi", show_alert=True)


@dp.callback_query(F.data.startswith("rank:"))
async def rank_cb(cb: CallbackQuery):
    cid = cb.data.split(":", 1)[1]
    await cb.answer("🔎 Reyting tekshirilmoqda...")
    result = await fetch_current(cid)
    if not result:
        await cb.message.answer("⚠️ Natija topilmadi.")
        return

    lines = ["<b>🏆 Reyting</b>", ""]
    lines.append("🌐 Umumiy: <b>aniqlanmoqda...</b>")
    lines.append("🎓 OTM + yo‘nalish: <b>aniqlanmoqda...</b>")
    if result.total_score:
        lines.append(f"🎯 Ball: <b>{esc(result.total_score)}</b>")

    msg = await cb.message.answer("\n".join(lines), reply_markup=result_kb(cid))
    asyncio.create_task(_finish_all_ranks(msg, result))


async def _finish_all_ranks(msg: Message, result: Result):
    cid = result.candidate_id
    overall_line = "🌐 Umumiy: <b>hozircha aniqlanmadi</b>"
    try:
        cached = rank_cache.get(cid)
        if cached and (time.time() - cached[0]) < RANK_CACHE_TTL and cached[1].rank_overall:
            ranked = cached[1]
        else:
            ranked = result
            ranked.rank_overall = await asyncio.to_thread(client._rank_from_global_bachelor_pages, result)
            rank_cache[cid] = (time.time(), ranked)

        if ranked.rank_overall and ranked.rank_overall.rank:
            total = f" / {ranked.rank_overall.total}" if ranked.rank_overall.total else ""
            overall_line = f"🌐 Umumiy: <b>{ranked.rank_overall.rank}-o‘rin{esc(total)}</b>"

        # Global rank appears as soon as it is ready; direction rank never blocks it.
        first_text = ["<b>🏆 Reyting</b>", "", overall_line, "🎓 OTM + yo‘nalish: <b>aniqlanmoqda...</b>"]
        if result.total_score:
            first_text.append(f"🎯 Ball: <b>{esc(result.total_score)}</b>")
        await msg.edit_text("\n".join(first_text), reply_markup=result_kb(cid))

        direction = None
        try:
            direction = await asyncio.to_thread(client._browser_rank, result, "direction")
        except Exception:
            log.exception("Direction browser rank failed: %s", cid)
        if not direction:
            try:
                _, direction = await asyncio.to_thread(client._discover_rank_from_public_pages, result)
            except Exception:
                direction = None
        if not direction:
            try:
                direction = await asyncio.to_thread(client._pagination_rank, result, "direction")
            except Exception:
                direction = None

        direction_line = "🎓 OTM + yo‘nalish: <b>rasmiy manbada hozircha aniqlanmadi</b>"
        if direction and direction.rank:
            total = f" / {direction.total}" if direction.total else ""
            direction_line = f"🎓 OTM + yo‘nalish: <b>{direction.rank}-o‘rin{esc(total)}</b>"

        final_text = ["<b>🏆 Reyting</b>", "", overall_line, direction_line]
        if result.total_score:
            final_text.append(f"🎯 Ball: <b>{esc(result.total_score)}</b>")
        await msg.edit_text("\n".join(final_text), reply_markup=result_kb(cid))
    except Exception:
        log.exception("Rank background failed: %s", cid)
        try:
            await msg.edit_text(
                "<b>🏆 Reyting</b>\n\n"
                f"{overall_line}\n"
                "🎓 OTM + yo‘nalish: <b>vaqtincha aniqlanmadi</b>\n"
                f"🎯 Ball: <b>{esc(result.total_score or '—')}</b>",
                reply_markup=result_kb(cid),
            )
        except Exception:
            pass


@dp.callback_query(F.data.startswith("app:"))
async def app_cb(cb: CallbackQuery):
    cid = cb.data.split(":", 1)[1]
    row = await db.get(cb.from_user.id, cid)
    if not row:
        await cb.answer("Ariza topilmadi", show_alert=True)
        return
    await cb.answer()
    r = Result.from_json(row["latest_json"]) if row["latest_json"] else None
    if r:
        await cb.message.answer(result_text(r), reply_markup=result_kb(cid))
    else:
        await cb.message.answer(f"🆔 {cid}\n⏳ Natija hali saqlanmagan.")


# ============================================================
# ADMIN
# ============================================================
@dp.message(Command("admin"))
async def admin_cmd(message: Message):
    if not is_admin(message.from_user.id):
        await message.answer("Ruxsat yo‘q.")
        return
    await message.answer("<b>👑 LEGALIX ADMIN PANEL</b>", reply_markup=admin_kb())


@dp.message(Command("stats"))
async def stats_cmd(message: Message):
    if not is_admin(message.from_user.id):
        return
    total, waiting, notified, users, known = await db.stats()
    await message.answer(
        f"<b>📊 Statistika</b>\n\n"
        f"👥 Foydalanuvchilar: <b>{users}</b>\n"
        f"📝 Arizalar: <b>{total}</b>\n"
        f"⏳ Kuzatilmoqda: <b>{waiting}</b>\n"
        f"✅ Yakuniy yuborilgan: <b>{notified}</b>\n"
        f"💾 Ma’lumoti saqlangan: <b>{known}</b>\n"
        f"🟢 Monitoring: <b>{'YOQILGAN' if monitor_enabled else 'O‘CHIRILGAN'}</b>",
        reply_markup=admin_kb(),
    )


@dp.callback_query(F.data == "admin:stats")
async def admin_stats_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo‘q", show_alert=True)
        return
    await cb.answer()
    await stats_cmd(cb.message)


@dp.callback_query(F.data == "admin:monitor")
async def admin_monitor_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo‘q", show_alert=True)
        return
    total, waiting, notified, users, known = await db.stats()
    await cb.answer()
    await cb.message.answer(
        f"<b>🟢 Monitoring</b>\n\n"
        f"Holat: <b>{'YOQILGAN' if monitor_enabled else 'O‘CHIRILGAN'}</b>\n"
        f"Interval: <b>{CHECK_INTERVAL} soniya</b>\n"
        f"Kuzatuvdagi ID: <b>{waiting}</b>\n"
        f"Saqlangan natijalar: <b>{known}</b>"
    )


@dp.callback_query(F.data == "admin:broadcast")
async def admin_broadcast_cb(cb: CallbackQuery):
    if not is_admin(cb.from_user.id):
        await cb.answer("Ruxsat yo‘q", show_alert=True)
        return
    broadcast_waiting.add(cb.from_user.id)
    await cb.answer()
    await cb.message.answer("📢 Barcha saqlangan foydalanuvchilarga yuboriladigan xabarni yuboring.")


# ============================================================
# PDF + MONITORING
# ============================================================
def is_final_mandat(result: Result, previous: Result | None) -> bool:
    raw = (result.raw_text or "").lower()
    # Explicit official placement signals.
    final_phrases = [
        "davlat granti asosida tavsiya etildi",
        "to‘lov-kontrakt asosida tavsiya etildi",
        "to'lov-kontrakt asosida tavsiya etildi",
        "talabalikka tavsiya etildi",
        "tavsiya etilmadi",
        "talabalikka tavsiya qilindi",
        "mandat natijasi",
    ]
    if any(p in raw for p in final_phrases):
        return True
    # A final page may expose placement fields before a standard status label.
    placement_tokens = ["grant", "kontrakt", "tavsiya", "talabalik"]
    has_placement = (result.university and result.direction and any(t in raw for t in placement_tokens))
    if has_placement and previous and result.fingerprint != previous.fingerprint:
        return True
    return False

async def notify_pdf_background(bot: Bot, row: sqlite3.Row, result: Result):
    if not result.pdf_url:
        return
    try:
        data = await asyncio.to_thread(client.download_pdf, result.pdf_url)
        if data:
            doc = io.BytesIO(data)
            doc.name = f"mandat_{row['candidate_id']}.pdf"
            await bot.send_document(row["telegram_id"], doc, caption="📄 Rasmiy PDF natija")
            log.info("PDF sent: candidate=%s user=%s", row["candidate_id"], row["telegram_id"])
    except Exception:
        log.exception("PDF send failed for candidate=%s", row["candidate_id"])

async def notify_final(bot: Bot, row: sqlite3.Row, result: Result):
    # Critical text notification is sent first. PDF is detached so it cannot
    # delay the mandate notification.
    last_exc = None
    for attempt in range(3):
        try:
            await bot.send_message(row["telegram_id"], result_text(result, final=True))
            last_exc = None
            break
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(0.5 * (attempt + 1))
    if last_exc:
        raise last_exc
    if result.pdf_url:
        asyncio.create_task(notify_pdf_background(bot, row, result))


async def _rediscover_candidate(cid: str) -> Optional[str]:
    now = time.monotonic()
    last = rediscovery_last.get(cid, 0.0)
    if now - last < REDISCOVERY_INTERVAL:
        return None
    lock = rediscovery_locks.setdefault(cid, asyncio.Lock())
    if lock.locked():
        return None
    async with lock:
        now = time.monotonic()
        if now - rediscovery_last.get(cid, 0.0) < REDISCOVERY_INTERVAL:
            return None
        rediscovery_last[cid] = now
        try:
            return await asyncio.to_thread(client.find_candidate_url, cid)
        except Exception:
            log.exception("Rediscovery failed: candidate=%s", cid)
            return None


async def _monitor_exact_and_rediscover(bot: Bot, row: sqlite3.Row):
    """Dual monitoring:
    1) saved Details URL = fastest path, checked every cycle;
    2) ID rediscovery = independent safety path, periodically refreshed.
    """
    cid = row["candidate_id"]
    previous = Result.from_json(row["latest_json"]) if row["latest_json"] else None
    saved_url = row["result_url"]

    # Fast path: current saved Details URL.
    result = None
    if saved_url:
        result = await fetch_exact_url(cid, saved_url)

    # Safety path: if saved URL is stale/unavailable, immediately try rediscovery;
    # otherwise refresh periodically so a hashId change is detected before final.
    need_rediscovery = result is None or (time.monotonic() - rediscovery_last.get(cid, 0.0) >= REDISCOVERY_INTERVAL)
    rediscovered_url = None
    if need_rediscovery:
        rediscovered_url = await _rediscover_candidate(cid)
        if rediscovered_url and rediscovered_url != saved_url:
            refreshed = await fetch_exact_url(cid, rediscovered_url)
            if refreshed is not None:
                result = refreshed
                saved_url = rediscovered_url
        elif result is None and rediscovered_url:
            refreshed = await fetch_exact_url(cid, rediscovered_url)
            if refreshed is not None:
                result = refreshed
                saved_url = rediscovered_url

    return result, saved_url, previous


async def monitor_one(bot: Bot, row: sqlite3.Row):
    async with monitor_sem:
        cid = row["candidate_id"]
        try:
            result, current_url, previous = await _monitor_exact_and_rediscover(bot, row)
            if not result:
                await db.mark_checked(row["id"])
                return

            # Persist a newly discovered hashId immediately. This ensures a site-side
            # URL change does not break monitoring on later cycles or after restart.
            if current_url and current_url != row["result_url"]:
                result.result_url = current_url
                await db.update_result_url(row["id"], current_url, result)
            else:
                await db.save_latest(row["id"], result)

            if is_final_mandat(result, previous):
                try:
                    await notify_final(bot, row, result)
                    await db.mark_notified(row["id"], result)
                    log.info("FINAL MANDAT SENT: candidate=%s user=%s url=%s", cid, row["telegram_id"], result.result_url)
                except Exception:
                    log.exception("Final notification failed: candidate=%s", cid)
            else:
                await db.mark_checked(row["id"])
        except Exception:
            log.exception("Monitoring failed: subscription=%s candidate=%s", row["id"], cid)

async def monitor_loop(bot: Bot):
    global monitor_enabled
    log.info("DUAL mandate monitoring started: fast_url=%ss rediscovery=%ss concurrency=%s", CHECK_INTERVAL, REDISCOVERY_INTERVAL, MONITOR_CONCURRENCY)
    while True:
        try:
            if not monitor_enabled:
                await asyncio.sleep(CHECK_INTERVAL)
                continue
            rows = await db.waiting(MAX_BATCH)
            if rows:
                await asyncio.gather(*(monitor_one(bot, row) for row in rows), return_exceptions=True)
            await asyncio.sleep(CHECK_INTERVAL)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Monitor loop crash")
            await asyncio.sleep(2)


# ============================================================
# MAIN
# ============================================================
async def main():
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    # Warm the real /Bakalavr pagination specification in the background so the
    # first user who presses "🏆 Reytingim" does not have to wait for Chromium discovery.
    asyncio.create_task(asyncio.to_thread(client._detect_bachelor_page_param))
    task = asyncio.create_task(monitor_loop(bot))
    try:
        log.info("Legalix Mandat Bot TOMORROW starting...")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
