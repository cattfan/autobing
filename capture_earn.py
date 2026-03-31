import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        try:
            print("Connecting to Edge on 9323...")
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9323")
            context = browser.contexts[0]
            page = context.pages[0] if context.pages else await context.new_page()
            
            print(f"Current URL: {page.url}")
            await page.goto("https://rewards.bing.com/earn")
            await page.wait_for_timeout(3000)
            
            await page.screenshot(path="capture_earn.png", full_page=True)
            print("Screenshot saved to capture_earn.png")
            
            body = await page.inner_text("body")
            print("--- BODY TEXT ---")
            for line in body.split('\n'):
                if 'Edge' in line or 'min' in line or 'streak' in line.lower() or 'activit' in line.lower():
                    print(line.strip()[:100])
                    
            await browser.close()
        except Exception as e:
            print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(main())
