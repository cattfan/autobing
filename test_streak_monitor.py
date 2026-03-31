"""
Edge Streak Monitor - Restored to working version (be033ab) + Background DOM Scraper
"""
import asyncio
from pathlib import Path
from src.utils import load_settings
from src.crypto import load_plaintext_accounts, load_encrypted_accounts
from src.edge_streak_native import NativeEdgeStreak
from rich.console import Console

console = Console()

async def monitor():
    accounts = load_plaintext_accounts()
    if not accounts:
        try:
            accounts = load_encrypted_accounts("admin") 
        except:
            accounts = load_encrypted_accounts("123")
            
    if not accounts:
        print("Failed to load accounts.")
        return
        
    account = accounts[0]
    email = account['email']
    print(f"Tracking streak for {email}")
    
    # Needs profile dir
    data_dir = Path("data")
    state_path = data_dir / "profiles" / f"{email.replace('@', '_at_')}_state.json"
    
    edge_streak = NativeEdgeStreak(account_email=email, storage_state_path=state_path)
    
    def progress_cb(done, total):
        pass # Not using callback purely for logging, we rely on NativeEdgeStreak logger
        
    await edge_streak.browse(
        target_minutes=30,
        on_progress=progress_cb,
        start_url="https://www.bing.com",
    )
    print("Done!")

if __name__ == "__main__":
    asyncio.run(monitor())
