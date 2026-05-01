import hashlib
import re
from typing import Any, TypedDict, List, Optional

from playwright.async_api import Page


class DashboardTask(TypedDict):
    title: str
    description: str
    points: int
    url: str
    element_index: int
    category: str
    is_quiz: bool
    semantic_id: str
    section_heading: str
    aria_label: str
    element_title: str
    data_bi_id: str
    tag_name: str
    role_hint: str
    text_content: str
    match_key: str
    scan_version: str
    fingerprint: str
    selector_strategy: str
    selector: str
    dom_shape: dict


NON_ACTIONABLE_CARD_TITLES = {
    "bing search streak",
    "bing app streak",
    "edge browsing streak",
    "daily set streak",
    "your points history",
}

SCAN_VERSION = "dashboard-dom-v2"


def _normalize_key(*parts: str) -> str:
    text = " ".join(str(part or "").strip().lower() for part in parts if str(part or "").strip())
    text = re.sub(r"\s+", " ", text)
    return text[:240]


def _css_string(value: str) -> str:
    return str(value or "").replace("\\", "\\\\").replace('"', '\\"')


def build_task_fingerprint(title: str, href: str, points: int, category: str, section: str = "") -> str:
    stable_key = _normalize_key(title, href.split("?")[0] if href else "", str(points or 0), category, section)
    return hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:16] if stable_key else ""


def _selector_for_task(href: str, data_bi_id: str, aria_label: str, element_title: str, tag_name: str, role_hint: str) -> tuple[str, str]:
    if href:
        return "href", f'a[href="{_css_string(href)}"]'
    if data_bi_id:
        return "data-bi-id", f'[data-bi-id="{_css_string(data_bi_id)}"]'
    if aria_label:
        return "aria", f'[aria-label="{_css_string(aria_label)}"]'
    if element_title:
        return "title", f'[title="{_css_string(element_title)}"]'
    if tag_name == "mee-card":
        return "mee-card", "mee-card"
    if role_hint:
        return "role", f'[role="{_css_string(role_hint)}"]'
    if tag_name:
        return tag_name, tag_name
    return "broad", ""


def summarize_selector_health(tasks: List[dict]) -> dict:
    strategies: dict[str, int] = {}
    stable = 0
    fallback_only = 0
    missing_fingerprint = 0
    for task in tasks or []:
        strategy = str(task.get("selector_strategy") or "unknown")
        strategies[strategy] = strategies.get(strategy, 0) + 1
        if strategy in {"href", "data-bi-id", "aria", "title"}:
            stable += 1
        if strategy in {"broad", "index_fallback", "unknown"}:
            fallback_only += 1
        if not task.get("fingerprint"):
            missing_fingerprint += 1
    return {
        "task_count": len(tasks or []),
        "stable_selector_count": stable,
        "fallback_selector_count": fallback_only,
        "missing_fingerprint_count": missing_fingerprint,
        "strategy_counts": strategies,
        "scan_version": SCAN_VERSION,
    }


def _infer_dashboard_category(section_heading: str, title: str, description: str, href: str) -> str:
    section = (section_heading or "").strip().lower()
    title_lower = (title or "").strip().lower()
    desc_lower = (description or "").strip().lower()
    href_lower = (href or "").strip().lower()
    haystack = " ".join([title_lower, desc_lower, href_lower]).lower()

    if any(token in href_lower for token in ("referandearn", "form=tgrew")):
        return "more_promo"
    if any(token in title_lower for token in ("turn referrals into rewards", "order history", "streak bonus")):
        return "more_promo"
    if any(token in section for token in ("keep earning", "earn more", "more activities", "more promotions")):
        return "more_promo"
    if "daily set" in section:
        return "daily_set"
    if any(token in href_lower for token in ("dailyset", "daily-set")):
        return "daily_set"
    if any(token in title_lower for token in ("test your knowledge", "supersonic quiz", "this or that")):
        return "daily_set"
    if "daily set" in haystack:
        return "daily_set"
    return "unknown"


