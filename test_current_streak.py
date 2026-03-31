import re
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        try:
            print("Connecting to live Edge on 9323...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9323")
            page = browser.contexts[0].pages[0] if browser.contexts[0].pages else await browser.contexts[0].new_page()
            
            original_url = page.url
            print(f"Current page is {original_url}")
            
            if "rewards.bing.com/earn" not in original_url:
                await page.goto("https://rewards.bing.com/earn")
                await page.wait_for_timeout(3000)
            
            page_text = await page.locator("body").inner_text()
            
            edge_match = re.search(
                r"Edge(?:\s+Browsing(?:\s+Streak)?)?(?:[\s\S]{0,150})Minutes:\s*(\d+)\s*/\s*(\d+)",
                page_text, re.IGNORECASE
            )
            if edge_match:
                print(f"LIVE STREAK: {edge_match.group(1)} / {edge_match.group(2)}")
            else:
                print("No edge streak digits found!")
                
            if original_url and "rewards.bing.com" not in original_url:
                print("Restoring original URL...")
                await page.goto(original_url)
                
            await browser.close()
            print("Done.")
        except Exception as e:
            print("Error connecting:", e)

if __name__ == "__main__":
    asyncio.run(main())
