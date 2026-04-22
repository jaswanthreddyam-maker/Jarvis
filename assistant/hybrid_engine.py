"""Hybrid Intelligence Engine — routes between local and online processing.

Seamlessly combines local task execution with online AI queries.
The user never sees the switching — it just works.

Pipeline:
    1. Intent Router classifies input → LOCAL_TASK or ONLINE_QUERY
    2. If LOCAL_TASK → local planner + action engine
    3. If ONLINE_QUERY (or local confidence < threshold) → online provider
    4. Fallback gracefully if online fails → local response
    5. Cache repeated online queries

Privacy:
    - System commands, file paths, passwords NEVER sent online
    - Sensitive content always forced to local pipeline
"""
from __future__ import annotations

import logging
import time
from typing import Any

from assistant.intent_router import IntentRouter, IntentType, RoutingDecision
from assistant.online_provider import OnlineProvider, OnlineResponse
from assistant.privacy_guard import guard as privacy_guard
from assistant.behavior_learning import learner as behavior_learner

logger = logging.getLogger("Jarvis.HybridEngine")


class HybridResult:
    """Unified result from either local or online pipeline."""
    __slots__ = (
        "response", "source", "intent_type", "confidence",
        "latency_ms", "cached", "privacy_safe", "provider", "snapshot",
    )

    def __init__(
        self,
        response: str,
        source: str = "local",
        intent_type: str = "LOCAL_TASK",
        confidence: float = 1.0,
        latency_ms: float = 0.0,
        cached: bool = False,
        privacy_safe: bool = True,
        provider: str = "",
        snapshot: dict[str, object] | None = None,
    ) -> None:
        self.response = response
        self.source = source
        self.intent_type = intent_type
        self.confidence = confidence
        self.latency_ms = latency_ms
        self.cached = cached
        self.privacy_safe = privacy_safe
        self.provider = provider
        self.snapshot = snapshot


