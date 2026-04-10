import re
import json
from typing import TypedDict, List, Optional
from playwright.async_api import Page

class DashboardTask(TypedDict):
    title: str
    description: str
    points: int
    url: str
    element_index: int  # Index in the array returned by querySelectorAll('a')
    category: str       # "daily_set" or "more_promo" (inferred)
    is_quiz: bool


def _infer_dashboard_category(section_heading: str, title: str, description: str, href: str) -> str:
    """Infer task category from the nearest visible section heading and task copy."""
    section = (section_heading or "").strip().lower()
    title_lower = (title or "").strip().lower()
    desc_lower = (description or "").strip().lower()
    href_lower = (href or "").strip().lower()
    haystack = " ".join([title_lower, desc_lower, href_lower]).lower()

    if any(token in section for token in ("keep earning", "more activities", "more promotions")):
        return "more_promo"
    if "daily set" in section:
        return "daily_set"
    if any(token in href_lower for token in ("form=dset", "dsetqu", "dailyset", "daily-set")):
        return "daily_set"
    if any(token in title_lower for token in ("test your knowledge", "supersonic quiz", "this or that")):
        return "daily_set"
    if any(token in href_lower for token in ("referandearn", "rnoreward=1", "form=tgrew", "rewards.bing.com/earn")):
        return "more_promo"
    if any(token in title_lower for token in ("turn referrals into rewards", "order history", "streak bonus")):
        return "more_promo"
    if "daily set" in haystack:
        return "daily_set"
    return "unknown"


async def scan_dashboard_dom(page: Page) -> List[DashboardTask]:
    """
    Scrapes the Rewards dashboard natively using Playwright DOM evaluation.
    This entirely replaces the legacy HTTP API getuserinfo call.
    """
    
    # Scroll to the bottom to trigger lazy-load of ALL sections (including "Keep earning")
    for _ in range(25):
        await page.evaluate("window.scrollBy(0, 500)")
        import asyncio
        await asyncio.sleep(0.15)
    import asyncio
    await asyncio.sleep(2)
    # Scroll back to top for consistent DOM state
    await page.evaluate("window.scrollTo(0, 0)")
    await asyncio.sleep(0.5)
    
    # Evaluate JS to find all anchor tags that represent unfinished tasks
    raw_tasks = await page.evaluate("""() => {
        const results = [];
        // We select broadly because some tasks use button or div[role='link']
        const nodes = document.querySelectorAll('a, button, [role="button"], [role="link"], mee-card, .c-card');
        nodes.forEach((el, index) => {
            let text = el.innerText || '';
            const href = el.href || el.getAttribute('href') || el.getAttribute('data-url') || '';
            const aria = el.getAttribute('aria-label') || '';
            const title = el.getAttribute('title') || '';
            let sectionHeading = '';
            
            const lowerText = text.toLowerCase();
            // Strictly skip completed, locked cards, and non-actionable summary widgets like Edge Browsing Streak
            if (lowerText.includes('completed') || lowerText.includes('available on') || lowerText.includes('edge browsing streak') || lowerText.includes('minutes:') || el.querySelector('.mee-icon-SkypeCircleCheck') || el.querySelector('span[class*="complete"]')) {
                return; 
            }
            
            // Check if this element lives inside a "Keep earning" or "More activities" section
            let isInKeepEarningSection = false;
            let ancestor = el.parentElement;
            for (let depth = 0; ancestor && depth < 10; depth++) {
                const ancestorText = (ancestor.querySelector('h2, h3, h4, [class*="title"], [class*="heading"]') || {}).textContent || '';
                const ancestorLower = ancestorText.toLowerCase();
                if (ancestorText && !sectionHeading) {
                    sectionHeading = ancestorText.trim();
                }
                if (ancestorLower.includes('keep earning') || ancestorLower.includes('more activities') || ancestorLower.includes('more promotions')) {
                    isInKeepEarningSection = true;
                    break;
                }
                ancestor = ancestor.parentElement;
            }
            
            // Unfinished tasks typically have "+5", "+10", "+50" etc in their text content.
            // Some cards omit "+" but use "mee-card" tag or have 'point' in aria.
            // Cards inside "Keep earning" section are always captured regardless of +.
            const tagName = el.tagName ? el.tagName.toLowerCase() : '';
            const hasBiId = el.hasAttribute('data-bi-id');
            if (text.includes('+') || aria.toLowerCase().includes('point') || tagName === 'mee-card' || isInKeepEarningSection || hasBiId) {
                results.push({
                    href: href,
                    text: text,
                    aria: aria,
                    title: title,
                    index: index,
                    sectionHeading: sectionHeading
                });
            }
        });
        return results;
    }""")
    
    parsed_tasks: List[DashboardTask] = []
    
    for raw in raw_tasks:
        text = raw.get("text", "").strip()
        href = raw.get("href", "")
        if not text:
            continue
            
        # Parse points. Usually looks like "+10", "+5", "point 15", or simply a floating "15" at the end.
        points = 10
        point_match = re.search(r'\+?\b(\d{1,3})\b(?!.*\b\d+\b)', text)
        if point_match:
            points = int(point_match.group(1))
            
        # Parse Title and Description
        # e.g., "Jupiter's moons\nDiscover the many moons orbiting Jupiter.\n+10"
        # Since innerText keeps newlines, we split by newline or double spaces
        parts = [p.strip() for p in text.split('\n') if p.strip()]
        if len(parts) == 1:
            parts = [p.strip() for p in text.split('  ') if p.strip()]
            
        title = parts[0] if parts else "Unknown Task"
        desc = parts[1] if len(parts) > 1 else ""
        
        # Strip the +10 out of the title/desc if it got caught
        if title.startswith('+'): title = "Task"
        if repr(points) in desc:
            desc = desc.replace(f"+{points}", "").strip()
            
        # Infer type
        is_quiz = "quiz" in title.lower() or "quiz" in href.lower() or "dsetqu" in href.lower()
        
        parsed_tasks.append({
            "title": title,
            "description": desc,
            "points": points,
            "url": href,
            "element_index": raw["index"],
            "category": _infer_dashboard_category(raw.get("sectionHeading", ""), title, desc, href),
            "is_quiz": is_quiz
        })
        
    return parsed_tasks

async def click_task_by_index(page: Page, index: int) -> bool:
    """Uses Playwright to physically click the anchor element found at the given index."""
    try:
        # We scroll the nth matching node into view and click it
        await page.evaluate(f"""(index) => {{
            const nodes = document.querySelectorAll('a, button, [role="button"], [role="link"], mee-card, .c-card');
            const el = nodes[index];
            if (el) {{
                el.scrollIntoView({{behavior: 'smooth', block: 'center'}});
            }}
        }}""", index)
        
        # Small human delay before clicking
        await page.wait_for_timeout(1000)
        
        # Click natively via evaluation to avoid strict playwright visibility checks that might fail on complex cards
        await page.evaluate(f"""(index) => {{
            const nodes = document.querySelectorAll('a, button, [role="button"], [role="link"], mee-card, .c-card');
            const el = nodes[index];
            if (el) el.click();
        }}""", index)
        return True
    except Exception as e:
        return False