async def scan_dashboard_dom(page: Page) -> List[DashboardTask]:
    for _ in range(25):
        await page.evaluate("window.scrollBy(0, 500)")
        import asyncio
        await asyncio.sleep(0.15)
    import asyncio
    await asyncio.sleep(2)
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)

    raw_tasks = await page.evaluate(
        """() => {
            const results = [];
            const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
            const nodes = document.querySelectorAll('a, button, [role="button"], [role="link"], mee-card, .c-card');
            nodes.forEach((el, index) => {
                let text = el.innerText || '';
                const href = el.href || el.getAttribute('href') || el.getAttribute('data-url') || '';
                const aria = el.getAttribute('aria-label') || '';
                const title = el.getAttribute('title') || '';
                const biId = el.getAttribute('data-bi-id') || '';
                const roleHint = el.getAttribute('role') || '';
                const tagName = el.tagName ? el.tagName.toLowerCase() : '';
                let sectionHeading = '';

                const lowerText = text.toLowerCase();
                if (lowerText.includes('completed') || lowerText.includes('available on') || lowerText.includes('edge browsing streak') || lowerText.includes('minutes:') || el.querySelector('.mee-icon-SkypeCircleCheck') || el.querySelector('span[class*="complete"]')) {
                    return;
                }

                let isInKeepEarningSection = false;
                let ancestor = el.parentElement;
                for (let depth = 0; ancestor && depth < 10; depth++) {
                    const ancestorText = (ancestor.querySelector('h2, h3, h4, [class*="title"], [class*="heading"]') || {}).textContent || '';
                    const ancestorLower = ancestorText.toLowerCase();
                    if (ancestorText && !sectionHeading) {
                        sectionHeading = ancestorText.trim();
                    }
                    if (ancestorLower.includes('keep earning') || ancestorLower.includes('earn more') || ancestorLower.includes('more activities') || ancestorLower.includes('more promotions')) {
                        isInKeepEarningSection = true;
                        sectionHeading = ancestorText.trim();
                        break;
                    }
                    ancestor = ancestor.parentElement;
                }

                const hasBiId = el.hasAttribute('data-bi-id');
                if (text.includes('+') || aria.toLowerCase().includes('point') || tagName === 'mee-card' || isInKeepEarningSection || hasBiId) {
                    const textKey = normalize(text);
                    const hrefKey = normalize(href);
                    const titleKey = normalize(title);
                    const ariaKey = normalize(aria);
                    const sectionKey = normalize(sectionHeading);
                    const matchKey = [textKey.slice(0, 120), hrefKey, titleKey.slice(0, 80), ariaKey.slice(0, 80), biId, sectionKey.slice(0, 80)].filter(Boolean).join(' | ');
                    const className = typeof el.className === 'string' ? el.className : '';
                    results.push({
                        href,
                        text,
                        aria,
                        title,
                        index,
                        sectionHeading,
                        dataBiId: biId,
                        tagName,
                        roleHint,
                        matchKey,
                        domShape: {
                            tagName,
                            hasHref: Boolean(href),
                            hasDataBiId: Boolean(biId),
                            roleHint,
                            className: className.slice(0, 120),
                        },
                    });
                }
            });
            return results;
        }"""
    )

    parsed_tasks: List[DashboardTask] = []
    for raw in raw_tasks:
        text = raw.get("text", "").strip()
        href = raw.get("href", "")
        if not text:
            continue

        points = 10
        point_match = re.search(r"\+?\b(\d{1,3})\b(?!.*\b\d+\b)", text)
        if point_match:
            points = int(point_match.group(1))

        parts = [p.strip() for p in text.split("\n") if p.strip()]
        if len(parts) == 1:
            parts = [p.strip() for p in text.split("  ") if p.strip()]

        title = parts[0] if parts else "Unknown Task"
        desc = parts[1] if len(parts) > 1 else ""

        if title.startswith("+"):
            title = "Task"
        if repr(points) in desc:
            desc = desc.replace(f"+{points}", "").strip()

        title_lower = title.lower().strip()
        if title_lower in NON_ACTIONABLE_CARD_TITLES:
            continue
        if not href and title_lower in NON_ACTIONABLE_CARD_TITLES:
            continue

        section_heading = raw.get("sectionHeading", "")
        aria_label = raw.get("aria", "")
        element_title = raw.get("title", "")
        data_bi_id = raw.get("dataBiId", "")
        tag_name = raw.get("tagName", "")
        role_hint = raw.get("roleHint", "")
        text_content = text
        semantic_id = _normalize_key(title, desc, href or data_bi_id or element_title)
        match_key = raw.get("matchKey") or _normalize_key(text_content, href, element_title, aria_label, data_bi_id, section_heading)

        is_quiz = "quiz" in title_lower or "quiz" in href.lower() or "dsetqu" in href.lower()
        category = _infer_dashboard_category(section_heading, title, desc, href)
        selector_strategy, selector = _selector_for_task(href, data_bi_id, aria_label, element_title, tag_name, role_hint)
        fingerprint = build_task_fingerprint(title, href, points, category, section_heading)
        parsed_tasks.append({
            "title": title,
            "description": desc,
            "points": points,
            "url": href,
            "element_index": raw["index"],
            "category": category,
            "is_quiz": is_quiz,
            "semantic_id": semantic_id,
            "section_heading": section_heading,
            "aria_label": aria_label,
            "element_title": element_title,
            "data_bi_id": data_bi_id,
            "tag_name": tag_name,
            "role_hint": role_hint,
            "text_content": text_content,
            "match_key": match_key,
            "scan_version": SCAN_VERSION,
            "fingerprint": fingerprint,
            "selector_strategy": selector_strategy,
            "selector": selector,
            "dom_shape": raw.get("domShape") or {},
        })

    return parsed_tasks


