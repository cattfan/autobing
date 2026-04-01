"""
Points tracker, CSV logging, graph generation, auto-redeem, and streak protection.
"""

from __future__ import annotations
import csv
import asyncio
import re
from pathlib import Path
from datetime import datetime
from typing import Optional

from playwright.async_api import Page

from src.utils import logger, REWARDS_URL, DATA_DIR, today_str


class PointsTracker:
    """Tracks, logs, and visualizes Microsoft Rewards points."""

    CSV_PATH = DATA_DIR / "points_log.csv"
    GRAPH_PATH = DATA_DIR / "graph.png"

    def __init__(self, settings: dict):
        self.settings = settings
        self.current_points: int = 0
        self.streak_count: int = 0
        self._ensure_csv()

    def _ensure_csv(self) -> None:
        """Create CSV file with headers if it doesn't exist."""
        if not self.CSV_PATH.exists():
            with open(self.CSV_PATH, "w", newline="", encoding="utf-8") as f:
                writer = csv.writer(f)
                writer.writerow([
                    "date", "total_points", "earned_today",
                    "desktop_done", "mobile_done", "edge_done",
                    "daily_set_done", "streak"
                ])

    async def read_points(self, page: Page) -> dict:
        """
        Read current points and status from Rewards dashboard.

        Returns:
            Dict with points info
        """
        info = {
            "total_points": 0,
            "available_points": 0,
            "streak": 0,
            "level": "",
        }

        try:
            await page.goto(REWARDS_URL, wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(3)

            # Read total points
            points_el = page.locator(
                '#id_rc, .mee-icon-AddMedium + span, '
                '[data-bi-id="rewards-segment"] span, '
                '.points-container span'
            )
            if await points_el.count() > 0:
                text = await points_el.first.inner_text()
                numbers = re.findall(r"[\d,]+", text)
                if numbers:
                    info["total_points"] = int(numbers[0].replace(",", ""))

            # Try to get streak info
            streak_el = page.locator(
                '[class*="streak"] span, .streak-count, '
                '[data-bi-id*="streak"]'
            )
            if await streak_el.count() > 0:
                text = await streak_el.first.inner_text()
                numbers = re.findall(r"\d+", text)
                if numbers:
                    info["streak"] = int(numbers[0])

            if info["total_points"] <= 0:
                api_info = await self._read_points_from_api(page)
                info["total_points"] = api_info.get("total_points", info["total_points"])
                info["available_points"] = api_info.get("available_points", info["available_points"])
                info["level"] = api_info.get("level", info["level"])

            self.current_points = info["total_points"]
            self.streak_count = info["streak"]
            logger.info(
                f"Points: {info['total_points']}, Streak: {info['streak']} days"
            )

        except Exception as e:
            logger.warning(f"Failed to read points: {e}")

        return info

    async def _read_points_from_api(self, page: Page) -> dict:
        """Fallback to the Rewards API when the DOM selectors fail or lag behind."""
        try:
            data = await page.evaluate("""
                async () => {
                    try {
                        const r = await fetch('/api/getuserinfo?type=1', {
                            credentials: 'include',
                            headers: {'Accept': 'application/json'}
                        });
                        return await r.json();
                    } catch (e) {
                        return null;
                    }
                }
            """)

            dashboard = (data or {}).get("dashboard", {})
            user_status = dashboard.get("userStatus", {})
            level_info = user_status.get("levelInfo", {})

            return {
                "total_points": user_status.get("availablePoints", 0),
                "available_points": user_status.get("availablePoints", 0),
                "level": (
                    level_info.get("activeLevel")
                    or level_info.get("levelName")
                    or ""
                ),
            }
        except Exception as e:
            logger.debug(f"Points API fallback failed: {e}")
            return {
                "total_points": 0,
                "available_points": 0,
                "level": "",
            }

    def log_daily(
        self,
        total_points: int,
        earned_today: int = 0,
        desktop_done: bool = False,
        mobile_done: bool = False,
        edge_done: bool = False,
        daily_set_done: bool = False,
        streak: int = 0,
    ) -> None:
        """Log daily points to CSV."""
        with open(self.CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow([
                today_str(),
                total_points,
                earned_today,
                desktop_done,
                mobile_done,
                edge_done,
                daily_set_done,
                streak,
            ])
        logger.info(f"Daily points logged: {total_points} (earned: {earned_today})")

    def get_history(self, days: int = 30) -> list[dict]:
        """Get points history from CSV."""
        history = []
        try:
            with open(self.CSV_PATH, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    history.append(row)
        except Exception:
            pass
        return history[-days:] if len(history) > days else history

    def generate_graph(self, days: int = 30) -> Optional[str]:
        """Generate a points progress graph using matplotlib."""
        try:
            import matplotlib
            matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            import matplotlib.dates as mdates

            history = self.get_history(days)
            if not history:
                logger.warning("No history data for graph")
                return None

            dates = []
            points = []
            daily_earned = []

            for entry in history:
                try:
                    dates.append(datetime.strptime(entry["date"], "%Y-%m-%d"))
                    points.append(int(entry.get("total_points", 0)))
                    daily_earned.append(int(entry.get("earned_today", 0)))
                except (ValueError, KeyError):
                    continue

            if not dates:
                return None

            # Create figure with dark theme
            plt.style.use("dark_background")
            fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 8), height_ratios=[2, 1])
            fig.suptitle(
                "📊 Microsoft Rewards Progress",
                fontsize=16,
                fontweight="bold",
                color="#00d4ff",
            )

            # Total points line chart
            ax1.plot(dates, points, color="#00d4ff", linewidth=2, marker="o", markersize=4)
            ax1.fill_between(dates, points, alpha=0.2, color="#00d4ff")
            ax1.set_ylabel("Total Points", color="#ccc")
            ax1.set_title("Total Points Over Time", color="#aaa", fontsize=12)
            ax1.grid(True, alpha=0.2)
            ax1.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

            # Daily earned bar chart
            ax2.bar(dates, daily_earned, color="#7c3aed", alpha=0.8, width=0.8)
            ax2.set_ylabel("Points Earned", color="#ccc")
            ax2.set_title("Daily Points Earned", color="#aaa", fontsize=12)
            ax2.grid(True, alpha=0.2)
            ax2.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))

            plt.tight_layout()
            plt.savefig(str(self.GRAPH_PATH), dpi=150, bbox_inches="tight")
            plt.close()

            logger.info(f"Graph saved to {self.GRAPH_PATH}")
            return str(self.GRAPH_PATH)

        except ImportError:
            logger.warning("matplotlib not installed, cannot generate graph")
            return None
        except Exception as e:
            logger.error(f"Graph generation error: {e}")
            return None

    def get_statistics(self) -> dict:
        """Calculate statistics from history."""
        history = self.get_history(365)
        if not history:
            return {"total_earned": 0, "avg_daily": 0, "streak": 0, "days_tracked": 0}

        earned_values = []
        for entry in history:
            try:
                earned_values.append(int(entry.get("earned_today", 0)))
            except (ValueError, KeyError):
                pass

        total_earned = sum(earned_values)
        avg_daily = total_earned / len(earned_values) if earned_values else 0
        max_streak = max(
            (int(e.get("streak", 0)) for e in history),
            default=0,
        )

        return {
            "total_earned": total_earned,
            "avg_daily": round(avg_daily, 1),
            "streak": self.streak_count,
            "max_streak": max_streak,
            "days_tracked": len(history),
            "estimated_monthly": round(avg_daily * 30, 0),
        }

    async def check_auto_redeem(self, page: Page) -> Optional[str]:
        """
        Check if auto-redeem should trigger.

        Returns:
            Redeemed item name, or None
        """
        if not self.settings.get("auto_redeem", False):
            return None

        goal = self.settings.get("auto_redeem_goal", 5000)

        if self.current_points >= goal:
            logger.info(f"Auto-redeem threshold reached: {self.current_points}/{goal}")
            # Navigate to redeem page
            # Note: actual redemption requires careful implementation
            # For safety, just log and notify instead of auto-clicking
            return f"Threshold reached: {self.current_points} >= {goal} points"

        return None

    async def check_streak_protection(self, page: Page) -> dict:
        """
        Check streak status and activate protection if needed.

        Returns:
            Dict with streak info
        """
        info = {"streak": self.streak_count, "protected": False, "warning": ""}

        if not self.settings.get("streak_protection", True):
            return info

        if self.streak_count > 0:
            logger.info(f"Current streak: {self.streak_count} days")
        else:
            info["warning"] = "No active streak detected"

        return info
