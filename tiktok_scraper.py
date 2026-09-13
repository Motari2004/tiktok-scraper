#!/usr/bin/env python3
"""TikTok scraper using TikTokApi with Playwright."""

import asyncio
import json
import logging
import os
from pathlib import Path

from TikTokApi import TikTokApi

logger = logging.getLogger(__name__)

# Path to msToken file
MS_TOKEN_FILE = Path("ms_token.txt")


def _load_ms_token() -> str:
    """Load msToken from file or environment variable."""
    # Prefer environment variable
    token = os.environ.get("ms_token") or os.environ.get("MS_TOKEN")
    if token:
        logger.info("Loaded msToken from environment")
        return token.strip()

    # Fall back to file
    if MS_TOKEN_FILE.exists():
        token = MS_TOKEN_FILE.read_text(encoding="utf-8").strip()
        if token:
            logger.info("Loaded msToken from ms_token.txt")
            return token

    logger.warning("No msToken found - requests may be blocked")
    return None


class TikTokScraper:
    def __init__(self, cookie: str = None):
        self.cookie = cookie
        self.ms_token = _load_ms_token()

    async def _fetch_user_videos(self, username: str, limit: int = 30):
        """Fetch videos from a user account using Playwright."""
        async with TikTokApi() as api:
            # Create sessions with msToken (critical for avoiding blocks)
            await api.create_sessions(
                ms_tokens=[self.ms_token] if self.ms_token else None,
                num_sessions=1,
                sleep_after=3,
                browser="chromium",
            )

            videos = []
            user = api.user(username=username)

            try:
                async for video in user.videos(count=limit):
                    video_data = video.as_dict
                    videos.append(video_data)
                    logger.info(f"Fetched video {len(videos)}/{limit}: {video.id}")
            except Exception as e:
                logger.error(f"Error iterating videos: {e}")
                raise

            return videos

    def get_user_posts(self, username: str, limit: int = 30):
        """Synchronous wrapper for Flask compatibility."""
        try:
            videos = asyncio.run(self._fetch_user_videos(username, limit))

            return {
                "status": "success",
                "username": username,
                "count": len(videos),
                "videos": videos,
            }

        except Exception as e:
            logger.exception("Scraping failed")
            return {
                "status": "error",
                "message": str(e),
            }

    async def _fetch_user_profile(self, username: str):
        """Fetch user profile info."""
        async with TikTokApi() as api:
            await api.create_sessions(
                ms_tokens=[self.ms_token] if self.ms_token else None,
                num_sessions=1,
                sleep_after=3,
                browser="chromium",
            )

            user = api.user(username=username)
            info = await user.info()
            return info

    def get_user_profile(self, username: str):
        """Synchronous wrapper for profile fetching."""
        try:
            info = asyncio.run(self._fetch_user_profile(username))
            return {"status": "success", "data": info}
        except Exception as e:
            logger.exception("Profile fetch failed")
            return {"status": "error", "message": str(e)}