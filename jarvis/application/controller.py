from __future__ import annotations

import asyncio
import logging
from typing import Any

from jarvis.config.settings import Settings
from jarvis.core.intent_classifier import IntentClassifier
from jarvis.monitoring import JarvisHealthService
from jarvis.runtime.planner import ExecutionPlanner
from jarvis.runtime.execution_types import (
    ExecutionCommand,
    ExecutionIntent,
    NoOpIntent,
    OpenIntent,
    OrchestratorStepIntent,
    PlayIntent,
    SearchIntent,
)


class JarvisApplication:
    def __init__(
        self,
        orchestrator,
        settings: Settings | None = None,
        health_service: JarvisHealthService | None = None,
        execution_planner: ExecutionPlanner | None = None,
        intent_classifier: IntentClassifier | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._settings = settings
        self._health_service = health_service
        self.execution_planner = execution_planner
        self.intent_classifier = intent_classifier or IntentClassifier()
        self._fallback_last_domain: str | None = None
        self._active_requests: set[str] = set()
        self._cancelled_ids: set[int | str] = set()
        # Issue 1 fix: explicit video URL history stack (avoids page.go_back() landing on non-video pages)
        self._video_history: list[str] = []
        self._execution_engine_delegate = None

        try:
            setattr(self._orchestrator, "controller", self)
        except Exception:
            pass

    @property
    def orchestrator(self):
        return self._orchestrator

    @property
    def controller(self):
        return self

    @property
    def settings(self) -> Settings | None:
        return self._settings

    @property
    def _last_domain(self) -> str | None:
        try:
            page = self.get_active_page()
            if page and getattr(page, "url", ""):
                return self._normalize_domain(page.url)
        except Exception:
            pass
        return self._fallback_last_domain

    @_last_domain.setter
    def _last_domain(self, val: str | None):
        self._fallback_last_domain = val

    def subscribe_runtime_event(self, event_name: str, callback) -> None:
        event_bus = getattr(self.orchestrator, "event_bus", getattr(self.orchestrator, "_event_bus", None))
        if event_bus is not None:
            event_bus.subscribe(event_name, callback)

    def unsubscribe_runtime_event(self, event_name: str, callback) -> None:
        event_bus = getattr(self.orchestrator, "event_bus", getattr(self.orchestrator, "_event_bus", None))
        if event_bus is not None:
            event_bus.unsubscribe(event_name, callback)

    def begin_execution(self, request_id: int | str | None = None) -> None:
        cancellation_controller = getattr(self.orchestrator, "cancellation_controller", getattr(self.orchestrator, "_cancellation_controller", None))
        if cancellation_controller is not None:
            cancellation_controller.begin(request_id)

    def cancel_active(self, request_id: int | str | None = None) -> bool:
        if request_id is not None:
            for key in self._request_keys(request_id):
                self._cancelled_ids.add(key)
        cancellation_controller = getattr(self.orchestrator, "cancellation_controller", getattr(self.orchestrator, "_cancellation_controller", None))
        cancelled = False
        if cancellation_controller is not None:
            cancelled = cancellation_controller.cancel(request_id)
        self.orchestrator.interrupt(request_id=request_id)
        return cancelled

    def finish_execution(self, request_id: int | str | None = None) -> None:
        self.orchestrator.finish_request(request_id)

    def attach_execution_engine(self, execution_engine) -> None:
        self._execution_engine_delegate = execution_engine

    def start_request(self, request_id: int | str | None = None) -> bool:
        req_id_str = str(request_id or "").strip()
        if req_id_str and req_id_str in self._active_requests:
            return False
        if req_id_str:
            self._active_requests.add(req_id_str)
        return True

    def end_request(self, request_id: int | str | None = None) -> None:
        req_id_str = str(request_id or "").strip()
        if req_id_str and req_id_str in self._active_requests:
            self._active_requests.remove(req_id_str)
        self._clear_cancelled(request_id)

    def handle_text(
        self,
        user_input: ExecutionCommand | Any,
        request_id: int | str | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop is not None and loop.is_running():
            return asyncio.run_coroutine_threadsafe(self.handle_text_async(user_input, request_id=request_id), loop).result()
        return asyncio.run(self.handle_text_async(user_input, request_id=request_id))

    async def handle_text_async(
        self,
        user_input: ExecutionCommand | Any,
        request_id: int | str | None = None,
    ) -> tuple[str, dict[str, object] | None]:
        command = self._coerce_execution_command(user_input)
        engine = self._execution_engine_delegate
        if engine is None:
            from jarvis.runtime.execution_engine import ExecutionEngine

            engine = ExecutionEngine(
                self,
                timeout_seconds=30.0,
                logger=logging.getLogger("Jarvis.RuntimeExecution"),
                planner=self.execution_planner,
                classifier=self.intent_classifier,
            )
        return await engine.execute_application_command(command, request_id=request_id)

    @staticmethod
    def _coerce_execution_command(user_input: ExecutionCommand | Any) -> ExecutionCommand:
        if isinstance(user_input, ExecutionCommand):
            return user_input
        if isinstance(user_input, str):
            return ExecutionCommand.for_text(user_input)
        raise TypeError(f"Unsupported execution payload: {type(user_input).__name__}")

    async def execute_intent(self, intent: ExecutionIntent, request_id: int | str | None = None) -> str | None:
        return await self._execute_intent(intent, request_id)

    async def record_execution_result(
        self,
        *,
        source_text: str,
        results: list[str],
        last_intent: ExecutionIntent | None,
        source: str,
        request_id: int | str | None = None,
    ) -> None:
        if not hasattr(self.orchestrator, "memory") and not hasattr(self.orchestrator, "_memory"):
            return
        success = not any("FAILED" in r for r in results)
        action, query, action_type = self._intent_memory_parts(last_intent)
        media_state = await self._resolve_media_state(action, action_type)
        memory = getattr(self.orchestrator, "memory", getattr(self.orchestrator, "_memory", None))
        if memory:
            memory.add_interaction(
            source_text,
            " | ".join(results),
            metadata=self._build_memory_metadata(
                action=action,
                query=query,
                domain=self._last_domain,
                media_state=media_state,
                source=source,
                request_id=request_id,
                success=success,
            ),
        )

    @staticmethod
    def _intent_memory_parts(intent: ExecutionIntent | None) -> tuple[str, str, str | None]:
        if intent is None:
            return "none", "", None
        if isinstance(intent, OpenIntent):
            return "open", intent.target, None
        if isinstance(intent, SearchIntent):
            return "search", intent.query, None
        if isinstance(intent, PlayIntent):
            return "play", intent.query, intent.action_type
        if isinstance(intent, NoOpIntent):
            return "no_op", intent.message, None
        return "orchestrator_execute", getattr(intent.step_obj, "target", "") or "", None

    @staticmethod
    def _request_keys(request_id: int | str | None) -> tuple[int | str, ...]:
        if request_id is None:
            return ()
        req_id_str = str(request_id).strip()
        if req_id_str and req_id_str != request_id:
            return (request_id, req_id_str)
        return (request_id,)

    def _clear_cancelled(self, request_id: int | str | None) -> None:
        for key in self._request_keys(request_id):
            self._cancelled_ids.discard(key)

    def _is_cancelled(self, request_id: int | str | None) -> bool:
        return any(key in self._cancelled_ids for key in self._request_keys(request_id))

    @staticmethod
    def _normalize_domain(url: str | None) -> str | None:
        if not url:
            return None
        try:
            import urllib.parse

            return urllib.parse.urlparse(url).netloc.lower().replace("www.", "")
        except Exception:
            return None

    def get_active_page(self):
        browser = getattr(self.orchestrator, "browser_controller", None)
        if browser is None:
            return None
        getter = getattr(browser, "get_active_page", None)
        if callable(getter):
            try:
                return getter()
            except Exception:
                return None
        return getattr(browser, "page", None)

    async def _resolve_media_state(self, action: str, intent_action_type: str | None) -> str:
        if intent_action_type == "pause":
            return "paused"
        if intent_action_type in ("resume", "replay", "next", "previous", "search_and_play"):
            return "playing"
        if action != "play":
            return "unknown"
        try:
            page = self.get_active_page()
            if page and "youtube.com/watch" in (page.url or ""):
                js = "(function(){ const v=document.querySelector('video'); if(!v) return 'unknown'; if(v.ended) return 'ended'; if(v.paused) return 'paused'; if(v.readyState < 3) return 'buffering'; return 'playing'; })()"
                state = await page.evaluate(js)
                return str(state) if state else "playing"
        except Exception:
            return "playing"
        return "playing"

    def _build_memory_metadata(
        self,
        *,
        action: str | None,
        query: str | None,
        domain: str | None,
        media_state: str | None,
        source: str,
        request_id: int | str | None = None,
        success: bool | None = None,
    ) -> dict[str, Any]:
        metadata: dict[str, Any] = {
            "action": str(action or "none"),
            "query": str(query or ""),
            "domain": str(domain or ""),
            "media_state": str(media_state or "unknown"),
            "source": str(source or "controller"),
        }
        if request_id is not None:
            metadata["request_id"] = str(request_id)
        metadata["success"] = bool(success) if success is not None else False
        return metadata

    def _publish_request_complete(
        self,
        request_id: int | str,
        response: str,
        task_snapshot: dict[str, object] | None,
    ) -> None:
        publish_method = getattr(self.orchestrator, "publish_event", getattr(self.orchestrator, "_publish", None))
        if publish_method:
            publish_method(
                "runtime.request_complete",
            {
                "type": "request_complete",
                "request_id": str(request_id),
                "response": response,
                "task_snapshot": task_snapshot,
            },
        )

    def publish_request_complete(
        self,
        request_id: int | str,
        response: str,
        task_snapshot: dict[str, object] | None,
    ) -> None:
        self._publish_request_complete(request_id, response, task_snapshot)

    def publish_request_failed(self, request_id: int | str, error: str) -> None:
        publish_method = getattr(self.orchestrator, "publish_event", getattr(self.orchestrator, "_publish", None))
        if publish_method:
            publish_method(
                "runtime.request_failed",
            {
                "type": "request_failed",
                "request_id": str(request_id),
                "error": error or "FAILED_EXECUTION",
            },
        )

    async def _safe_call(self, coro, label=""):
        try:
            print(f"[TRACE] START {label}", flush=True)
            result = await asyncio.wait_for(coro, timeout=10.0)
            print(f"[TRACE] END {label}", flush=True)
            if hasattr(result, "success") and not bool(getattr(result, "success", False)):
                message = getattr(result, "message", "") or "Execution failed"
                return f"FAILED_EXECUTION: {message}"
            if isinstance(result, dict) and "success" in result and not bool(result.get("success")):
                message = str(result.get("message", "") or "Execution failed")
                return f"FAILED_EXECUTION: {message}"
            return result
        except Exception as e:
            print(f"[ERROR] {label} failed: {e}", flush=True)
            return "FAILED_TIMEOUT"

    async def _safe_navigate(self, url: str, request_id: int | str | None = None) -> str:
        browser = getattr(self.orchestrator, "browser_controller", None)
        if not browser:
            return "FAILED_EXECUTION: Browser unavailable"
            
        for attempt in range(2):
            result = await self._safe_call(browser.open_url(url), "open_url")
            if isinstance(result, str) and result.startswith("FAILED"):
                if result == "FAILED_TIMEOUT":
                    print(f"[Browser] attempt {attempt+1} timed out for {url}", flush=True)
                    if attempt == 1:
                        return "FAILED_TIMEOUT: Browser hung during navigation"
                    await asyncio.sleep(1)
                    continue
                return result
            if result != "FAILED_TIMEOUT":
                return "SUCCESS"
        return "FAILED_TIMEOUT"

    async def _execute_intent(self, intent: ExecutionIntent, request_id: int | str | None = None) -> str | None:
        print("[FIRE] EXECUTE_INTENT CALLED", flush=True)
        print(f"[EXEC] Running intent: {intent}", flush=True)
        
        if isinstance(intent, NoOpIntent):
            return f"SUCCESS: {intent.message}"
            
        if isinstance(intent, OrchestratorStepIntent):
            step_obj = intent.step_obj
            action = getattr(step_obj, "action", "unknown")
            print(f"[EXEC] Delegating native step {action} to ToolExecutor", flush=True)
            executor = getattr(self.orchestrator, "executor", getattr(self.orchestrator, "_executor", None))
            if executor:
                try:
                    from jarvis.core.context import ExecutionPlan
                    plan = ExecutionPlan(intent="delegate", goal=getattr(step_obj, "description", action), steps=[step_obj], confidence=1.0)
                    report = await executor.execute_plan(plan, request_id=request_id)
                    return report.response if report.success else f"FAILED_EXECUTION: {report.response}"
                except Exception as e:
                    return f"FAILED_EXECUTION: {e}"
            return "FAILED_RESOLUTION: Missing executor"
            
        try:
            if isinstance(intent, OpenIntent):
                return await self._handle_open(intent, request_id)
            if isinstance(intent, SearchIntent):
                return await self._handle_search(intent, request_id)
            if isinstance(intent, PlayIntent):
                return await self._handle_play(intent, request_id)
            return "FAILED_RESOLUTION: No handler found for intent"
        except Exception as e:
            print(f"[ERROR] Execution crashed: {e}", flush=True)
            return "FAILED_EXECUTION: Execution crashed"

    async def _handle_open(self, intent: OpenIntent, request_id: int | str | None) -> str | None:
        print("[FIRE] OPEN HANDLER HIT", flush=True)
        target = intent.target.lower().strip()
        if not target: 
            return "FAILED_RESOLUTION: No target provided"

        url = None
        print(f"[OPEN] target={target}", flush=True)

        # 1. Custom sites have top priority and stop resolution.
        try:
            from jarvis.core.custom_sites import get_store, CustomSiteResolver
            resolver = CustomSiteResolver(get_store())
            resolved = resolver.resolve(target)
            if resolved and resolved.get("url"):
                url = str(resolved.get("url")).strip()
                print(f"[OPEN] Using custom site -> {url}", flush=True)
        except Exception as e:
            print(f"[DEBUG] Custom site resolution failed: {e}", flush=True)

        # 2. Built-in sites/direct URLs only run when custom sites did not match.
        if not url:
            from jarvis.config.urls import ALIASES, KNOWN_SITES, normalize_url
            builtin_target = ALIASES.get(target, target)
            if builtin_target in KNOWN_SITES:
                url = KNOWN_SITES[builtin_target]
                print(f"[OPEN] Using builtin site -> {url}", flush=True)
            elif target.startswith(("http://", "https://")) or "." in target:
                try:
                    url = normalize_url(target)
                    print(f"[OPEN] Using direct URL -> {url}", flush=True)
                except ValueError:
                    url = None

        # 3. Fail explicitly. No fallback search and no browser action.
        if not url:
            return f"FAILED_RESOLUTION: Unknown target '{target}'"

        browser = getattr(self.orchestrator, "browser_controller", None)
        if not browser:
            return "FAILED_EXECUTION: Browser unavailable"

        if request_id:
            publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
            if publish_chunks:
                publish_chunks(str(request_id), f"Opening {target}...")

        result = await self._safe_call(browser.open_url(url), "open_url")
        if isinstance(result, str) and result.startswith("FAILED"):
            if result == "FAILED_TIMEOUT":
                return "FAILED_TIMEOUT: open_url failed"
            return result

        if request_id:
            publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
            if publish_chunks:
                publish_chunks(str(request_id), "Done")

        self._last_domain = self._normalize_domain(url)
        return f"SUCCESS: Opened {target}"

    async def _handle_search(self, intent: SearchIntent, request_id: int | str | None) -> str | None:
        print("[TRACE] entering _handle_search", flush=True)
        query = intent.query
        if not query: return "FAILED_RESOLUTION: Empty query"
            
        import urllib.parse
        encoded = urllib.parse.quote_plus(query)
        
        browser = getattr(self.orchestrator, "browser_controller", None)
        context = self._last_domain or ""
        try:
            page = self.get_active_page()
            if page and getattr(page, "url", ""):
                context = self._normalize_domain(page.url) or context
        except Exception: pass
            
        DOMAIN_SEARCH_ROUTING = {
            "github.com": "https://github.com/search?q={query}",
            "youtube.com": "https://www.youtube.com/results?search_query={query}",
            "youtu.be": "https://www.youtube.com/results?search_query={query}",
        }
        
        # Normalize context to handle www.
        normalized_context = context.replace("www.", "")
        url_template = DOMAIN_SEARCH_ROUTING.get(normalized_context, "https://www.google.com/search?q={query}")
        url = url_template.format(query=encoded)
            
        print(f"[Browser] resolved search URL: {url}", flush=True)
        if request_id:
            publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
            if publish_chunks:
                publish_chunks(str(request_id), f"Searching for '{query}'...")
            
        nav_status = await self._safe_navigate(url, request_id)
        if not nav_status.startswith("SUCCESS"): return nav_status
                
        if request_id:
            publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
            if publish_chunks:
                publish_chunks(str(request_id), "Done")
        return f"SUCCESS: Searched for {query}"

    async def _handle_play(self, intent: PlayIntent, request_id: int | str | None) -> str | None:
        print("[TRACE] entering _handle_play", flush=True)
        query = intent.query
        platform = intent.platform
        action_type = intent.action_type or "search_and_play"
        
        browser = getattr(self.orchestrator, "browser_controller", None)
        page = self.get_active_page()
        current_url = page.url if page else ""
        
        # Deep Context Optimization: Click Next or Replay if context says so
        if "youtube.com" in current_url:
            try:
                url = current_url
                is_watch_page = "youtube.com/watch" in url
                
                if action_type in ("pause", "resume", "replay", "next", "previous") and not is_watch_page:
                    # Issue 3 fix: human-friendly UX message instead of raw technical error
                    return "FAILED_EXECUTION: You're not watching a video right now. Open a YouTube video first and then try again."
                
                if action_type == "pause":
                    print("[OPTIMIZATION] Pausing video", flush=True)
                    res = await self._safe_call(page.evaluate("const v = document.querySelector('video'); if(v) v.pause(); else throw new Error('No video');"), "js_pause")
                    if str(res).startswith("FAILED"): return "FAILED_EXECUTION: No video found to pause"
                    if request_id:
                        publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
                        if publish_chunks:
                            publish_chunks(str(request_id), "Done")
                    return "SUCCESS: Paused video"
                    
                if action_type == "resume":
                    print("[OPTIMIZATION] Resuming video", flush=True)
                    res = await self._safe_call(page.evaluate("const v = document.querySelector('video'); if(v) v.play(); else throw new Error('No video');"), "js_play")
                    if str(res).startswith("FAILED"): return "FAILED_EXECUTION: No video found to resume"
                    if request_id:
                        publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
                        if publish_chunks:
                            publish_chunks(str(request_id), "Done")
                    return "SUCCESS: Resumed video"
                    
                if action_type == "previous":
                    print("[OPTIMIZATION] Returning to previous video", flush=True)
                    if request_id:
                        publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
                        if publish_chunks:
                            publish_chunks(str(request_id), "Going back...")
                    # Issue 1 fix: use explicit video history stack instead of page.go_back()
                    # page.go_back() may land on homepage/search, not the actual previous video
                    prev_url = None
                    if len(self._video_history) >= 2:
                        self._video_history.pop()  # remove current
                        prev_url = self._video_history[-1]
                    if prev_url:
                        print(f"[VIDEO HISTORY] Navigating to previous video: {prev_url}", flush=True)
                        await self._safe_call(browser.open_url(prev_url), "open_prev_video")
                    else:
                        print("[VIDEO HISTORY] No history, falling back to page.go_back()", flush=True)
                        await self._safe_call(page.go_back(), "go_back")
                    if request_id:
                        publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
                        if publish_chunks:
                            publish_chunks(str(request_id), "Done")
                    return "SUCCESS: Navigated to previous video"
                
                if action_type == "replay":
                    print("[OPTIMIZATION] Restarting video via JS injection", flush=True)
                    if request_id:
                        publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
                        if publish_chunks:
                            publish_chunks(str(request_id), "Restarting video...")
                    res = await self._safe_call(page.evaluate("const v = document.querySelector('video'); if(v) { v.currentTime = 0; v.play(); } else throw new Error('No video');"), "js_replay")
                    if str(res).startswith("FAILED"): return "FAILED_EXECUTION: No video found to replay"
                    if request_id:
                        publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
                        if publish_chunks:
                            publish_chunks(str(request_id), "Done")
                    return f"SUCCESS: Replayed video"
                    
                if action_type == "next":
                    print("[OPTIMIZATION] Clicking 'Next Video' button directly", flush=True)
                    if request_id:
                        publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
                        if publish_chunks:
                            publish_chunks(str(request_id), "Playing next video...")
                    
                    selectors = [
                        "a.ytp-next-button",
                        "button[aria-label='Next']",
                        "a[aria-label='Next']"
                    ]
                    
                    clicked = False
                    for sel in selectors:
                        next_btn = await self._safe_call(page.query_selector(sel), "query_next_btn")
                        if next_btn and next_btn != "FAILED_TIMEOUT":
                            await self._safe_call(next_btn.click(), "click_next")
                            clicked = True
                            break
                            
                    if clicked:
                        # Record the new video URL in history after next-click navigation settles
                        # Micro 2: retry once if YouTube lag causes same video to reload
                        try:
                            old_url = current_url
                            await asyncio.sleep(0.8)
                            new_url = page.url or ""
                            if new_url == old_url:
                                print("[NEXT] Same URL detected after click — retrying once", flush=True)
                                for sel in selectors:
                                    retry_btn = await self._safe_call(page.query_selector(sel), "retry_next_btn")
                                    if retry_btn and retry_btn != "FAILED_TIMEOUT":
                                        await self._safe_call(retry_btn.click(), "retry_click_next")
                                        break
                                await asyncio.sleep(1.0)
                                new_url = page.url or ""
                            # Micro 1: cap history at 20 entries
                            if "youtube.com/watch" in new_url and (not self._video_history or self._video_history[-1] != new_url):
                                self._video_history.append(new_url)
                                if len(self._video_history) > 20:
                                    self._video_history.pop(0)
                                print(f"[VIDEO HISTORY] Recorded: {new_url} (total={len(self._video_history)})", flush=True)
                        except Exception:
                            pass
                        if request_id:
                            publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
                            if publish_chunks:
                                publish_chunks(str(request_id), "Done")
                        return f"SUCCESS: Played next video"
                    else:
                        return "FAILED_EXECUTION: Could not find next button"
            except Exception as e:
                print(f"[ERROR] Playwright action_type optimization failed: {e}", flush=True)
                if action_type == "next":
                    return "FAILED_EXECUTION: Next button optimization crashed"
                
        # Normal play logic
        if not query: return "FAILED_RESOLUTION: Empty play query"
            
        import urllib.parse
        if platform == "youtube":
            url = f"https://www.youtube.com/results?search_query={urllib.parse.quote_plus(query)}"
        else: return f"FAILED_RESOLUTION: Unsupported platform {platform}"
            
        print(f"[Browser] resolved play URL: {url}", flush=True)
        if request_id:
            publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
            if publish_chunks:
                publish_chunks(str(request_id), f"Playing {query} on {platform}...")
            
        active_page = self.get_active_page()
        current_url_check = active_page.url if active_page else ""
        if "youtube.com" in current_url_check:
            print("[OPTIMIZATION] Skipping navigation", flush=True)
            nav_status = "SUCCESS"
        else:
            nav_status = await self._safe_navigate(url, request_id)
            
        if not nav_status.startswith("SUCCESS"): return nav_status
                
        if browser:
            try:
                page = self.get_active_page()
                if page:
                    if await self._safe_call(page.wait_for_load_state("domcontentloaded"), "dom_ready") == "FAILED_TIMEOUT":
                        return "FAILED_TIMEOUT: DOM ready hung"
                        
                    if await self._safe_call(page.wait_for_load_state("networkidle"), "network_idle") == "FAILED_TIMEOUT":
                        return "FAILED_TIMEOUT: Network idle hung"
                    
                    if self._is_cancelled(request_id):
                        print(f"[EXEC] Playwright interaction cancelled request_id={request_id}", flush=True)
                        self._clear_cancelled(request_id)
                        return "FAILED_CANCELLED"
                        
                    for sel in ["ytd-video-renderer", "ytd-rich-item-renderer", "a#video-title"]:
                        el = await self._safe_call(page.query_selector(sel), "query_selector")
                        if el and el != "FAILED_TIMEOUT":
                            if await self._safe_call(el.click(), "click") == "FAILED_TIMEOUT":
                                return "FAILED_TIMEOUT: Click hung"
                            # Record the video URL into history once navigation settles
                            # Micro 1: cap history at 20 entries
                            try:
                                await asyncio.sleep(1.2)
                                landed_url = page.url or ""
                                if "youtube.com/watch" in landed_url and (not self._video_history or self._video_history[-1] != landed_url):
                                    self._video_history.append(landed_url)
                                    if len(self._video_history) > 20:
                                        self._video_history.pop(0)
                                    print(f"[VIDEO HISTORY] Recorded: {landed_url} (total={len(self._video_history)})", flush=True)
                            except Exception:
                                pass
                            break
            except Exception as e:
                print(f"[ERROR] Playwright interaction failed: {e}", flush=True)
            
        if request_id:
            publish_chunks = getattr(self.orchestrator, "publish_response_chunks", getattr(self.orchestrator, "_publish_response_chunks", None))
            if publish_chunks:
                publish_chunks(str(request_id), "Done")
        return f"SUCCESS: Played {query}"

    def poll_notifications(self) -> list[str]:
        return self.orchestrator.poll_notifications()

    def health_snapshot(self) -> dict[str, object]:
        if self._health_service is None:
            return {"status": "unknown"}
        return self._health_service.snapshot(application=self)

    async def health_snapshot_async(self) -> dict[str, object]:
        if self._health_service is None:
            return {"status": "unknown"}
        return await self._health_service.snapshot_async(application=self)

    def shutdown(self) -> None:
        if hasattr(self.orchestrator, "shutdown"):
            self.orchestrator.shutdown()


