"""
Google Trends RSS + Vietnamese query generation with typo simulation.
"""

import random
import re
import xml.etree.ElementTree as ET
from typing import Optional

import httpx

from src.utils import logger, load_search_topics


# ─── Vietnamese typo maps (Telex keyboard mistakes) ────────────

_VIET_TONE_SWAPS = {
    'ă': 'a', 'â': 'a', 'ê': 'e', 'ô': 'o', 'ơ': 'o', 'ư': 'u',
    'đ': 'd', 'á': 'a', 'à': 'a', 'ả': 'a', 'ã': 'a', 'ạ': 'a',
    'é': 'e', 'è': 'e', 'ẻ': 'e', 'ẽ': 'e', 'ẹ': 'e',
    'ó': 'o', 'ò': 'o', 'ỏ': 'o', 'õ': 'o', 'ọ': 'o',
    'ú': 'u', 'ù': 'u', 'ủ': 'u', 'ũ': 'u', 'ụ': 'u',
    'í': 'i', 'ì': 'i', 'ỉ': 'i', 'ĩ': 'i', 'ị': 'i',
}

_TONE_CONFUSIONS = {
    'ô': 'ơ', 'ơ': 'ô', 'ă': 'â', 'â': 'ă',
    'á': 'ã', 'ã': 'á', 'è': 'ẻ', 'ẻ': 'è',
    'ó': 'ỏ', 'ỏ': 'ó', 'ú': 'ủ', 'ủ': 'ú',
}

_ADJACENT_KEYS = {
    'a': 'sq', 'b': 'vn', 'c': 'xv', 'd': 'sf', 'e': 'wr',
    'g': 'fh', 'h': 'gj', 'i': 'uo', 'k': 'jl', 'l': 'k',
    'm': 'n', 'n': 'bm', 'o': 'ip', 'p': 'o', 'r': 'et',
    's': 'ad', 't': 'ry', 'u': 'yi', 'v': 'cb', 'w': 'qe',
    'y': 'tu',
}


def _add_vietnamese_typo(text: str) -> str:
    """Add realistic Vietnamese keyboard typos (Telex input mistakes)."""
    chars = list(text)
    typo_count = random.randint(1, 3)

    for _ in range(typo_count):
        if len(chars) < 3:
            break

        typo_type = random.choices(
            ['drop_tone', 'wrong_tone', 'double_letter', 'adjacent_key', 'swap_chars'],
            weights=[30, 20, 20, 15, 15],
        )[0]

        if typo_type == 'drop_tone':
            positions = [i for i, c in enumerate(chars) if c.lower() in _VIET_TONE_SWAPS]
            if positions:
                pos = random.choice(positions)
                original = chars[pos]
                replacement = _VIET_TONE_SWAPS.get(original.lower(), original)
                chars[pos] = replacement.upper() if original.isupper() else replacement

        elif typo_type == 'wrong_tone':
            positions = [i for i, c in enumerate(chars) if c.lower() in _TONE_CONFUSIONS]
            if positions:
                pos = random.choice(positions)
                original = chars[pos]
                replacement = _TONE_CONFUSIONS.get(original.lower(), original)
                chars[pos] = replacement.upper() if original.isupper() else replacement

        elif typo_type == 'double_letter':
            positions = [i for i, c in enumerate(chars) if c.isalpha()]
            if positions:
                pos = random.choice(positions)
                chars.insert(pos, chars[pos])

        elif typo_type == 'adjacent_key':
            positions = [i for i, c in enumerate(chars) if c.lower() in _ADJACENT_KEYS]
            if positions:
                pos = random.choice(positions)
                original = chars[pos]
                adj = _ADJACENT_KEYS.get(original.lower(), '')
                if adj:
                    replacement = random.choice(list(adj))
                    chars[pos] = replacement.upper() if original.isupper() else replacement

        elif typo_type == 'swap_chars':
            positions = [i for i in range(len(chars) - 1) if chars[i].isalpha() and chars[i+1].isalpha()]
            if positions:
                pos = random.choice(positions)
                chars[pos], chars[pos + 1] = chars[pos + 1], chars[pos]

    return ''.join(chars)


