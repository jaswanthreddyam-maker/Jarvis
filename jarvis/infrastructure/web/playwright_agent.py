import asyncio
import os
from pathlib import Path
from typing import Any

class PlaywrightAgent:
    def __init__(self):
        self._pw = None
        self.browser = None
        self.context = None

    async def start(self):
        from playwright.async_api import async_playwright
        self._pw = await async_playwright().start()
        
        user_data_dir = os.path.join(Path.home(), ".jarvis", "browser_profile")
        os.makedirs(user_data_dir, exist_ok=True)
        
        self.context = await self._pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=False,
            viewport={"width": 1280, "height": 720},
            args=["--disable-blink-features=AutomationControlled"]
        )

    async def new_page(self):
        if not self.context:
            await self.start()
        try:
            # launch_persistent_context automatically creates one page
            if len(self.context.pages) > 0 and self.context.pages[0].url == "about:blank":
                return self.context.pages[0]
            return await self.context.new_page()
        except Exception:
            # Context may have died (all pages closed). Restart.
            await self.start()
            if len(self.context.pages) > 0 and self.context.pages[0].url == "about:blank":
                return self.context.pages[0]
            return await self.context.new_page()
        
    async def get_visible_page(self):
        """Find the page the user is currently looking at."""
        if not self.context:
            return None
        for page in reversed(self.context.pages):
            try:
                state = await page.evaluate("document.visibilityState")
                if state == "visible":
                    return page
            except Exception:
                continue
        return None