async def click_task(page: Page, task: DashboardTask) -> bool:
    try:
        clicked = await page.evaluate(
            """(task) => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const nodes = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"], mee-card, .c-card'));
                const expectedKey = normalize(task.match_key || '');
                const titleKey = normalize(task.title || '');
                const descKey = normalize(task.description || '');
                const urlKey = normalize(task.url || '');
                const biKey = normalize(task.data_bi_id || '');
                const sectionKey = normalize(task.section_heading || '');
                const semanticId = normalize(task.semantic_id || '');

                const scoreNode = (el) => {
                    const text = normalize(el.innerText || '');
                    const href = normalize(el.href || el.getAttribute('href') || el.getAttribute('data-url') || '');
                    const aria = normalize(el.getAttribute('aria-label') || '');
                    const title = normalize(el.getAttribute('title') || '');
                    const biId = normalize(el.getAttribute('data-bi-id') || '');
                    const role = normalize(el.getAttribute('role') || '');
                    const tagName = normalize(el.tagName || '');
                    let section = '';
                    let ancestor = el.parentElement;
                    for (let depth = 0; ancestor && depth < 10; depth++) {
                        const ancestorText = (ancestor.querySelector('h2, h3, h4, [class*="title"], [class*="heading"]') || {}).textContent || '';
                        if (ancestorText && !section) {
                            section = normalize(ancestorText);
                        }
                        ancestor = ancestor.parentElement;
                    }
                    const matchKey = [text.slice(0, 120), href, title.slice(0, 80), aria.slice(0, 80), biId, section.slice(0, 80)].filter(Boolean).join(' | ');
                    let score = 0;
                    if (expectedKey && matchKey === expectedKey) score += 100;
                    if (semanticId && [text, href, title, biId].some(part => part && semanticId.includes(part))) score += 20;
                    if (titleKey && (text.includes(titleKey) || title.includes(titleKey) || aria.includes(titleKey))) score += 25;
                    if (descKey && text.includes(descKey)) score += 15;
                    if (urlKey && href && href === urlKey) score += 30;
                    if (biKey && biId && biId === biKey) score += 20;
                    if (sectionKey && section && section === sectionKey) score += 10;
                    if (role && task.role_hint && role === normalize(task.role_hint)) score += 3;
                    if (tagName && task.tag_name && tagName === normalize(task.tag_name)) score += 3;
                    return { el, score };
                };

                let best = null;
                for (const node of nodes) {
                    const candidate = scoreNode(node);
                    if (!best || candidate.score > best.score) best = candidate;
                }

                const target = best && best.score >= 25 ? best.el : nodes[task.element_index];
                if (!target) return false;
                target.scrollIntoView({ behavior: 'smooth', block: 'center' });
                target.click();
                return true;
            }""",
            task,
        )
        if clicked:
            return True
        await page.wait_for_timeout(1000)
        await page.evaluate(
            """(index) => {
                const nodes = document.querySelectorAll('a, button, [role="button"], [role="link"], mee-card, .c-card');
                const el = nodes[index];
                if (el) {
                    el.scrollIntoView({behavior: 'smooth', block: 'center'});
                    el.click();
                }
            }""",
            task["element_index"],
        )
        return True
    except Exception:
        return False