class TrendsManager:
    """Fetches Vietnamese trending searches from Google Trends RSS."""

    TRENDS_RSS_URL = "https://trends.google.com/trending/rss"
    _CACHE_TTL = 900  # 15 minutes cache

    def __init__(self):
        self.trending_queries: list[str] = []
        self.fallback_topics: list[str] = load_search_topics()
        self._used_queries: set[str] = set()
        self._last_fetch_time: float = 0

    async def fetch_trending(self, geo: str = "VN") -> list[str]:
        """Fetch trending searches from Google Trends RSS (Vietnamese).
        Cached for 15 minutes to avoid redundant requests."""
        import time as _time
        now = _time.time()
        if self.trending_queries and (now - self._last_fetch_time) < self._CACHE_TTL:
            logger.debug(f"Using cached trends ({len(self.trending_queries)} queries, {int(now - self._last_fetch_time)}s old)")
            return self.trending_queries

        queries = []

        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    self.TRENDS_RSS_URL,
                    params={"geo": geo},
                    headers={
                        "User-Agent": (
                            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
                        )
                    },
                )

                if response.status_code == 200:
                    root = ET.fromstring(response.text)
                    ns = {"ht": "https://trends.google.com/trending/rss"}

                    for item in root.findall(".//item"):
                        # Main trending title
                        title_el = item.find("title")
                        if title_el is not None and title_el.text:
                            queries.append(title_el.text.strip())

                        # News item titles (more natural queries)
                        for news in item.findall("ht:news_item", ns):
                            news_title = news.find("ht:news_item_title", ns)
                            if news_title is not None and news_title.text:
                                title = news_title.text.strip()
                                title = title.replace("\u2018", "").replace("\u2019", "")
                                if 5 < len(title) < 80:
                                    queries.append(title)

            if queries:
                self.trending_queries = queries
                self._last_fetch_time = now
                logger.info(f"Fetched {len(queries)} trending queries (VN)")
            else:
                logger.warning("No trending queries found, using fallback")

        except Exception as e:
            logger.warning(f"Failed to fetch Google Trends: {e}. Using fallback topics.")

        return queries

    def get_random_query(self) -> str:
        """Get a random search query (Vietnamese trending + fallback mix).
        ~15% chance of having typos for human-like behavior.
        """
        pool = []

        # Vietnamese fallback topics get priority
        pool.extend(self.fallback_topics)

        if self.trending_queries:
            pool.extend(self.trending_queries)

        # Filter out already used queries
        available = [q for q in pool if q not in self._used_queries]

        if len(available) < 10:
            self._used_queries.clear()
            available = pool

        query = random.choice(available)
        self._used_queries.add(query)

        # Occasionally modify the query (Vietnamese-style)
        if random.random() < 0.25:
            query = self._modify_query(query)

        # ~15% chance of typos (realistic keyboard mistakes)
        if random.random() < 0.15:
            query = _add_vietnamese_typo(query)

        return query

    def _modify_query(self, query: str) -> str:
        """Add natural Vietnamese variation to a query."""
        modifications = [
            lambda q: f"{q} ở đâu",
            lambda q: f"cách {q}",
            lambda q: f"{q} giá bao nhiêu",
            lambda q: f"{q} 2026",
            lambda q: f"{q} có tốt không",
            lambda q: f"{q} nên mua loại nào",
            lambda q: f"{q} cho người mới",
            lambda q: f"mua {q} ở đâu",
            lambda q: f"{q} review",
            lambda q: f"top {q} tốt nhất",
            lambda q: f"so sánh {q}",
            lambda q: f"{q} mới nhất",
        ]

        modifier = random.choice(modifications)
        modified = modifier(query)

        if len(modified) > 80:
            return query

        return modified

    def get_batch_queries(self, count: int) -> list[str]:
        """Get a batch of queries with topic clustering.
        
        Real users search in clusters: 3-5 related queries about a topic,
        then switch to a different topic. This simulates that pattern.
        """
        queries = []
        cluster_size = 0
        current_topic = ""

        while len(queries) < count:
            # Start a new cluster every 3-5 queries
            if cluster_size <= 0:
                current_topic = self.get_random_query()
                queries.append(current_topic)
                cluster_size = random.randint(2, 4)  # 2-4 related follow-ups
            else:
                # Generate a related query based on current topic
                related = self._generate_related(current_topic)
                queries.append(related)
                cluster_size -= 1

        return queries[:count]

    def _generate_related(self, topic: str) -> str:
        """Generate a related search query for clustering."""
        # Clean topic: remove Vietnamese modifiers if present
        clean = topic
        for prefix in ["cách ", "mua ", "top ", "so sánh "]:
            if clean.startswith(prefix):
                clean = clean[len(prefix):]
        for suffix in [" ở đâu", " giá bao nhiêu", " 2026", " mới nhất",
                       " có tốt không", " review", " cho người mới"]:
            if clean.endswith(suffix):
                clean = clean[:-len(suffix)]

        variations = [
            f"{clean} review",
            f"{clean} giá bao nhiêu",
            f"{clean} 2026",
            f"{clean} có tốt không",
            f"{clean} mới nhất",
            f"{clean} ở đâu",
            f"so sánh {clean}",
            f"top {clean}",
            f"{clean} vs",
            f"{clean} hướng dẫn",
            f"{clean} kinh nghiệm",
            f"{clean} nên hay không",
        ]

        query = random.choice(variations)

        # ~15% chance of typo
        if random.random() < 0.15:
            query = _add_vietnamese_typo(query)

        return query

    def reset(self) -> None:
        """Reset used queries tracker."""
        self._used_queries.clear()
