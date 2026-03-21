"""
Notification system: Discord webhook and Telegram bot.
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from src.utils import logger


class Notifier:
    """Sends notifications via Discord webhook and/or Telegram bot."""

    def __init__(self, settings: dict):
        self.discord_webhook = settings.get("discord_webhook", "")
        self.telegram_token = settings.get("telegram_bot_token", "")
        self.telegram_chat_id = settings.get("telegram_chat_id", "")

    @property
    def discord_enabled(self) -> bool:
        return bool(self.discord_webhook)

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_token and self.telegram_chat_id)

    def send_completion(
        self,
        account: str,
        points: int,
        searches_done: dict,
        daily_set: dict,
        punch_cards: dict,
        promotions: dict,
        streak: int = 0,
        graph_path: Optional[str] = None,
        verified_complete: bool = True,
        remaining_items: Optional[list[str]] = None,
    ) -> None:
        """Send a completion notification with farming results."""
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        remaining_items = remaining_items or []
        status_line = "[Rewards Farming Verified]" if verified_complete else "[Rewards Farming Incomplete]"
        text_msg = (
            f"{status_line}\n"
            f"Account: `{account[:5]}***`\n"
            f"Time: {timestamp}\n\n"
            f"Points: {points:,}\n"
            f"Streak: {streak} days\n\n"
            "[Results]\n"
            f"Desktop: {searches_done.get('desktop', {}).get('completed', 0)}/"
            f"{searches_done.get('desktop', {}).get('total', 0)}\n"
            f"Mobile: {searches_done.get('mobile', {}).get('completed', 0)}/"
            f"{searches_done.get('mobile', {}).get('total', 0)}\n"
            f"Edge: {searches_done.get('edge', {}).get('completed', 0)}/"
            f"{searches_done.get('edge', {}).get('total', 0)}\n"
            f"Daily Set: {daily_set.get('completed', 0)}/"
            f"{daily_set.get('total', 0)}\n"
            f"Punch Cards: {punch_cards.get('completed', 0)}/"
            f"{punch_cards.get('total', 0)}\n"
            f"Promotions: {promotions.get('completed', 0)}/"
            f"{promotions.get('total', 0)}\n"
        )

        if remaining_items:
            text_msg += "\n[Remaining]\n"
            for item in remaining_items[:8]:
                text_msg += f"- {item}\n"

        if self.discord_enabled:
            self._send_discord(text_msg, graph_path)

        if self.telegram_enabled:
            self._send_telegram(text_msg, graph_path)

    def send_error(self, account: str, error: str) -> None:
        """Send an error notification."""
        msg = (
            "[Rewards Bot Error]\n"
            f"Account: `{account[:5]}***`\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}\n"
            f"Error: {error}"
        )

        if self.discord_enabled:
            self._send_discord(msg)
        if self.telegram_enabled:
            self._send_telegram(msg)

    def send_streak_warning(self, account: str, streak: int) -> None:
        """Send a streak warning notification."""
        msg = (
            "[Streak Warning]\n"
            f"Account: `{account[:5]}***`\n"
            f"Current streak: {streak} days\n"
            "Run the bot today to keep your streak!"
        )

        if self.discord_enabled:
            self._send_discord(msg)
        if self.telegram_enabled:
            self._send_telegram(msg)

    def send_redeem_alert(self, account: str, points: int, goal: int) -> None:
        """Send auto-redeem threshold notification."""
        msg = (
            "[Auto-Redeem Alert]\n"
            f"Account: `{account[:5]}***`\n"
            f"Points: {points:,} / Goal: {goal:,}\n"
            "Ready to redeem!"
        )

        if self.discord_enabled:
            self._send_discord(msg)
        if self.telegram_enabled:
            self._send_telegram(msg)

    def send_manual_action(
        self,
        account: str,
        context: str,
        url: str,
        details: str,
        screenshot_path: Optional[str] = None,
    ) -> None:
        """Notify the user that manual action in the browser is required."""
        msg = (
            "[Manual Verification Required]\n"
            f"Account: `{account[:5]}***`\n"
            f"Context: {context}\n"
            f"URL: {url}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S')}\n"
            f"Details: {details}"
        )

        if self.discord_enabled:
            self._send_discord(msg, screenshot_path)
        if self.telegram_enabled:
            self._send_telegram(msg, screenshot_path)

    def _send_discord(self, message: str, file_path: Optional[str] = None) -> None:
        """Send a message to Discord webhook."""
        try:
            embed = {
                "description": message,
                "color": 0x00D4FF,
                "footer": {"text": "Rewards Search Automator v1.0"},
                "timestamp": datetime.utcnow().isoformat(),
            }
            payload = {"embeds": [embed]}

            if file_path and Path(file_path).exists():
                with open(file_path, "rb") as f:
                    files = {"file": (Path(file_path).name, f)}
                    response = requests.post(
                        self.discord_webhook,
                        data={"payload_json": json.dumps(payload)},
                        files=files,
                        timeout=10,
                    )
            else:
                response = requests.post(
                    self.discord_webhook,
                    json=payload,
                    timeout=10,
                )

            if response.status_code in (200, 204):
                logger.info("Discord notification sent")
            else:
                logger.warning(
                    f"Discord webhook returned {response.status_code}: {response.text}"
                )

        except Exception as e:
            logger.error(f"Discord notification failed: {e}")

    def _send_telegram(self, message: str, file_path: Optional[str] = None) -> None:
        """Send a message via Telegram bot."""
        try:
            api_url = f"https://api.telegram.org/bot{self.telegram_token}"

            clean_msg = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", message)
            clean_msg = re.sub(r"`(.+?)`", r"<code>\1</code>", clean_msg)
            clean_msg = clean_msg.replace("*", "")

            response = requests.post(
                f"{api_url}/sendMessage",
                json={
                    "chat_id": self.telegram_chat_id,
                    "text": clean_msg,
                    "parse_mode": "HTML",
                },
                timeout=10,
            )

            if response.status_code == 200:
                logger.info("Telegram message sent")
            else:
                logger.warning(f"Telegram API returned {response.status_code}")

            if file_path and Path(file_path).exists():
                with open(file_path, "rb") as f:
                    requests.post(
                        f"{api_url}/sendPhoto",
                        data={"chat_id": self.telegram_chat_id},
                        files={"photo": f},
                        timeout=10,
                    )
                    logger.info("Telegram graph sent")

        except Exception as e:
            logger.error(f"Telegram notification failed: {e}")

    def test_notifications(self) -> dict:
        """Test all configured notification channels."""
        results = {}

        if self.discord_enabled:
            try:
                self._send_discord("[Test] Notification from Rewards Bot")
                results["discord"] = "Success"
            except Exception as e:
                results["discord"] = f"Failed: {e}"
        else:
            results["discord"] = "Not configured"

        if self.telegram_enabled:
            try:
                self._send_telegram("[Test] Notification from Rewards Bot")
                results["telegram"] = "Success"
            except Exception as e:
                results["telegram"] = f"Failed: {e}"
        else:
            results["telegram"] = "Not configured"

        return results
