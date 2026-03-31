import asyncio
from playwright.async_api import async_playwright

async def main():
    async with async_playwright() as p:
        try:
            browser = await p.chromium.connect_over_cdp("http://127.0.0.1:9323")
            page = browser.contexts[0].pages[0] if browser.contexts[0].pages else await browser.contexts[0].new_page()
            print(f"URL: {page.url}")
            if "rewards.bing.com/earn" not in page.url:
                await page.goto("https://rewards.bing.com/earn")
                await page.wait_for_timeout(3000)
                
            edge_cards = await page.evaluate('''() => {
                const cards = document.querySelectorAll('mee-card');
                const results = [];
                for (let card of cards) {
                    if (card.textContent.includes('Edge')) {
                        results.push(card.innerText || card.textContent);
                    }
                }
                return results;
            }''')
            
            print("EDGE CARDS FOUND:")
            for idx, text in enumerate(edge_cards):
                print(f"--- Card {idx+1} ---")
                print(text)
                
            await browser.close()
        except Exception as e:
            print("Error", e)

if __name__ == "__main__":
    asyncio.run(main())