async def click_task_by_metadata(page: Page, metadata: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await page.evaluate(
            """(metadata) => {
                const normalize = (value) => String(value || '').replace(/\\s+/g, ' ').trim().toLowerCase();
                const normalizeHref = (value) => normalize(String(value || '').split('?')[0]);
                const nodes = Array.from(document.querySelectorAll('a, button, [role="button"], [role="link"], mee-card, .c-card'));
                const expectedTitle = normalize(metadata.title || '');
                const expectedHref = normalizeHref(metadata.url || metadata.href || '');
                const expectedFingerprint = normalize(metadata.fingerprint || '');
                const expectedCategory = normalize(metadata.category || '');
                const expectedPoints = Number(metadata.points || 0);
                const expectedIndex = Number.isInteger(metadata.element_index) ? metadata.element_index : null;

                const buildFingerprint = (title, href, points, category, section) => {
                    const key = [title, normalizeHref(href), points || 0, category, section].map(normalize).filter(Boolean).join(' ');
                    let h = 0;
                    for (let i = 0; i < key.length; i++) h = ((h << 5) - h + key.charCodeAt(i)) | 0;
                    return String(Math.abs(h));
                };

                const candidateMeta = (el, index) => {
                    const text = el.innerText || el.textContent || '';
                    const href = el.href || el.getAttribute('href') || el.getAttribute('data-url') || '';
                    const aria = el.getAttribute('aria-label') || '';
                    const titleAttr = el.getAttribute('title') || '';
                    const biId = el.getAttribute('data-bi-id') || '';
                    const tagName = el.tagName ? el.tagName.toLowerCase() : '';
                    let section = '';
                    let ancestor = el.parentElement;
                    for (let depth = 0; ancestor && depth < 10; depth++) {
                        const ancestorText = (ancestor.querySelector('h2, h3, h4, [class*="title"], [class*="heading"]') || {}).textContent || '';
                        if (ancestorText && !section) section = ancestorText;
                        ancestor = ancestor.parentElement;
                    }
                    const pointsMatch = String(text || '').match(/\\+?\\b(\\d{1,3})\\b(?!.*\\b\\d+\\b)/);
                    const points = pointsMatch ? Number(pointsMatch[1]) : 0;
                    const category = normalize(section).includes('daily set') ? 'daily_set' : (normalize(section).includes('earn') ? 'more_promo' : 'unknown');
                    const title = normalize(titleAttr || aria || String(text).split(String.fromCharCode(10)).filter(Boolean)[0] || '');
                    const fingerprint = buildFingerprint(title, href, points || expectedPoints, expectedCategory || category, section);
                    return { el, index, text: normalize(text), href: normalizeHref(href), aria: normalize(aria), title, biId: normalize(biId), tagName, section: normalize(section), points, category: normalize(category), fingerprint };
                };

                const validate = (meta) => {
                    let score = 0;
                    const reasons = [];
                    if (expectedHref && meta.href && meta.href === expectedHref) { score += 80; reasons.push('href'); }
                    if (expectedTitle && (meta.text.includes(expectedTitle) || meta.title.includes(expectedTitle) || meta.aria.includes(expectedTitle))) { score += 35; reasons.push('title'); }
                    if (metadata.data_bi_id && meta.biId && meta.biId === normalize(metadata.data_bi_id)) { score += 60; reasons.push('data-bi-id'); }
                    if (expectedPoints && meta.points && meta.points === expectedPoints) { score += 10; reasons.push('points'); }
                    if (expectedCategory && meta.category && meta.category === expectedCategory) { score += 10; reasons.push('category'); }
                    if (expectedFingerprint && meta.fingerprint && meta.fingerprint === expectedFingerprint) { score += 45; reasons.push('fingerprint'); }
                    return { score, reasons };
                };

                const tryClick = (meta, strategy, validation) => {
                    meta.el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                    meta.el.click();
                    return { clicked: true, strategy, validated: validation.score >= 35, reason: validation.reasons.join(',') || strategy };
                };

                if (metadata.selector && ['href', 'data-bi-id', 'aria', 'title'].includes(metadata.selector_strategy || '')) {
                    const selected = document.querySelector(metadata.selector);
                    if (selected) {
                        const meta = candidateMeta(selected, nodes.indexOf(selected));
                        const validation = validate(meta);
                        if (validation.score >= 35) return tryClick(meta, metadata.selector_strategy, validation);
                    }
                }

                let best = null;
                for (const [index, node] of nodes.entries()) {
                    const meta = candidateMeta(node, index);
                    const validation = validate(meta);
                    if (!best || validation.score > best.validation.score) best = { meta, validation };
                }
                if (best && best.validation.score >= 35) return tryClick(best.meta, 'validated_rescan', best.validation);

                if (expectedIndex !== null && nodes[expectedIndex]) {
                    const meta = candidateMeta(nodes[expectedIndex], expectedIndex);
                    const validation = validate(meta);
                    if (validation.score >= 35) return tryClick(meta, 'index_fallback', validation);
                    return { clicked: false, strategy: 'index_fallback', validated: false, reason: 'index_identity_mismatch' };
                }
                return { clicked: false, strategy: 'none', validated: false, reason: 'no_valid_candidate' };
            }""",
            metadata,
        )
        return result if isinstance(result, dict) else {"clicked": False, "strategy": "unknown", "validated": False, "reason": "invalid_result"}
    except Exception as e:
        return {"clicked": False, "strategy": "exception", "validated": False, "reason": str(e)[:160]}


async def click_task_by_index(page: Page, index: int) -> bool:
    result = await click_task_by_metadata(page, {"element_index": index})
    return bool(result.get("clicked"))
