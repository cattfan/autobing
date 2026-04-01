import asyncio
import random
import json
import logging
import traceback
from typing import Dict, Any, Optional

from playwright.async_api import Page, BrowserContext

logger = logging.getLogger(__name__)

class BingAppRewards:
    """
    Handles MS Rewards tasks exclusive to the Bing Mobile App
    such as Read-to-Earn (articles) and the Mobile App Daily Check-in coin.
    """
    
    # Official Bing App user agents required to trigger these tasks
    BING_APP_UA = [
        "Mozilla/5.0 (Linux; Android 14; Mobile) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Android/14 BingSapphire/30.0.410309301",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Mobile/15E148 BingSapphire/1.0.410309301",
    ]

    def __init__(self, humanizer):
        self.humanizer = humanizer

    async def read_to_earn(self, page: Page) -> bool:
        """
        Simulate reading news articles on the Bing Mobile App to get 30 points (10 articles).
        Loops dynamically until the max points quota is reached.
        """
        logger.info(" Starting Bing App Read-to-Earn...")
        max_loops = 4
        loop_count = 0
        
        try:
            while loop_count < max_loops:
                # Check current read points
                progress, max_pts = await self._get_read_progress(page)
                if max_pts == 0:
                    logger.info(" Read-to-Earn not available today (API max=0)")
                    return False
                    
                if progress >= max_pts:
                    logger.info(f" Read-to-Earn complete! ({progress}/{max_pts})")
                    return True
                    
                articles_to_read = max(1, (max_pts - progress) // 3)
                logger.info(f" Loop {loop_count+1}/{max_loops}: Target {articles_to_read} articles (Progress: {progress}/{max_pts})...")

                await page.goto("https://www.bing.com/news?dcf=1", wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(4)

                read_count = 0
                # Wait for news feed
                for _ in range(3):
                    try:
                        cards = await page.locator("a.title, a.card-title, .title-container a, a.card-link, .news-card a").all()
                        if cards:
                            break
                        await page.evaluate("window.scrollBy(0, 300)")
                        await asyncio.sleep(2)
                    except Exception:
                        pass

                cards = await page.locator("a.title, a.card-title, .title-container a, a.card-link, .news-card a").all()
                if not cards:
                    logger.warning(" Could not find news articles to read.")
                    loop_count += 1
                    continue

                import random
                random.shuffle(cards)
                
                for card in cards:
                    if read_count >= articles_to_read + 1:  # +1 buffer
                        break
                        
                    try:
                        if not await card.is_visible(timeout=1000):
                            continue
                            
                        url = await card.get_attribute("href")
                        if not url or "microsoft.com" in url or "windows" in url:
                            continue
                            
                        logger.debug(" Opening article...")
                        await card.scroll_into_view_if_needed()
                        await asyncio.sleep(random.uniform(1.0, 2.0))
                        
                        async with page.expect_navigation(timeout=10000):
                            await card.click()
                            
                        # Simulate reading
                        read_time = random.uniform(8.0, 15.0)
                        await self.humanizer.simulate_reading(page, read_time)
                        
                        read_count += 1
                        logger.info(f"   Read article {read_count} / {articles_to_read}")
                        
                        # Go back to news feed
                        await page.goto("https://www.bing.com/news?dcf=1", wait_until="domcontentloaded", timeout=35000)
                        await asyncio.sleep(random.uniform(2.0, 3.0))
                        
                    except Exception as e:
                        logger.debug(f"Article read failed: {e}")
                        try:
                            await page.goto("https://www.bing.com/news?dcf=1", wait_until="domcontentloaded", timeout=10000)
                        except Exception:
                            pass
                            
                # Verify updated progress
                new_prog, _ = await self._get_read_progress(page)
                if new_prog > progress:
                    logger.info(f" Progress updated: {progress} -> {new_prog} / {max_pts}")
                else:
                    logger.warning(f" Progress unchanged ({progress}/{max_pts}). Check UA or network delay.")
                    
                loop_count += 1

            logger.warning(f" Reached max loops for Read-to-Earn. Final progress: {progress}/{max_pts}")
            return False

        except Exception as e:
            logger.error(f" Read-to-Earn error: {e}")
            import traceback
            logger.error(f"Read-to-earn error: {traceback.format_exc()}")
            return False

    async def daily_checkin(self, page: Page) -> bool:
        """
        Claim the mobile app daily check-in coin via the Rewards me/activities API.
        """
        logger.info(" Checking Bing App Daily Check-in...")
        try:
            # Get dashboard data
            await page.goto("https://us.bing.com/rewards/app/dashboard", wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(4)

            # Look for checkin UI button just in case
            try:
                checkin_btn = page.locator('button:has-text("Claim"), button:has-text("Check in")').first
                if await checkin_btn.count() > 0 and await checkin_btn.is_visible(timeout=3000):
                    logger.info(" Found Daily Check-in button, clicking...")
                    await checkin_btn.click()
                    await asyncio.sleep(3)
                    logger.info(" Claimed Daily Check-in via UI")
                    return True
            except Exception:
                pass

            # Fallback to API payload using Playwright context (bypasses CORS)
            try:
                r = await page.request.get('https://rewards.bing.com/api/getuserinfo?type=1', headers={'Accept': 'application/json'})
                checkin_data = await r.json()
            except Exception as e:
                logger.warning(f" Failed to fetch userinfo for check-in via page.request: {e}")
                checkin_data = None
                
            if not checkin_data:
                logger.warning(" checkin_data is empty")
                return False
                
            dashboard = checkin_data.get("dashboard", {}) if checkin_data else {}
            promos = dashboard.get("promotionalItems", []) + dashboard.get("punchCards", [])
            
            offer_id_to_claim = None
            
            logger.info(f" Found {len(dashboard.get('punchCards', []))} punchCards and {len(dashboard.get('promotionalItems', []))} promos in getuserinfo.")
            for p in promos:
                parent = p.get("parentPromotion", p)
                name = parent.get("name", "")
                title = parent.get("title", "")
                offer_id = parent.get("offerId")
                
                title_lower = (title or name or "").lower()
                if parent.get("promotionType") == "checkin" or "check in" in title_lower or "app streak" in title_lower or "check-in" in title_lower:
                    if parent.get("complete", False):
                        logger.info(" Daily Check-in already complete.")
                        return True
                    if offer_id:
                        offer_id_to_claim = offer_id
                        break
            
            # If not found in API, use known fallback IDs
            fallback_ids = [offer_id_to_claim] if offer_id_to_claim else ["ENUS_checkin", "MobileApp_Checkin", "App_Checkin", "checkin"]
            
            logger.info(f" Attempting check-in API claim using IDs: {fallback_ids}...")
            
            for test_offer_id in fallback_ids:
                if not test_offer_id: continue
                logger.info(f"  -> Spoofing offerId: {test_offer_id}")
                claim_result = await page.evaluate(f'''
                    async () => {{
                        try {{
                            const r = await fetch('https://prod.rewardsplatform.microsoft.com/dapi/me/activities', {{
                                method: 'POST',
                                credentials: 'include',
                                headers: {{ 'Content-Type': 'application/json' }},
                                body: JSON.stringify({{
                                    id: crypto.randomUUID(),
                                    offerId: "{test_offer_id}",
                                    type: "urlreward",
                                    amount: 1,
                                }})
                            }});
                            return r.status;
                        }} catch(e) {{ return -1; }}
                    }}
                ''')
                
                if claim_result in (200, 204):
                    logger.info(f" Daily Check-in claimed successfully! (Used {test_offer_id})")
                    return True
                else:
                    logger.debug(f" API check-in failed for {test_offer_id} with status {claim_result}")
            
            logger.info(" No Daily Check-in promotion could be claimed (Maybe already collected or regional block).")
            return False

        except Exception as e:
            logger.warning(f" Daily Check-in error: {e}")
            return False

    async def _get_read_progress(self, page: Page) -> tuple[int, int]:
        """Fetch the read article progress from the Rewards API."""
        try:
            return await page.evaluate("""
                async () => {
                    try {
                        const r = await fetch('https://rewards.bing.com/api/getuserinfo?type=1');
                        const data = await r.json();
                        const counters = data?.dashboard?.userStatus?.counters || {};
                        const news = counters['readArticle'];
                        const n = Array.isArray(news) ? news[0] : news;
                        if (n) {
                            return [n.pointProgress || 0, n.pointProgressMax || 0];
                        }
                        return [0, 0];
                    } catch(e) { return [0, 0]; }
                }
            """)
        except Exception:
            return 0, 0
