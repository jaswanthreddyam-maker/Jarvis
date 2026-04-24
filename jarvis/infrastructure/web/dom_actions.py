import asyncio
from typing import Any

DEFAULT_TIMEOUT = 5000

class DOMActions:
    """Generic DOM interaction layer with fallback strategies and strict policies."""
    
    @staticmethod
    async def get_safe_locator(page: Any, selector: str):
        locator = page.locator(selector)
        count = await locator.count()
        if count == 0:
            raise Exception(f"No elements found for selector: {selector}")
        return locator.first

    @staticmethod
    async def click(page: Any, selector: str, timeout: int = DEFAULT_TIMEOUT, retries: int = 2) -> dict[str, Any]:
        import time
        start = time.time()
        for i in range(retries):
            try:
                locator = await DOMActions.get_safe_locator(page, selector)
                await locator.scroll_into_view_if_needed(timeout=timeout)
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.click(timeout=timeout)
                return {"selector": selector, "retries": i, "used_fallback": False, "duration_ms": int((time.time() - start) * 1000)}
            except Exception:
                await asyncio.sleep(0.3)
                
        # Final Fallback: force click via Playwright's locator JS evaluation
        try:
            locator = await DOMActions.get_safe_locator(page, selector)
            await locator.evaluate("el => el.click()")
            return {"selector": selector, "retries": retries, "used_fallback": True, "duration_ms": int((time.time() - start) * 1000)}
        except Exception as e:
            raise Exception(f"Failed to click {selector}: {e}")

    @staticmethod
    async def type(page: Any, selector: str, text: str, timeout: int = DEFAULT_TIMEOUT, retries: int = 2) -> dict[str, Any]:
        import time
        start = time.time()
        for i in range(retries):
            try:
                locator = await DOMActions.get_safe_locator(page, selector)
                await locator.scroll_into_view_if_needed(timeout=timeout)
                await locator.wait_for(state="visible", timeout=timeout)
                await locator.fill(text, timeout=timeout)
                return {"selector": selector, "retries": i, "used_fallback": False, "duration_ms": int((time.time() - start) * 1000)}
            except Exception:
                await asyncio.sleep(0.3)
                
        # Final Fallback: force value injection via JS safely
        try:
            locator = await DOMActions.get_safe_locator(page, selector)
            await locator.evaluate("(el, txt) => { el.value = txt; el.dispatchEvent(new Event('input', { bubbles: true })); }", text)
            return {"selector": selector, "retries": retries, "used_fallback": True, "duration_ms": int((time.time() - start) * 1000)}
        except Exception as e:
            raise Exception(f"Failed to type in {selector}: {e}")

    @staticmethod
    async def scroll(page: Any, direction: str = "down", amount: int | None = None) -> dict[str, Any]:
        import time
        start = time.time()
        amount_str = "window.innerHeight" if amount is None else str(amount)
        if direction == "down":
            await page.evaluate(f"window.scrollBy(0, {amount_str})")
        elif direction == "up":
            await page.evaluate(f"window.scrollBy(0, -{amount_str})")
        return {"selector": "window", "retries": 0, "used_fallback": False, "duration_ms": int((time.time() - start) * 1000)}
