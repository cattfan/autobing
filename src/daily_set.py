"""
Daily Set task automation for Microsoft Rewards.
Includes Quiz Auto-Answer (search Bing for answers) and Captcha solving.
"""

import asyncio
import json
import random

from playwright.async_api import Page

from src.utils import logger, REWARDS_URL, retry
from src.humanizer import Humanizer
from src.quiz_solver import auto_answer_quiz
from src.captcha_solver import CaptchaSolver


class DailySetCompleter:
    """Completes Daily Set tasks on Microsoft Rewards."""

    def __init__(self, humanizer: Humanizer, settings: dict = None, ai_agent=None):
        self.humanizer = humanizer
        self.ai_agent = ai_agent
        self.settings = settings or {}
        self.captcha = CaptchaSolver(self.settings)

    @staticmethod
    def _normalize_title(value: str) -> str:
        return " ".join((value or "").replace("\u200b", " ").replace("\xa0", " ").lower().split())

    @classmethod
    def _titles_match(cls, expected_title: str, observed_title: str) -> bool:
        expected = cls._normalize_title(expected_title)
        observed = cls._normalize_title(observed_title)
        if not expected or not observed:
            return False
        if expected in observed or observed in expected:
            return True

        expected_tokens = [token for token in expected.replace("?", " ").replace(":", " ").split() if len(token) >= 4]
        observed_tokens = set(token for token in observed.replace("?", " ").replace(":", " ").split() if len(token) >= 4)
        if not expected_tokens or not observed_tokens:
            return False

        overlap = sum(1 for token in expected_tokens if token in observed_tokens)
        return overlap >= max(1, min(2, len(expected_tokens)))

    async def _collect_daily_set_activity_targets(
        self,
        page: Page,
        *,
        expected_title: str = "",
        excluded_titles: set[str] | None = None,
    ) -> list[dict]:
        """Return clickable visible Daily Set activities, prioritizing the expected task."""
        expected_key = self._normalize_title(expected_title)
        excluded = sorted(
            self._normalize_title(title)
            for title in (excluded_titles or set())
            if (title or "").strip()
        )
        try:
            targets = await page.evaluate(
                """
                ({ expectedTitle, excludedTitles }) => {
                    const normalize = (value) => (value || "")
                        .replace(/\\u200b/g, "")
                        .replace(/\\u00a0/g, " ")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
                    const clickableSelector =
                        "a,button,[role='button'],[role='link'],[data-rac],.cursor-pointer,[tabindex]";
                    const cardSelector = [
                        "mee-rewards-daily-set-item-content",
                        "#daily-sets mee-card",
                        "#daily-sets [data-bi-id]",
                        "[data-bi-area*='DailySet']",
                        "[data-bi-id*='DailySet']",
                        "[data-bi-id*='dailyset']",
                        "[data-task-type]",
                        "[class*='daily-set']",
                        "[class*='dailySet']",
                        "[class*='daily']",
                    ].join(",");
                    const bannedTitles = new Set((excludedTitles || []).map(normalize).filter(Boolean));

                    for (const old of document.querySelectorAll("[data-codex-daily-activity='true']")) {
                        old.removeAttribute("data-codex-daily-activity");
                    }

                    const pickTitle = (node) => {
                        const rawLines = String(node.innerText || node.textContent || "")
                            .split(/\\n+/)
                            .map((line) => normalize(line))
                            .filter(Boolean);
                        for (const line of rawLines) {
                            if (
                                line.includes("daily set")
                                || line.includes("activity:")
                                || line === "completed"
                                || /^\\+?\\d+\\s*(point|points)?$/.test(line)
                            ) {
                                continue;
                            }
                            return line;
                        }
                        return rawLines[0] || "";
                    };

                    const results = [];
                    const pushCandidate = (candidate, title, fullText, baseScore = 0) => {
                        const hasExpectedMatch = expectedTitle && (title.includes(expectedTitle) || expectedTitle.includes(title) || fullText.includes(expectedTitle));
                        const hasDailyActionSignal = /\\+\\d+/.test(fullText) || fullText.includes("quiz") || fullText.includes("poll") || fullText.includes("search") || fullText.includes("activity");
                        if (!candidate || !title || bannedTitles.has(title) || (!hasExpectedMatch && !hasDailyActionSignal)) {
                            return;
                        }
                        if (!candidate.id) {
                            candidate.id = `codex-daily-activity-${Math.random().toString(36).slice(2, 10)}`;
                        }
                        candidate.setAttribute("data-codex-daily-activity", "true");

                        let score = baseScore;
                        if (expectedTitle) {
                            if (title.includes(expectedTitle) || expectedTitle.includes(title)) {
                                score += 1000;
                            }
                            if (fullText.includes(expectedTitle)) {
                                score += 400;
                            }
                        }
                        if (/\\+\\d+/.test(fullText)) {
                            score += 80;
                        }
                        score += Math.min(fullText.length, 160);
                        const href = candidate.href || candidate.getAttribute('href') || candidate.getAttribute('data-url') || '';
                        const dataBiId = candidate.getAttribute('data-bi-id') || '';
                        const tagName = candidate.tagName ? candidate.tagName.toLowerCase() : '';
                        const strategy = href ? 'href' : (dataBiId ? 'data-bi-id' : (candidate.getAttribute('role') || tagName || 'unknown'));
                        const identity = [title, href.split('?')[0], strategy].map(normalize).filter(Boolean).join(' | ');
                        results.push({ id: candidate.id, title, score, href, dataBiId, strategy, identity });
                    };

                    const modalRoots = Array.from(document.querySelectorAll(
                        "[role='dialog'],[aria-modal='true'],mee-modal,[class*='modal'],[class*='flyout'],[class*='popover']"
                    )).filter(root => {
                        const rect = root.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 20) return false;
                        const text = normalize(root.innerText || root.textContent || "");
                        return text.includes("daily") || text.includes("activity") || text.includes("+10") || text.includes("+5");
                    });
                    for (const root of modalRoots) {
                        for (const link of root.querySelectorAll(clickableSelector)) {
                            const rect = link.getBoundingClientRect();
                            if (rect.width < 10 || rect.height < 10) continue;
                            const style = window.getComputedStyle(link);
                            if (style.visibility === "hidden" || style.display === "none") continue;
                            const fullText = normalize(link.innerText || link.textContent || link.getAttribute("aria-label") || link.getAttribute("title") || "");
                            if (!fullText || fullText === "completed" || fullText === "close" || fullText.includes("daily set")) continue;
                            const title = pickTitle(link);
                            const lines = fullText.split(/\n+/).map(normalize).filter(Boolean);
                            const completed = lines.some(line => line === "completed" || line === "complete")
                                || fullText.includes("✓") || fullText.includes("✔");
                            if (completed) continue;
                            pushCandidate(link, title || fullText, fullText, 200);
                        }
                    }

                    for (const card of document.querySelectorAll(cardSelector)) {
                        const rect = card.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 20) {
                            continue;
                        }

                        const style = window.getComputedStyle(card);
                        if (style.visibility === "hidden" || style.display === "none") {
                            continue;
                        }

                        const fullText = normalize(card.innerText || card.textContent || "");
                        if (!fullText) {
                            continue;
                        }

                        const title = pickTitle(card);
                        if (!title || bannedTitles.has(title)) {
                            continue;
                        }

                        const ownText = normalize(card.innerText || card.textContent || "");
                        const textLines = ownText.split(/\n+/).map(normalize).filter(Boolean);
                        const completed =
                            textLines.some(line => line === "completed" || line === "complete")
                            || fullText.includes("✓")
                            || fullText.includes("✔")
                            || Boolean(
                                card.querySelector(
                                    "[class*='complete'],[class*='check'],[class*='done'],[aria-label*='complete']"
                                )
                            );
                        if (completed) {
                            continue;
                        }

                        let candidate =
                            card.closest(clickableSelector)
                            || card.querySelector(clickableSelector)
                            || card;
                        pushCandidate(candidate, title, fullText, 0);
                    }

                    results.sort((a, b) => b.score - a.score || a.title.localeCompare(b.title));
                    return results;
                }
                """,
                {
                    "expectedTitle": expected_key,
                    "excludedTitles": excluded,
                },
            )
        except Exception:
            return []

        normalized_seen: set[str] = set()
        deduped: list[dict] = []
        for target in targets or []:
            title = (target.get("title") or "").strip()
            key = self._normalize_title(title)
            if not title or key in normalized_seen:
                continue
            normalized_seen.add(key)
            deduped.append({
                "id": target.get("id", ""),
                "title": title,
                "href": target.get("href", ""),
                "data_bi_id": target.get("dataBiId", ""),
                "selector_strategy": target.get("strategy", ""),
                "identity": target.get("identity", ""),
            })
        return deduped

    async def locate_daily_set_surface_task(self, page: Page, expected_title: str) -> dict | None:
        expected_key = self._normalize_title(expected_title)
        if not expected_key:
            return None
        try:
            located = await page.evaluate(
                """
                ({ expectedTitle }) => {
                    const normalize = (value) => (value || "")
                        .replace(/​/g, "")
                        .replace(/ /g, " ")
                        .replace(/\\s+/g, " ")
                        .trim()
                        .toLowerCase();
                    const titleMatches = (title, expected) => {
                        if (!title || !expected) return false;
                        if (title.includes(expected) || expected.includes(title)) return true;
                        const expectedTokens = expected.replace(/[?:]/g, ' ').split(' ').filter(token => token.length >= 4);
                        const titleTokens = new Set(title.replace(/[?:]/g, ' ').split(' ').filter(token => token.length >= 4));
                        let overlap = 0;
                        for (const token of expectedTokens) {
                            if (titleTokens.has(token)) overlap += 1;
                        }
                        return overlap >= Math.max(1, Math.min(2, expectedTokens.length));
                    };

                    for (const old of document.querySelectorAll('[data-codex-daily-surface="true"]')) {
                        old.removeAttribute('data-codex-daily-surface');
                    }

                    const selectors = [
                        '#daily-sets mee-card',
                        '#daily-sets [data-bi-id]',
                        '[data-bi-area*="DailySet"]',
                        '[data-bi-id*="DailySet"]',
                        '[data-bi-id*="dailyset"]',
                        'mee-card',
                        'a',
                        'button',
                        '[role="button"]',
                        '[role="link"]'
                    ].join(',');

                    const results = [];
                    for (const node of document.querySelectorAll(selectors)) {
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 20) continue;
                        const style = window.getComputedStyle(node);
                        if (style.visibility === 'hidden' || style.display === 'none') continue;
                        const text = normalize(node.innerText || node.textContent || '');
                        if (!text) continue;
                        if (!titleMatches(text, expectedTitle)) continue;
                        const clickable = node.closest('a,button,[role="button"],[role="link"],[tabindex]') || node;
                        if (!clickable.id) {
                            clickable.id = `codex-daily-surface-${Math.random().toString(36).slice(2, 10)}`;
                        }
                        clickable.setAttribute('data-codex-daily-surface', 'true');
                        const href = clickable.href || clickable.getAttribute('href') || clickable.getAttribute('data-url') || '';
                        results.push({ id: clickable.id, title: text, href });
                    }
                    return results[0] || null;
                }
                """,
                {"expectedTitle": expected_key},
            )
        except Exception:
            return None
        return located or None

    async def click_daily_set_surface_task(self, page: Page, expected_title: str) -> bool:
        located = await self.locate_daily_set_surface_task(page, expected_title)
        if not located:
            return False
        try:
            target = page.locator(f"#{located.get('id', '')}").first
            if await target.count() == 0 or not await target.is_visible(timeout=2000):
                return False
            await target.scroll_into_view_if_needed(timeout=3000)
            await target.click(timeout=5000)
            logger.info(f"Clicked Daily Set surface task via title match: {expected_title}")
            return True
        except Exception:
            return False

    async def read_daily_set_surface_debug(self, page: Page) -> list[str]:
        try:
            observed = await page.evaluate(
                """
                () => {
                    const normalize = (value) => (value || '')
                        .replace(/​/g, '')
                        .replace(/ /g, ' ')
                        .replace(/\\s+/g, ' ')
                        .trim();
                    const results = [];
                    for (const node of document.querySelectorAll('mee-card, a, button, [role="button"], [role="link"], [data-bi-id], [class*="daily"]')) {
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 20) continue;
                        const style = window.getComputedStyle(node);
                        if (style.visibility === 'hidden' || style.display === 'none') continue;
                        const text = normalize(node.innerText || node.textContent || '');
                        if (!text) continue;
                        const lower = text.toLowerCase();
                        if (!lower.includes('daily') && !lower.includes('quiz') && !lower.includes('poll') && !lower.includes('this or that')) continue;
                        results.push(text.slice(0, 180));
                    }
                    return results.slice(0, 12);
                }
                """
            )
        except Exception:
            return []
        return observed or []

    async def attempt_surface_fallback(self, page: Page, expected_title: str = "") -> dict:
        rewards_surfaces = [
            f"{REWARDS_URL}/earn",
            f"{REWARDS_URL}/dashboard",
            REWARDS_URL,
        ]
        debug_texts: list[str] = []
        for rewards_url in rewards_surfaces:
            try:
                await page.goto(rewards_url, wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(2)
            except Exception:
                continue
            debug_texts = await self.read_daily_set_surface_debug(page)
            if expected_title and await self.click_daily_set_surface_task(page, expected_title):
                await asyncio.sleep(3)
                await self.captcha.solve_if_present(page)
                await self._handle_task(page)
                return {"clicked": True, "debug_texts": debug_texts}
        return {"clicked": False, "debug_texts": debug_texts}


    async def locate_daily_set_surface_activities(self, page: Page, *, excluded_titles: set[str] | None = None) -> list[dict]:
        excluded = [self._normalize_title(title) for title in (excluded_titles or set()) if (title or "").strip()]
        try:
            located = await page.evaluate(
                """
                ({ excludedTitles }) => {
                    const normalize = (value) => (value || '')
                        .replace(/​/g, '')
                        .replace(/ /g, ' ')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .toLowerCase();
                    const excluded = new Set((excludedTitles || []).map(normalize).filter(Boolean));
                    const selectors = [
                        '#daily-sets mee-card',
                        '#daily-sets [data-bi-id]',
                        '[data-bi-area*=DailySet]',
                        '[data-bi-id*=DailySet]',
                        '[data-bi-id*=dailyset]',
                        'mee-rewards-daily-set-item-content',
                        'mee-card'
                    ].join(',');
                    const pickTitle = (node) => {
                        const lines = String(node.innerText || node.textContent || '')
                            .split(/\n+/)
                            .map(normalize)
                            .filter(Boolean);
                        for (const line of lines) {
                            if (line.includes('daily set') || line.includes('activity:') || line === 'completed' || /^\\+?\\d+\\s*(point|points)?$/.test(line)) continue;
                            return line;
                        }
                        return lines[0] || '';
                    };
                    const results = [];
                    for (const node of document.querySelectorAll(selectors)) {
                        const rect = node.getBoundingClientRect();
                        if (rect.width < 20 || rect.height < 20) continue;
                        const style = window.getComputedStyle(node);
                        if (style.visibility === 'hidden' || style.display === 'none') continue;
                        const text = normalize(node.innerText || node.textContent || '');
                        if (!text) continue;
                        if (!text.includes('daily') && !text.includes('quiz') && !text.includes('poll') && !text.includes('search') && !/\\+\\d+/.test(text)) continue;
                        if (text.includes('completed') || text.includes('complete')) continue;
                        const title = pickTitle(node);
                        if (!title || excluded.has(title)) continue;
                        const clickable = node.closest('a,button,[role="button"],[role="link"],[tabindex]') || node.querySelector('a,button,[role="button"],[role="link"],[tabindex]') || node;
                        if (!clickable.id) clickable.id = `codex-daily-surface-any-${Math.random().toString(36).slice(2, 10)}`;
                        clickable.setAttribute('data-codex-daily-surface-any', 'true');
                        const href = clickable.href || clickable.getAttribute('href') || clickable.getAttribute('data-url') || '';
                        let score = 0;
                        if (/\\+\\d+/.test(text)) score += 80;
                        if (text.includes('quiz') || text.includes('poll') || text.includes('search')) score += 40;
                        score += Math.min(text.length, 160);
                        results.push({ id: clickable.id, title, href, score });
                    }
                    results.sort((a, b) => b.score - a.score || a.title.localeCompare(b.title));
                    return results.slice(0, 5);
                }
                """,
                {"excludedTitles": excluded},
            )
        except Exception:
            return []
        return located or []

    async def click_next_daily_set_surface_activity(self, page: Page, *, excluded_titles: set[str] | None = None) -> dict | None:
        for activity in await self.locate_daily_set_surface_activities(page, excluded_titles=excluded_titles):
            try:
                target = page.locator(f"#{activity.get('id', '')}").first
                if await target.count() == 0 or not await target.is_visible(timeout=2000):
                    continue
                await target.scroll_into_view_if_needed(timeout=3000)
                await target.click(timeout=5000)
                logger.info(f"Clicked Daily Set surface activity: {activity.get('title', '')}")
                return activity
            except Exception:
                continue
        return None

    async def extract_hidden_daily_set_urls(self, page: Page) -> list[dict]:
        try:
            found = await page.evaluate(
                r"""
                () => {
                    const normalize = (value) => String(value || '')
                        .replace(/​/g, '')
                        .replace(/ /g, ' ')
                        .replace(/\s+/g, ' ')
                        .trim();
                    const results = [];
                    const seen = new Set();
                    const push = (url, title, source) => {
                        url = normalize(url);
                        title = normalize(title) || 'Hidden Daily Set';
                        if (!url || seen.has(url)) return;
                        const lower = (url + ' ' + title).toLowerCase();
                        const dailyish = lower.includes('daily')
                            || lower.includes('dset')
                            || lower.includes('dsetqu')
                            || lower.includes('rewardsquiz_dailyset')
                            || lower.includes('wqoskey')
                            || lower.includes('btepokey');
                        const actionable = lower.includes('bing.com/search')
                            || lower.includes('rewards.bing.com')
                            || lower.includes('spotlight')
                            || lower.includes('form=dset')
                            || lower.includes('form=ml');
                        if (!dailyish || !actionable) return;
                        seen.add(url);
                        results.push({ title, destination_url: url, source });
                    };

                    for (const node of document.querySelectorAll('a[href], [data-url], [data-destination-url], [data-bi-destinationurl]')) {
                        const url = node.href
                            || node.getAttribute('href')
                            || node.getAttribute('data-url')
                            || node.getAttribute('data-destination-url')
                            || node.getAttribute('data-bi-destinationurl')
                            || '';
                        push(url, node.innerText || node.textContent || node.getAttribute('aria-label') || '', 'dom');
                    }

                    const blobs = [];
                    for (const script of document.querySelectorAll('script')) {
                        const text = script.textContent || '';
                        if (text.includes('daily') || text.includes('Daily') || text.includes('dset') || text.includes('DSET')) {
                            blobs.push(text);
                        }
                    }
                    blobs.push(document.documentElement.innerHTML || '');

                    const urlPattern = /https?:\/\/[^\s"'<>\\]+/g;
                    for (const blob of blobs) {
                        for (const match of blob.matchAll(urlPattern)) {
                            let url = match[0]
                                .replace(/\u0026/g, '&')
                                .replace(/&amp;/g, '&')
                                .replace(/\\\//g, '/')
                                .replace(/[),.;]+$/g, '');
                            const context = blob.slice(Math.max(0, match.index - 300), Math.min(blob.length, match.index + url.length + 300));
                            push(url, context, 'script');
                        }
                    }

                    try {
                        for (const entry of performance.getEntriesByType('resource')) {
                            push(entry.name, entry.name, 'performance');
                        }
                    } catch (_) {}

                    return results.slice(0, 10);
                }
                """
            )
        except Exception as exc:
            logger.debug(f"Hidden Daily Set URL extraction failed: {exc}")
            return []
        return found or []

    async def log_daily_set_empty_modal_diagnostics(self, page: Page, *, progress: dict | None = None) -> None:
        if not self.settings.get("diagnostic_logging", True):
            return
        try:
            payload = await page.evaluate(
                r"""
                () => {
                    const normalize = (value) => String(value || '')
                        .replace(/​/g, ' ')
                        .replace(/ /g, ' ')
                        .replace(/\s+/g, ' ')
                        .trim();
                    const roots = Array.from(document.querySelectorAll(
                        "[role='dialog'],[aria-modal='true'],mee-modal,[class*='modal'],[class*='flyout'],[class*='popover']"
                    ));
                    const modalTexts = roots.map((root) => normalize(root.innerText || root.textContent || '')).filter(Boolean).slice(0, 5);
                    const links = Array.from(document.querySelectorAll('a[href], [data-url], [data-destination-url], [data-bi-destinationurl]')).map((node) => ({
                        text: normalize(node.innerText || node.textContent || node.getAttribute('aria-label') || ''),
                        href: normalize(node.href || node.getAttribute('href') || node.getAttribute('data-url') || node.getAttribute('data-destination-url') || node.getAttribute('data-bi-destinationurl') || ''),
                    })).filter((item) => {
                        const lower = (item.text + ' ' + item.href).toLowerCase();
                        return lower.includes('daily') || lower.includes('dset') || lower.includes('quiz') || lower.includes('poll');
                    }).slice(0, 20);
                    return { url: location.href, modalTexts, links };
                }
                """
            )
        except Exception as exc:
            logger.debug(f"Daily Set empty modal diagnostics failed: {exc}")
            return
        logger.info(
            "[diag][daily-set] Empty Daily Set modal diagnostics | "
            f"progress={json.dumps(progress or {}, ensure_ascii=False, sort_keys=True)} | "
            f"payload={json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)[:4000]}"
        )

    async def try_direct_daily_set_url(self, page: Page, destination_url: str, title: str = "") -> dict:
        result = {"attempted": False, "progress_completed": 0, "progress_total": 0, "category_proven": False}
        destination_url = (destination_url or "").strip()
        if not destination_url:
            return result

        try:
            await page.goto(destination_url, wait_until="domcontentloaded", timeout=35000)
            result["attempted"] = True
            await asyncio.sleep(3)
            await self.captcha.solve_if_present(page)
            await self._handle_task(page)
            await asyncio.sleep(2)
            progress = await self._read_daily_set_progress(page)
            result["progress_completed"] = int(progress.get("completed", 0) or 0)
            result["progress_total"] = int(progress.get("total", 0) or 0)
            result["category_proven"] = bool(progress.get("category_proven", False)) or (
                result["progress_total"] > 0
                and result["progress_completed"] >= result["progress_total"]
            )
            logger.info(f"Tried Daily Set direct URL recovery: {title or destination_url}")
        except Exception as exc:
            logger.debug(f"Daily Set direct URL recovery failed: {exc}")
        return result

    async def _read_daily_set_progress(self, page: Page) -> dict:
        """Read the visible Daily Set progress summary from Rewards surfaces."""
        proof = {
            "completed": 0,
            "total": 0,
            "category_proven": False,
            "signals": [],
        }
        if not hasattr(page, "goto"):
            return proof

        rewards_surfaces = [
            REWARDS_URL,
            f"{REWARDS_URL}/dashboard",
            f"{REWARDS_URL}/earn",
        ]

        for rewards_url in rewards_surfaces:
            try:
                await page.goto(
                    rewards_url,
                    wait_until="domcontentloaded",
                    timeout=35000,
                )
                await asyncio.sleep(2)
            except Exception:
                continue

            try:
                observed = await page.evaluate(
                    """
                    () => {
                        const normalize = (value) => (value || "")
                            .replace(/\\u200b/g, "")
                            .replace(/\\u00a0/g, " ")
                            .replace(/\\s+/g, " ")
                            .trim();

                        const signals = [];
                        const nodes = document.querySelectorAll("a,button,div,span,p,mee-card");
                        for (const node of nodes) {
                            const rect = node.getBoundingClientRect();
                            if (rect.width < 4 || rect.height < 4) {
                                continue;
                            }

                            const style = window.getComputedStyle(node);
                            if (style.visibility === "hidden" || style.display === "none") {
                                continue;
                            }

                            const text = normalize(node.innerText || node.textContent || "");
                            if (!text) {
                                continue;
                            }

                            const lower = text.toLowerCase();
                            if (!lower.includes("daily set") && !lower.includes("activity")) {
                                continue;
                            }
                            signals.push(text);
                        }

                        let completed = 0;
                        let total = 0;
                        for (const text of signals) {
                            const match = text.match(/activity\\s*:?\\s*(\\d+)\\s*\\/\\s*(\\d+)/i)
                                || text.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
                            if (!match) {
                                continue;
                            }
                            const current = Number(match[1] || 0);
                            const maximum = Number(match[2] || 0);
                            if (maximum <= 0 || maximum > 7 || current < 0 || current > maximum) {
                                continue;
                            }
                            if (maximum > total || (maximum === total && current > completed)) {
                                completed = current;
                                total = maximum;
                            }
                        }

                        return {
                            completed,
                            total,
                            signals: signals.slice(0, 6),
                        };
                    }
                    """
                )
            except Exception:
                continue

            observed_completed = int(observed.get("completed", 0) or 0)
            observed_total = int(observed.get("total", 0) or 0)
            if observed_total <= 0 or observed_total > 7 or observed_completed < 0 or observed_completed > observed_total:
                continue
            proof["completed"] = max(proof["completed"], observed_completed)
            proof["total"] = max(proof["total"], observed_total)
            proof["signals"] = observed.get("signals", []) or proof["signals"]
            if proof["total"] > 0 and proof["completed"] >= proof["total"]:
                proof["category_proven"] = True
                break

        return proof

    async def _click_daily_set_card(self, page: Page) -> bool:
        """Open the Daily Set container using broad selectors for the current UI."""
        streak_selectors = [
            '#daily-sets mee-card-group mee-card:first-child a',
            '#daily-sets mee-card:first-child a',
            '#daily-sets a',
            '#daily-sets [role="link"]',
            '#daily-sets [role="button"]',
            '#daily-sets button',
            'mee-card:has-text("Daily Set Streak") a',
            'a:has-text("Daily Set Streak")',
            'a:has-text("Daily Set")',
            'mee-card:has-text("Activity") a',
            'mee-card:has-text("Activity") button',
            'mee-card:has-text("Activity") [role="button"]',
            '[data-bi-area="DailySet"] a',
            '[data-bi-name="promotion_item"]:has-text("Daily Set")',
            'mee-rewards-daily-set-item-content:first-of-type a',
        ]

        for sel in streak_selectors:
            try:
                el = page.locator(sel).first
                if await el.count() == 0 or not await el.is_visible(timeout=2000):
                    continue
                await el.scroll_into_view_if_needed(timeout=3000)
                await el.click(timeout=5000)
                logger.info(f"Clicked Daily Set Streak card via: {sel}")
                return True
            except Exception:
                continue

        dom_target_id = await page.evaluate(
            """
            () => {
                const normalize = (value) => (value || "")
                    .replace(/\\u200b/g, "")
                    .replace(/\\u00a0/g, " ")
                    .replace(/\\s+/g, " ")
                    .trim()
                    .toLowerCase();
                const selector = "a,button,[role='button'],[role='link'],[data-rac],.cursor-pointer,div";

                for (const old of document.querySelectorAll("[data-codex-daily='true']")) {
                    old.removeAttribute("data-codex-daily");
                    if ((old.id || "").startsWith("codex-daily-")) {
                        old.removeAttribute("id");
                    }
                }

                for (const node of document.querySelectorAll(selector)) {
                    const rect = node.getBoundingClientRect();
                    if (rect.width < 4 || rect.height < 4) {
                        continue;
                    }

                    const style = window.getComputedStyle(node);
                    if (style.visibility === "hidden" || style.display === "none") {
                        continue;
                    }

                    const text = normalize(node.innerText || node.textContent || "");
                    if (!text) {
                        continue;
                    }

                    if (!text.includes("daily set") && !text.includes("activity:")) {
                        continue;
                    }

                    const clickable =
                        node.closest("a,button,[role='button'],[role='link']") ||
                        node.querySelector("a,button,[role='button'],[role='link']") ||
                        node;

                    if (!clickable.id) {
                        clickable.id = `codex-daily-${Math.random().toString(36).slice(2, 10)}`;
                    }
                    clickable.setAttribute("data-codex-daily", "true");
                    return clickable.id;
                }

                return null;
            }
            """
        )

        if dom_target_id:
            try:
                target = page.locator(f"#{dom_target_id}").first
                if await target.count() > 0 and await target.is_visible(timeout=2000):
                    await target.scroll_into_view_if_needed(timeout=3000)
                    await target.click(timeout=5000)
                    logger.info("Clicked Daily Set card via DOM text match")
                    return True
            except Exception:
                pass

        return False

    @retry(max_retries=2, delay=3)
    async def complete_daily_set(self, page: Page, expected_title: str = "") -> dict:
        """
        Complete all Daily Set tasks.

        Returns:
            Dict with {completed, total, tasks}
        """
        logger.info("Starting Daily Set completion...")

        stats = {
            "completed": 0,
            "total": 0,
            "tasks": [],
            "state": "attempted_only",
            "attempted": False,
            "target_status": "not_proven",
            "target_proven": False,
            "category_proven": False,
            "attempted_only": False,
            "panel_control_failed": False,
            "proof_titles": [],
            "progress_completed": 0,
            "progress_total": 0,
            "source": "daily_set_completer",
        }
        panel_control_failed = False

        try:
            # ═══ Click-based approach using correct Rewards page DOM ═══
            streak_card_clicked = False
            rewards_surfaces = [
                REWARDS_URL,
                f"{REWARDS_URL}/dashboard",
                f"{REWARDS_URL}/earn",
            ]

            for rewards_url in rewards_surfaces:
                try:
                    await page.goto(
                        rewards_url,
                        wait_until="domcontentloaded",
                        timeout=35000,
                    )
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    await asyncio.sleep(3)
                else:
                    await asyncio.sleep(2)

                streak_card_clicked = await self._click_daily_set_card(page)
                if streak_card_clicked:
                    break

            if not streak_card_clicked:
                logger.warning("Could not find Daily Set Streak card on #daily-sets")
                stats["panel_control_failed"] = True
                raise RuntimeError("daily_set_panel_not_found")

            # Step 2: Wait for modal/flyout to open
            await asyncio.sleep(3)

            attempted_titles: set[str] = set()
            empty_modal_retries = 0
            max_empty_modal_retries = 2

            while True:
                if stats["attempted"]:
                    progress_proof = await self._read_daily_set_progress(page)
                    stats["progress_completed"] = max(stats["completed"], int(progress_proof.get("completed", 0)))
                    stats["progress_total"] = max(stats["total"], int(progress_proof.get("total", 0)))
                    if progress_proof.get("category_proven", False) or (
                        stats["progress_total"] > 0 and stats["progress_completed"] >= stats["progress_total"]
                    ):
                        stats["category_proven"] = True
                        logger.info("Daily Set progress proof reached full completion")
                        break

                targets = await self._collect_daily_set_activity_targets(
                    page,
                    expected_title=expected_title,
                    excluded_titles=attempted_titles,
                )
                if not targets:
                    if not stats["attempted"]:
                        logger.warning("No incomplete Daily Set activities found in modal")
                        await self.log_daily_set_empty_modal_diagnostics(
                            page,
                            progress={
                                "completed": stats.get("progress_completed", 0),
                                "total": stats.get("progress_total", 0),
                                "retry": empty_modal_retries,
                            },
                        )
                        if empty_modal_retries < max_empty_modal_retries:
                            empty_modal_retries += 1
                            logger.info(
                                f"Retrying Daily Set modal recovery ({empty_modal_retries}/{max_empty_modal_retries})"
                            )
                            reopened = False
                            for rewards_url in rewards_surfaces:
                                try:
                                    await page.goto(rewards_url, wait_until="domcontentloaded", timeout=35000)
                                    await asyncio.sleep(2)
                                    if await self._click_daily_set_card(page):
                                        await asyncio.sleep(3)
                                        reopened = True
                                        break
                                except Exception:
                                    continue
                            if reopened:
                                continue
                    if stats["attempted"]:
                        break
                    surface_progress = await self._read_daily_set_progress(page)
                    if (
                        int(surface_progress.get("total", 0) or 0) > 0
                        and int(surface_progress.get("completed", 0) or 0) < int(surface_progress.get("total", 0) or 0)
                    ):
                        clicked_surface = await self.click_next_daily_set_surface_activity(
                            page,
                            excluded_titles=attempted_titles,
                        )
                        if clicked_surface:
                            title = (clicked_surface.get("title") or "").strip()
                            attempted_titles.add(title)
                            stats["attempted"] = True
                            await asyncio.sleep(3)
                            await self.captcha.solve_if_present(page)
                            await self._handle_task(page)
                            if title:
                                stats["proof_titles"].append(title)
                            continue
                    break

                empty_modal_retries = 0

                stats["total"] = max(stats["total"], len(targets) + len(attempted_titles))
                if expected_title and not any(self._titles_match(expected_title, t.get("title", "")) for t in targets):
                    logger.info("Expected Daily Set title no longer present; continuing with strongest remaining target")
                target = targets[0]
                title = (target.get("title") or "").strip()
                target_id = target.get("id", "")
                attempted_titles.add(title)

                try:
                    logger.info(f"Daily Set {stats['completed'] + 1}/{stats['total']}: {title}")
                    logger.debug(
                        "Daily Set target metadata: strategy=%s href=%s identity=%s",
                        target.get("selector_strategy", ""),
                        target.get("href", ""),
                        target.get("identity", ""),
                    )

                    # Remember current pages before click

                    # Remember current pages before click
                    pages_before = len(page.context.pages)
                    stats["attempted"] = True

                    link = page.locator(f"#{target_id}").first
                    if await link.count() == 0 or not await link.is_visible(timeout=2000):
                        raise RuntimeError(f"daily_set_activity_target_missing:{title or 'unknown'}")
                    await link.scroll_into_view_if_needed(timeout=3000)
                    await link.click(timeout=5000)
                    await asyncio.sleep(3)

                    # Check if a new tab opened
                    current_pages = page.context.pages
                    if len(current_pages) > pages_before:
                        # Switch to new tab
                        new_tab = current_pages[-1]
                        await new_tab.wait_for_load_state("domcontentloaded", timeout=35000)
                        await asyncio.sleep(2)

                        # Handle the task in the new tab
                        await self.captcha.solve_if_present(new_tab)
                        await self._handle_task(new_tab)

                        # Close the tab and go back to modal
                        await new_tab.close()
                        await asyncio.sleep(1)
                        await page.bring_to_front()
                    else:
                        # Task opened in same page
                        await self.captcha.solve_if_present(page)
                        await self._handle_task(page)

                        # Go back to a rewards surface and reopen modal with fresh DOM
                        reopened = False
                        for rewards_url in rewards_surfaces:
                            try:
                                await page.goto(rewards_url, wait_until="domcontentloaded", timeout=35000)
                                await asyncio.sleep(2)
                                if await self._click_daily_set_card(page):
                                    await asyncio.sleep(3)
                                    reopened = True
                                    break
                            except Exception:
                                continue
                        if not reopened:
                            logger.warning("Could not reopen Daily Set panel after same-page task")

                    progress_before = int(stats.get("progress_completed", 0) or 0)
                    progress_proof = await self._read_daily_set_progress(page)
                    observed_completed = int(progress_proof.get("completed", 0) or 0)
                    observed_total = int(progress_proof.get("total", 0) or 0)
                    progress_advanced = observed_completed > progress_before
                    target_proven = bool(progress_advanced or progress_proof.get("category_proven", False))

                    stats["progress_completed"] = max(progress_before, observed_completed)
                    stats["progress_total"] = max(stats["total"], observed_total)

                    if target_proven:
                        stats["completed"] = max(stats["completed"], stats["progress_completed"])
                        stats["tasks"].append({"index": stats["completed"], "status": "completed"})
                        if title:
                            stats["proof_titles"].append(title)
                        if expected_title:
                            if self._titles_match(expected_title, title):
                                stats["target_status"] = "proven"
                        elif stats["completed"] > 0:
                            stats["target_status"] = "proven"
                    else:
                        stats["tasks"].append({"index": len(stats["tasks"]) + 1, "status": "attempted_no_proof"})
                        logger.warning(f"Daily Set activity attempted but progress did not advance: {title}")

                    if progress_proof.get("category_proven", False) or (
                        stats["progress_total"] > 0 and stats["progress_completed"] >= stats["progress_total"]
                    ):
                        stats["category_proven"] = True
                        logger.info("Daily Set progress proof reached full completion")
                        break
                    logger.info(f"Daily Set activity processed: {title}")
                    await self.humanizer.short_delay()

                except Exception as e:
                    logger.warning(f"Daily Set activity failed: {title or 'unknown'}: {e}")
                    stats["tasks"].append({"index": len(stats['tasks']) + 1, "status": f"failed: {e}"})

                    # Try to recover: go back to rewards and reopen modal
                    try:
                        # Close any extra tabs
                        while len(page.context.pages) > 1:
                            extra = page.context.pages[-1]
                            if extra != page:
                                await extra.close()
                            else:
                                break
                        await page.goto(f"{REWARDS_URL}/earn", wait_until="domcontentloaded", timeout=35000)
                        await asyncio.sleep(3)
                        if await self._click_daily_set_card(page):
                            await asyncio.sleep(3)
                    except Exception:
                        pass

        except Exception as e:
            message = str(e)
            if "Target page, context or browser has been closed" in message:
                stats["page_closed"] = True
                logger.debug(f"Daily Set completion stopped because page was already closed: {e}")
            else:
                logger.error(f"Daily Set completion error: {e}")
            if self.ai_agent and self.ai_agent.enabled:
                logger.info("🤖 Falling back to AI Agent for Daily Set...")
                try:
                    ai_result = await self.ai_agent.complete_daily_set(page)
                except Exception as ai_error:
                    logger.debug(f"AI Daily Set fallback failed: {ai_error}")
                else:
                    if ai_result.get("success"):
                        steps = int(ai_result.get("steps", 0) or 0)
                        stats["attempted"] = steps > 0
                        stats["completed"] = max(stats["completed"], steps)
                        stats["total"] = max(stats["total"], steps)
                        stats["tasks"].append({"status": "ai_completed"})
                        if steps > 0 and not expected_title:
                            stats["target_status"] = "proven"

        progress_proof = await self._read_daily_set_progress(page)
        stats["progress_completed"] = max(
            stats["completed"],
            int(progress_proof.get("completed", 0)),
        )
        stats["progress_total"] = max(
            stats["total"],
            int(progress_proof.get("total", 0)),
        )
        if not expected_title and (stats["completed"] > 0 or progress_proof.get("category_proven", False)):
            stats["target_status"] = "proven"
        stats["target_proven"] = stats["target_status"] == "proven"
        stats["category_proven"] = bool(progress_proof.get("category_proven", False)) or (
            stats["progress_total"] > 0 and stats["progress_completed"] >= stats["progress_total"]
        )
        if stats["category_proven"]:
            stats["state"] = "category_proven"
        elif stats["target_proven"]:
            stats["state"] = "target_proven"
        elif stats["attempted"]:
            stats["state"] = "attempted_only"
        else:
            stats["state"] = "panel_control_failed"
            stats["panel_control_failed"] = True
        stats["attempted_only"] = stats["state"] == "attempted_only"
        stats["panel_control_failed"] = stats["state"] == "panel_control_failed"
        logger.info(f"Daily Set: {stats['completed']}/{stats['total']} completed")
        return stats

    async def _handle_task(self, page: Page) -> None:
        """Handle an opened Daily Set task (quiz, poll, link visit)."""
        await asyncio.sleep(3)

        # Check if it's a quiz
        quiz_container = await page.query_selector(
            '#quizComplete498498498, .rqQuestion, [id*="rqQuestionState"], '
            '#currentQuestionContainer'
        )

        if quiz_container:
            await self._handle_quiz(page)
            return

        # Check if it's a poll
        poll_container = await page.query_selector(
            '.bt_poll, #btoption, [class*="poll"], [data-bi-type="poll"]'
        )

        if poll_container:
            await self._handle_poll(page)
            return

        # Check if it's a "This or That" quiz
        tot_container = await page.query_selector(
            '#currentQuestionContainer .btOptions, '
            '.rq_tooltip, [class*="thisOrThat"]'
        )

        if tot_container:
            await self._handle_this_or_that(page)
            return

        # Default: it's just a link visit task - wait and go back
        await self.humanizer.simulate_reading(page, random.uniform(3, 6))

    async def _handle_quiz(self, page: Page) -> None:
        """Handle a quiz task with auto-answer via Bing search."""
        logger.debug("Handling quiz task (auto-answer enabled)...")
        max_questions = 20

        for q in range(max_questions):
            try:
                # Wait for question
                await asyncio.sleep(2)

                # Check if quiz is complete
                complete = await page.query_selector(
                    '#quizCompleteContainer, [class*="quizComplete"], '
                    '.cico.rqDone'
                )
                if complete:
                    logger.debug("Quiz completed!")
                    break

                # Read the question text
                question_text = ""
                q_el = await page.query_selector(
                    '.rqQuestion .textContainer, '
                    '#currentQuestionContainer .rqQuestion, '
                    '.wk_questionText, .rq_questionText'
                )
                if q_el:
                    question_text = (await q_el.text_content() or "").strip()

                # Find answer options
                options = page.locator(
                    '#currentQuestionContainer .rqOption, '
                    '.wk_choicesInst498498498 .rq_button, '
                    '[id*="rqAnswerOption"]'
                )

                count = await options.count()
                if count == 0:
                    break

                # Get option texts for auto-answer
                option_texts = []
                for i in range(count):
                    text = (await options.nth(i).text_content() or "").strip()
                    # Also try data attributes
                    if not text:
                        text = await options.nth(i).get_attribute("data-option") or ""
                    option_texts.append(text)

                # Auto-answer: search Bing for the answer
                if question_text and any(option_texts):
                    idx = await auto_answer_quiz(page, question_text, option_texts)
                else:
                    idx = random.randint(0, count - 1)
                    logger.debug(f"No question text, random pick: {idx}")

                await options.nth(idx).click()
                await asyncio.sleep(2)

                # Check for "next" button
                next_btn = page.locator(
                    '#nextQuestionbtn, [class*="nextQuestion"], '
                    'input[value="Next"]'
                )
                if await next_btn.count() > 0:
                    await next_btn.click()
                    await asyncio.sleep(1)

            except Exception as e:
                logger.debug(f"Quiz question {q + 1} error: {e}")
                break

    async def _handle_poll(self, page: Page) -> None:
        """Handle a poll task."""
        logger.debug("Handling poll task...")
        try:
            options = page.locator(
                '#btoption0, #btoption1, .bt_poll .btOption, '
                '[id*="btoption"]'
            )
            count = await options.count()
            if count > 0:
                idx = random.randint(0, count - 1)
                await options.nth(idx).click()
                await asyncio.sleep(3)
                logger.debug("Poll answered")
        except Exception as e:
            logger.debug(f"Poll error: {e}")

    async def _handle_this_or_that(self, page: Page) -> None:
        """Handle a 'This or That' quiz with auto-answer."""
        logger.debug("Handling This or That quiz (auto-answer)...")
        max_rounds = 15

        for r in range(max_rounds):
            try:
                await asyncio.sleep(2)

                # Check if complete
                complete = await page.query_selector(
                    '#quizCompleteContainer, [class*="quizComplete"]'
                )
                if complete:
                    break

                # Read the question/prompt
                question_text = ""
                q_el = await page.query_selector(
                    '.rqQuestion, .btQuestionText, '
                    '#currentQuestionContainer .textContainer'
                )
                if q_el:
                    question_text = (await q_el.text_content() or "").strip()

                # Find the two options
                options = page.locator(
                    '#currentQuestionContainer .btOptionCard, '
                    '.rq_tooltip .btOption, [class*="btOption"]'
                )
                count = await options.count()
                if count >= 2:
                    # Get option texts
                    option_texts = []
                    for i in range(count):
                        text = (await options.nth(i).text_content() or "").strip()
                        option_texts.append(text)

                    # Auto-answer
                    if question_text and any(option_texts):
                        idx = await auto_answer_quiz(page, question_text, option_texts)
                    else:
                        idx = random.randint(0, min(1, count - 1))

                    await options.nth(idx).click()
                    await asyncio.sleep(2)
                elif count == 0:
                    break

            except Exception as e:
                logger.debug(f"This or That round {r + 1} error: {e}")
                break
