import asyncio
import pytest
import pytest_asyncio
from jarvis.infrastructure.web.controller import BrowserController

# Note: Before running these tests, ensure you have Playwright installed.
# python -m pip install playwright pytest pytest-asyncio
# playwright install chromium

# Configure pytest-asyncio to use a single event loop for the entire module
pytestmark = pytest.mark.asyncio(loop_scope="module")

_controller_instance = None

@pytest_asyncio.fixture(loop_scope="module", scope="module")
async def controller():
    global _controller_instance
    bc = BrowserController()
    await bc.start_backend()
    _controller_instance = bc
    yield bc
    # Teardown logic
    if bc._backend.context:
        await bc._backend.context.close()
    if hasattr(bc._backend, "_pw") and bc._backend._pw:
        await bc._backend._pw.stop()


async def test_youtube_search_and_play(controller):
    r1 = await controller.search_youtube("phonk music")
    assert r1.success is True
    assert "youtube" in r1.data["domain"]

    r2 = await controller.play_first_video()
    assert r2.success is True
    url = r2.data["url"]
    print(f"\n[DEBUG] play_first_video URL: {url}")
    print(f"[DEBUG] play_first_video outcome: {r2.data.get('outcome')}")
    print(f"[DEBUG] play_first_video telemetry: {r2.data.get('telemetry')}")
    assert "youtube.com" in url
    assert r2.data["outcome"] in ["navigated", "intercepted"]

    telemetry = r2.data["telemetry"]
    assert telemetry["duration_ms"] > 0
    assert telemetry["retries"] >= 0
    assert isinstance(telemetry["used_fallback"], bool)


async def test_tab_reuse(controller):
    r1 = await controller.search_youtube("lofi")
    tab1 = controller.get_active_tab()

    r2 = await controller.search_youtube("jazz")
    tab2 = controller.get_active_tab()

    # The controller should detect the domain match and reuse the active tab
    assert tab1 == tab2


async def test_ensure_page_recovery_after_manual_close(controller):
    # Simulate user manually closing the tab
    r1 = await controller.search_youtube("cyberpunk ambiance")
    tab_id = r1.data["tab_id"]

    # Close physical page
    page = controller._pages[tab_id]["page"]
    await page.close()

    # Try an action, Controller should realize it's dead, spin up a new page, and register it
    r2 = await controller.play_youtube("cyberpunk ambiance")
    assert r2.success is True
    assert r2.data["tab_id"] != tab_id  # Should be a new tab since the old one was killed
    assert controller._pages[r2.data["tab_id"]]["page"].is_closed() is False


async def test_scroll_page(controller):
    r1 = await controller.open_url("https://en.wikipedia.org/wiki/Main_Page")
    assert r1.success is True

    r2 = await controller.scroll_page(direction="down")
    assert r2.success is True

    # Check DOM telemetry if available
    assert "wikipedia" in r2.data["domain"]
    assert "telemetry" in r2.data
    assert r2.data["outcome"] == "scrolled"