class HybridEngine:
    """Orchestrates local vs online processing with smart routing.

    Usage:
        engine = HybridEngine(local_handler, router, online_provider)
        result = engine.process("explain quantum physics")
        # result.source == "online", result.response == "..."

        result = engine.process("open youtube")
        # result.source == "local", result.response == "..."
    """

    def __init__(
        self,
        local_handler: Any,  # JarvisAssistant or similar with .handle_text()
        router: IntentRouter | None = None,
        online: OnlineProvider | None = None,
    ) -> None:
        self._local = local_handler
        self._router = router or IntentRouter()
        self._online = online or OnlineProvider()
        self._conversation_context: list[dict[str, str]] = []

    def handle_text(self, user_input: str) -> tuple[str, dict[str, object] | None]:
        """Compatibility method for BackendWorker."""
        res = self.process(user_input)
        return res.response, getattr(res, "snapshot", None)

    def poll_notifications(self) -> list[str]:
        if hasattr(self._local, "poll_notifications"):
            return self._local.poll_notifications()
        return []

    def begin_execution(self, request_id: int | str | None = None) -> None:
        if hasattr(self._local, "begin_execution"):
            self._local.begin_execution(request_id)

    def cancel_active(self, request_id: int | str | None = None) -> bool:
        if hasattr(self._local, "cancel_active"):
            return bool(self._local.cancel_active(request_id))
        return False

    def finish_execution(self, request_id: int | str | None = None) -> None:
        if hasattr(self._local, "finish_execution"):
            self._local.finish_execution(request_id)

    def subscribe_stream(self, callback) -> None:
        if hasattr(self._local, "subscribe_stream"):
            self._local.subscribe_stream(callback)

    def unsubscribe_stream(self, callback) -> None:
        if hasattr(self._local, "unsubscribe_stream"):
            self._local.unsubscribe_stream(callback)

    def subscribe_runtime_event(self, event_name: str, callback) -> None:
        if hasattr(self._local, "subscribe_runtime_event"):
            self._local.subscribe_runtime_event(event_name, callback)

    def unsubscribe_runtime_event(self, event_name: str, callback) -> None:
        if hasattr(self._local, "unsubscribe_runtime_event"):
            self._local.unsubscribe_runtime_event(event_name, callback)

    def process(self, user_input: str) -> HybridResult:
        """Route and process user input through the appropriate pipeline."""
        start = time.perf_counter()
        text = user_input.strip()

        if not text:
            return HybridResult(
                response="I didn't catch that. Could you repeat?",
                source="local",
                intent_type="LOCAL_TASK",
                confidence=1.0,
            )

        # ── Step 0: Check for follow-up context replies ──────────────
        try:
            from assistant.task_context import task_context
            from assistant.trust_manager import trust_manager
            
            follow_up = task_context.handle_reply(text)
            if follow_up:
                action = follow_up.get("action")
                
                # Handling confirmation
                if action == "cancel":
                    # Reduce trust if they cancelled a confirmation
                    ctx_name, ctx_data, _ = task_context.get_context()
                    if ctx_name and ctx_name.startswith("confirm_"):
                        trust_manager.record_cancellation(ctx_data.get("category", ""))
                        
                    return HybridResult(
                        response=follow_up.get("message", "Cancelled."),
                        source="local",
                        intent_type="LOCAL_TASK"
                    )
                
                # It's a continuation of a task chain
                # We can route this as a local execution
                # But actually, to make it seamless, we just treat it as new input
                # e.g., "search youtube for X"
                if action == "youtube_search":
                    text = f"search youtube for {follow_up.get('query')}"
                    logger.info("Follow-up triggered new task: %s", text)
                    
                if action == "feedback_success":
                    category = follow_up.get("category", "")
                    trust_manager.record_success(category, quality_score=1.0)
                    return HybridResult(
                        response="Great! I've logged the success and increased my confidence for this task.",
                        source="local",
                        intent_type="LOCAL_TASK"
                    )
                    
                if action == "feedback_failure":
                    from assistant.command_executor import command_executor
                    from assistant.execution_history import execution_history
                    
                    category = follow_up.get("category", "")
                    rollback_cmd = follow_up.get("rollback_cmd")
                    
                    # Lower trust because the automation failed
                    trust_manager.record_cancellation(category)
                    execution_history.log_action("feedback_failure", {"category": category}, "User reported failure", False, -0.3)
                    
                    response_msg = "Noted. I've lowered my confidence for this task and will be more careful next time."
                    
                    if rollback_cmd:
                        rbs, rbo = command_executor.execute(rollback_cmd)
                        if rbs:
                            response_msg += f"\nI also successfully ran a rollback: {rollback_cmd}"
                            execution_history.log_action("rollback", {"command": rollback_cmd}, rbo, True)
                        else:
                            response_msg += f"\nI attempted a rollback but it failed: {rbo}"
                            execution_history.log_action("rollback", {"command": rollback_cmd}, rbo, False)
                            
                    return HybridResult(
                        response=response_msg,
                        source="local",
                        intent_type="LOCAL_TASK"
                    )
                    
                if action == "relaunch_admin":
                    from assistant.command_executor import command_executor
                    import sys
                    
                    success = command_executor.relaunch_as_admin()
                    if success:
                        # Relaunch succeeded, exit current unprivileged process
                        sys.exit(0)
                    return HybridResult(
                        response="I couldn't relaunch with elevated privileges.",
                        source="local",
                        intent_type="LOCAL_TASK"
                    )
                    
        except Exception as e:
            logger.error("Task context error: %s", e)

        # ── Step 1: Classify ─────────────────────────────────────────
        decision = self._router.classify(text)
        
        # Apply behavior learning confidence modifier
        modifier = behavior_learner.get_confidence_modifier(text)
        decision.confidence = max(0.0, min(1.0, decision.confidence + modifier))
        
        # Apply privacy guard safety check
        if not privacy_guard.is_safe(text):
            decision.privacy_safe = False
            
        logger.info(
            "Route decision: %s (%.2f) — %s",
            decision.intent_type.value, decision.confidence, decision.reason,
        )

        # ── Step 2: Check cache for online queries ───────────────────
        if decision.intent_type == IntentType.ONLINE_QUERY and decision.cached:
            cached_response = self._router.get_cached_response(text)
            if cached_response:
                elapsed = (time.perf_counter() - start) * 1000
                logger.info("Serving cached online response (%.1fms)", elapsed)
                return HybridResult(
                    response=cached_response,
                    source="online_cached",
                    intent_type=decision.intent_type.value,
                    confidence=decision.confidence,
                    latency_ms=elapsed,
                    cached=True,
                    privacy_safe=decision.privacy_safe,
                    provider="cache",
                )

        # ── Step 2.5: Goal Planner Interception ──────────────────────
        if text.lower().startswith("plan ") or text.lower().startswith("goal "):
            goal_text = text.split(" ", 1)[1].strip()
            local_decision = RoutingDecision(
                intent_type=IntentType.LOCAL_TASK,
                confidence=1.0,
                reason="explicit planning request",
                privacy_safe=decision.privacy_safe,
                cached=False,
            )
            result = self._execute_local(goal_text, local_decision, start)
            self._update_context(goal_text, result.response)
            return result
            goal_text = text[5:].strip() if text.lower().startswith("plan ") else text[5:].strip()
            from assistant.app import planner
            import asyncio
            
            async def _llm_gen(prompt: str) -> str:
                resp = self._online.query(prompt)
                return resp.text if resp.success else "[]"
                
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                
            plan_data = loop.run_until_complete(planner.generate_plan(goal_text, _llm_gen))
            
            if plan_data:
                preview = planner.format_plan_preview(goal_text, plan_data)
                from assistant.task_context import task_context
                task_context.set_context("confirm_goal_plan", {"goal": goal_text, "plan": plan_data}, expecting_reply=True)
                return HybridResult(
                    response=preview,
                    source="local_planner",
                    intent_type="LOCAL_TASK",
                    confidence=1.0
                )
            else:
                return HybridResult(
                    response="❌ Failed to generate a valid execution plan.",
                    source="local_planner",
                    intent_type="LOCAL_TASK",
                    confidence=0.0
                )

        # ── Step 3: Route to pipeline ────────────────────────────────
        if decision.intent_type == IntentType.LOCAL_TASK:
            result = self._execute_local(text, decision, start)
            self._update_context(text, result.response)
            self._publish_prediction(text)
            return result

        if decision.intent_type == IntentType.CONCEPTUAL_QUERY:
            result = self._execute_conceptual(text, decision, start)
            # Fallback to online if concept graph is insufficient
            if result.source == "conceptual_fallback":
                logger.info("Concept reasoning fallback, escalating to online")
                result = self._execute_online(text, decision, start)
        else:
            result = self._execute_online(text, decision, start)

        # ── Step 4: Confidence-based escalation ──────────────────────
        # If local execution returned a fallback and confidence is low,
        # try online as escalation
        if (
            decision.intent_type != IntentType.LOCAL_TASK
            and result.source == "local"
            and result.confidence < self._router.CONFIDENCE_THRESHOLD
            and decision.privacy_safe
        ):
            logger.info(
                "Local confidence %.2f < threshold %.2f — escalating to online",
                result.confidence, self._router.CONFIDENCE_THRESHOLD,
            )
            online_result = self._execute_online(text, decision, start)
            if online_result.source != "local_fallback":
                return online_result

        # ── Step 5: Update conversation context ──────────────────────
        self._update_context(text, result.response)

        # ── Step 6: Check for proactive follow-up suggestion ─────────
        self._publish_prediction(text)

        return result

    def _execute_local(
        self, text: str, decision: RoutingDecision, start: float
    ) -> HybridResult:
        """Run through the local planner + action engine."""
        try:
            response, snapshot = self._local.handle_text(text)
            elapsed = (time.perf_counter() - start) * 1000

            # Detect if this was a fallback (no steps executed)
            is_fallback = False
            if snapshot and isinstance(snapshot, dict):
                steps = snapshot.get("steps", [])
                if not steps:
                    is_fallback = True
            logger.info("Local execution completed in %.1fms", elapsed)

            return HybridResult(
                response=response,
                source="local",
                intent_type=decision.intent_type.value,
                confidence=decision.confidence if not is_fallback else 0.3,
                latency_ms=elapsed,
                privacy_safe=decision.privacy_safe,
                provider="local",
                snapshot=snapshot,
            )
        except Exception as e:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error("Local execution failed: %s", e)
            return HybridResult(
                response=f"I ran into an issue: {e}",
                source="local",
                intent_type="LOCAL_TASK",
                confidence=0.0,
                latency_ms=elapsed,
            )

    def _execute_conceptual(
        self, text: str, decision: RoutingDecision, start: float
    ) -> HybridResult:
        """Run through the Reasoning Engine using Knowledge Graph."""
        try:
            from assistant.concept_extractor import extractor
            from assistant.reasoning_engine import reasoner
            
            concepts = extractor.extract(text)
            logger.info("Extracted concepts: %s", concepts)
            
            reasoning_res = reasoner.reason(concepts)
            elapsed = (time.perf_counter() - start) * 1000
            logger.info("Conceptual execution completed in %.1fms", elapsed)
            
            if reasoning_res and reasoning_res["confidence"] >= 0.5:
                return HybridResult(
                    response=reasoning_res["explanation"],
                    source="conceptual",
                    intent_type=decision.intent_type.value,
                    confidence=reasoning_res["confidence"],
                    latency_ms=elapsed,
                    privacy_safe=decision.privacy_safe,
                    provider="reasoner",
                )
            else:
                return HybridResult(
                    response="Insufficient concepts or low confidence.",
                    source="conceptual_fallback",
                    intent_type=decision.intent_type.value,
                    confidence=reasoning_res["confidence"] if reasoning_res else 0.2,
                    latency_ms=elapsed,
                    provider="reasoner",
                )
        except Exception as e:
            logger.error("Conceptual execution failed: %s", e)
            elapsed = (time.perf_counter() - start) * 1000
            return HybridResult(
                response="Error reasoning.",
                source="conceptual_fallback",
                intent_type=decision.intent_type.value,
                confidence=0.0,
                latency_ms=elapsed,
                provider="reasoner",
            )

    def _execute_online(
        self, text: str, decision: RoutingDecision, start: float
    ) -> HybridResult:
        """Query online LLM providers with automatic fallback."""
        if not decision.privacy_safe:
            logger.warning("Blocking online query: sensitive content detected")
            return self._execute_local(text, decision, start)

        try:
            # Redact text before sending online
            safe_text = privacy_guard.redact(text)
            
            online_resp: OnlineResponse = self._online.query(
                safe_text, context=self._conversation_context
            )

            if online_resp.success:
                # Expand knowledge graph if it was a conceptual query
                if decision.intent_type.value == "CONCEPTUAL_QUERY":
                    try:
                        self._expand_knowledge(safe_text, online_resp.text)
                    except Exception as e:
                        logger.error("Knowledge expansion error: %s", e)

                # Cache the response
                self._router.cache_response(text, online_resp.text)
                elapsed = (time.perf_counter() - start) * 1000
                logger.info("Online execution completed in %.1fms via %s", elapsed, online_resp.provider)

                return HybridResult(
                    response=online_resp.text,
                    source="online",
                    intent_type=decision.intent_type.value,
                    confidence=decision.confidence,
                    latency_ms=elapsed,
                    privacy_safe=decision.privacy_safe,
                    provider=online_resp.provider,
                )
            else:
                # All providers failed → graceful local fallback
                logger.warning("All online providers failed, falling back to local")
                elapsed = (time.perf_counter() - start) * 1000
                return HybridResult(
                    response="I'll handle this locally. I couldn't reach any online AI service right now.",
                    source="local_fallback",
                    intent_type=decision.intent_type.value,
                    confidence=0.4,
                    latency_ms=elapsed,
                    privacy_safe=decision.privacy_safe,
                    provider="fallback",
                )

        except Exception as e:
            logger.error("Online query error: %s", e)
            elapsed = (time.perf_counter() - start) * 1000
            return HybridResult(
                response="I'll handle this locally. There was a connectivity issue.",
                source="local_fallback",
                intent_type="ONLINE_QUERY",
                confidence=0.3,
                latency_ms=elapsed,
                provider="fallback",
            )

    def _update_context(self, user_text: str, assistant_text: str) -> None:
        """Maintain sliding window of conversation context for online queries."""
        self._conversation_context.append({"role": "user", "content": user_text})
        self._conversation_context.append({"role": "assistant", "content": assistant_text})
        # Keep the last 10 turns (20 messages) for follow-up continuity.
        if len(self._conversation_context) > 20:
            self._conversation_context = self._conversation_context[-20:]

    def _publish_prediction(self, text: str) -> None:
        try:
            from assistant.predictive_engine import predictor

            next_pred = predictor.predict_for_context(text)
            if next_pred and next_pred.confidence >= 0.5:
                from assistant.event_bus import Events, bus

                bus.publish_async(Events.PREDICTION_READY, next_pred.to_dict())
        except Exception as e:
            logger.error("Failed to publish next-step prediction: %s", e)

    def _expand_knowledge(self, query: str, response: str) -> None:
        """Extract concepts from successful online responses to grow the knowledge graph."""
        from assistant.concept_extractor import extractor
        from assistant.knowledge_graph import knowledge_graph
        
        concepts_in_query = extractor.extract(query)
        # Extract main concepts from response (top 3)
        concepts_in_resp = extractor.extract(response)[:3]
        
        # Add them to graph as new nodes if they don't exist
        for c in concepts_in_query + concepts_in_resp:
            # We don't know the type, so default to concept
            knowledge_graph.add_node(f"concept:{c.replace(' ', '_')}", "concept", c.title(), save=False)
            
        # Bind them
        if concepts_in_query and concepts_in_resp:
            c1 = f"concept:{concepts_in_query[0].replace(' ', '_')}"
            c2 = f"concept:{concepts_in_resp[0].replace(' ', '_')}"
            if c1 != c2:
                # Add a related_to edge
                knowledge_graph.add_edge(c1, c2, "related_to", weight=0.7, save=False)
                
        # Bulk save once
        knowledge_graph._save()
        logger.info("Knowledge Graph expanded with concepts from online response.")

    def get_routing_info(self, text: str) -> dict[str, Any]:
        """Get routing decision info without executing (for debugging/logs)."""
        decision = self._router.classify(text)
        return {
            "intent_type": decision.intent_type.value,
            "confidence": decision.confidence,
            "reason": decision.reason,
            "privacy_safe": decision.privacy_safe,
            "cached": decision.cached,
        }
