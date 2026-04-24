import asyncio
import uuid
from urllib.parse import urlparse, quote_plus
from typing import Any

from jarvis.core.tools import ToolResult
from jarvis.infrastructure.web.playwright_agent import PlaywrightAgent
from jarvis.infrastructure.web.dom_actions import DOMActions

class BrowserController:
    """Stateful control layer for the browser."""
    
    def __init__(self):
        self._pages: dict[str, dict[str, Any]] = {}
        self._history: dict[str, list[str]] = {}
        self._active_tab: str | None = None
        self._backend = PlaywrightAgent()

    async def start_backend(self):
        await self._backend.start()

    @staticmethod
    def normalize_domain(url: str) -> str | None:
        try:
            return urlparse(url).netloc.replace("www.", "")
        except Exception:
            return None

    def _register_tab(self, url: str, page: Any) -> str:
        tab_id = str(uuid.uuid4())
        self._pages[tab_id] = {
            "page": page,
            "url": url,
            "domain": self.normalize_domain(url)
        }
        self._history[tab_id] = [url]
        self._active_tab = tab_id
        return tab_id

    def _update_tab(self, tab_id: str, url: str) -> None:
        if tab_id in self._pages:
            self._pages[tab_id]["url"] = url
            self._pages[tab_id]["domain"] = self.normalize_domain(url)
            if tab_id not in self._history:
                self._history[tab_id] = []
            self._history[tab_id].append(url)
            self._active_tab = tab_id

    def resolve_tab(self, domain: str | None) -> str | None:
        if not domain:
            return self._active_tab
        if self._active_tab and domain in self._pages.get(self._active_tab, {}).get("domain", ""):
            return self._active_tab
        for tab_id, meta in self._pages.items():
            if domain in meta.get("domain", ""):
                return tab_id
        return None

    async def ensure_page(self, tab_id: str | None, url: str):
        if tab_id and tab_id in self._pages:
            page = self._pages[tab_id].get("page")
            if page and not page.is_closed():
                await page.bring_to_front()
                return page, tab_id

        # Recreate page cleanly if dead or missing
        page = await self._backend.new_page()
        await page.bring_to_front()
        new_tab_id = self._register_tab(url, page)
        return page, new_tab_id

    def get_active_domain(self) -> str | None:
        if not self._active_tab or self._active_tab not in self._pages:
            return None
        return self._pages[self._active_tab].get("domain")

    def get_active_tab(self) -> str | None:
        return self._active_tab

    @property
    def open_tabs_count(self) -> int:
        return len(self._pages)

    async def sync_active_tab(self):
        """Finds visually active tab if user manually switched."""
        page = await self._backend.get_visible_page()
        if page:
            for tab_id, meta in self._pages.items():
                if meta.get("page") == page:
                    self._active_tab = tab_id
                    break

    async def open_url(self, url: str, browser_app: str | None = None) -> ToolResult:
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        await self.sync_active_tab()
        
        # Always open a new tab for explicit open_url
        page = await self._backend.new_page()
        await page.bring_to_front()
        tab_id = self._register_tab(url, page)

        try:
            await page.goto(url)
            self._update_tab(tab_id, page.url)
            domain = self.get_active_domain()
            return ToolResult(
                success=True,
                message=f"Opened {url}.",
                data={
                    "url": page.url,
                    "domain": domain or "",
                    "tab_id": tab_id,
                    "open_tabs_count": self.open_tabs_count,
                    "browser_app": browser_app or ""
                }
            )
        except Exception as e:
            return ToolResult(success=False, message=str(e), error="browser_launch_failed")

    async def search_youtube(self, query: str, browser_app: str | None = None) -> ToolResult:
        search_url = f"https://www.youtube.com/results?search_query={quote_plus(query.strip())}"
        
        await self.sync_active_tab()
        tab_id = self.resolve_tab("youtube")
        action_desc = "Navigated to" if tab_id else "Opened new tab for"
        
        page, tab_id = await self.ensure_page(tab_id, search_url)

        try:
            await page.goto("https://youtube.com")
            await page.wait_for_load_state("networkidle")
            
            # Resilient Selection — YouTube may render search as input#search or a combobox
            search_selector = "input#search, input[name='search_query']"
            telemetry = await DOMActions.type(page, search_selector, query)
            await page.locator(search_selector).first.press("Enter")
            
            # Wait for navigation
            await page.wait_for_load_state("domcontentloaded")
            
            self._update_tab(tab_id, page.url)
            return ToolResult(
                success=True,
                message=f"{action_desc} YouTube search: '{query.strip()}'.",
                data={
                    "url": page.url,
                    "query": query.strip(),
                    "domain": "youtube",
                    "tab_id": tab_id,
                    "open_tabs_count": self.open_tabs_count,
                    "browser_app": browser_app or "",
                    "telemetry": telemetry,
                    "outcome": "navigated"
                }
            )
        except Exception as e:
            return ToolResult(success=False, message=str(e), error="browser_launch_failed")

    async def search_web(self, query: str, browser_app: str | None = None) -> ToolResult:
        search_url = f"https://www.google.com/search?q={quote_plus(query.strip())}"
        
        await self.sync_active_tab()
        tab_id = self.resolve_tab("google")
        action_desc = "Navigated to" if tab_id else "Opened new tab for"
        
        page, tab_id = await self.ensure_page(tab_id, search_url)

        try:
            await page.goto("https://google.com")
            search_box = page.get_by_role("combobox")
            if await search_box.count() == 0:
                search_box = page.locator("textarea[name='q']").first
                
            await search_box.wait_for(state="visible", timeout=5000)
            await search_box.fill(query)
            await search_box.press("Enter")
            telemetry = {"selector": "textarea[name='q']", "retries": 0, "used_fallback": False, "duration_ms": 0}
            
            await page.wait_for_load_state("domcontentloaded")
            
            self._update_tab(tab_id, page.url)
            return ToolResult(
                success=True,
                message=f"{action_desc} web search: '{query.strip()}'.",
                data={
                    "url": page.url,
                    "query": query.strip(),
                    "domain": "google",
                    "tab_id": tab_id,
                    "open_tabs_count": self.open_tabs_count,
                    "browser_app": browser_app or "",
                    "telemetry": telemetry,
                    "outcome": "navigated"
                }
            )
        except Exception as e:
            return ToolResult(success=False, message=str(e), error="browser_launch_failed")

    async def play_youtube(self, query: str, video_url: str | None = None, browser_app: str | None = None) -> ToolResult:
        target_url = video_url or f"https://www.youtube.com/results?search_query={quote_plus(query.strip())}"
        
        await self.sync_active_tab()
        tab_id = self.resolve_tab("youtube")
        action_desc = "Navigated to" if tab_id else "Opened new tab for"
        
        page, tab_id = await self.ensure_page(tab_id, target_url)

        try:
            await page.goto(target_url)
            await page.wait_for_load_state("domcontentloaded")
            
            self._update_tab(tab_id, page.url)
            return ToolResult(
                success=True,
                message=f"{action_desc} YouTube playback: '{query.strip()}'.",
                data={
                    "url": page.url,
                    "query": query.strip(),
                    "domain": "youtube",
                    "tab_id": tab_id,
                    "open_tabs_count": self.open_tabs_count,
                    "browser_app": browser_app or ""
                }
            )
        except Exception as e:
            return ToolResult(success=False, message=str(e), error="browser_launch_failed")

    async def play_first_video(self) -> ToolResult:
        await self.sync_active_tab()
        tab_id = self.resolve_tab("youtube")
        
        if not tab_id:
            return ToolResult(success=False, message="No YouTube tab is currently active.", error="no_youtube_tab")
            
        page, _ = await self.ensure_page(tab_id, "")
        
        try:
            # ytd-video-renderer (search results) or ytd-rich-item-renderer (homepage)
            selector = "ytd-video-renderer, ytd-rich-item-renderer"
            await page.wait_for_selector(selector, state="visible", timeout=5000)
            
            # Click the thumbnail link inside the first visible renderer for reliability
            thumbnail_selector = f"{selector} a#thumbnail"
            url_before = page.url
            telemetry = await DOMActions.click(page, thumbnail_selector)
            
            # Wait for URL to actually change (YouTube SPA navigation)
            try:
                await page.wait_for_url(lambda u: u != url_before, timeout=5000)
            except Exception:
                pass  # URL may not change if it opened in the same view
            await page.wait_for_load_state("domcontentloaded")
            self._update_tab(tab_id, page.url)
            
            outcome = "intercepted" if "watch?v=" not in page.url else "navigated"
            
            return ToolResult(
                success=True,
                message="Playing the first video.",
                data={
                    "url": page.url,
                    "domain": "youtube",
                    "tab_id": tab_id,
                    "open_tabs_count": self.open_tabs_count,
                    "telemetry": telemetry,
                    "outcome": outcome
                }
            )
        except Exception as e:
            return ToolResult(success=False, message=str(e), error="dom_interaction_failed")

    async def scroll_page(self, direction: str = "down") -> ToolResult:
        await self.sync_active_tab()
        tab_id = self._active_tab
        if not tab_id:
            return ToolResult(success=False, message="No active tab to scroll.", error="no_active_tab")
            
        page, _ = await self.ensure_page(tab_id, "")
        try:
            telemetry = await DOMActions.scroll(page, direction=direction)
            return ToolResult(
                success=True,
                message=f"Scrolled {direction}.",
                data={
                    "url": page.url,
                    "domain": self.get_active_domain() or "",
                    "tab_id": tab_id,
                    "open_tabs_count": self.open_tabs_count,
                    "telemetry": telemetry,
                    "outcome": "scrolled"
                }
            )
        except Exception as e:
            return ToolResult(success=False, message=str(e), error="dom_interaction_failed")
