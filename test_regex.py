import re
import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9323")
        page = browser.contexts[0].pages[0] if browser.contexts[0].pages else await browser.contexts[0].new_page()
        if "rewards.bing.com/earn" not in page.url:
            await page.goto("https://rewards.bing.com/earn")
            await page.wait_for_timeout(3000)
            
        page_text = await page.locator("body").inner_text()
        
        edge_match = re.search(
            r"Edge(?:\s+Browsing(?:\s+Streak)?)?.*?Minutes:\s*(\d+)\s*/\s*(\d+)",
            page_text, re.IGNORECASE | re.DOTALL,
        )
        if not edge_match:
            edge_match = re.search(
                r"Edge(?:\s+Browsing(?:\s+Streak)?)?.*?Activit(?:y|ies):\s*(\d+)\s*/\s*(\d+)",
                page_text, re.IGNORECASE | re.DOTALL,
            )
        if not edge_match:
            edge_match = re.search(
                r"Edge\s+Browsing\s+Streak.*?(\d+)\s*/\s*(\d+)",
                page_text, re.IGNORECASE | re.DOTALL,
            )
            
        if edge_match:
            print(f"Matched! digits: {edge_match.group(1)} / {edge_match.group(2)}")
            print(f"Full matched substring:\n{edge_match.group(0)[:200]} ... {edge_match.group(0)[-50:]}")
        else:
            print("No match found.")

        await browser.close()
        
if __name__ == "__main__":
    asyncio.run(main())
