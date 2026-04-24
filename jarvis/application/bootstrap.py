from __future__ import annotations

import logging
from pathlib import Path

from jarvis.application.controller import JarvisApplication
from jarvis.application.execution_engine import register_tool_handlers
from jarvis.application.orchestrator import JarvisOrchestrator
from jarvis.application.runtime_support import EventBus, ReminderScheduler, SessionContextStore
from jarvis.config.constants import build_tool_catalog
from jarvis.config.settings import Settings, load_settings
from jarvis.core.brain import Brain
from jarvis.core.cancellation import CancellationController
from jarvis.core.executor import ToolExecutor
from jarvis.core.memory import LongTermMemory, MemoryManager, SemanticMemory, ShortTermMemory
from jarvis.core.planner import Planner
from jarvis.core.safety import ExecutionGuard, ExecutionSandbox, SafetyAuditLogger, ToolValidator
from jarvis.core.tools import ToolRegistry
from jarvis.infrastructure.ai.llm_client import LLMClient
from jarvis.monitoring import JarvisHealthService
from jarvis.observability import attach_event_bus_logging

from jarvis.runtime.normalizer import InputNormalizer
from jarvis.core.safety.prescreen import SafetyPreScreen
from jarvis.runtime.intent_cache import IntentCache
from jarvis.runtime.rule_engine import RuleEngine, RuleLoader
from jarvis.runtime.embedding_matcher import EmbeddingMatcher, IntentTemplateBank
from jarvis.core.memory.enricher import ContextEnricher
from jarvis.runtime.decision_engine import DecisionEngine
from jarvis.core.learning.feedback_loop import FeedbackLoop


logger = logging.getLogger("Jarvis.Bootstrap")


def build_application(
    memory_db_path: Path | None = None,
    *,
    llm_client: object | None = None,
    settings: Settings | None = None,
) -> JarvisApplication:
    settings = settings or load_settings(memory_db_path=memory_db_path)
    event_bus = EventBus()
    attach_event_bus_logging(event_bus)

    registry = ToolRegistry()
    register_tool_handlers(registry)

    cancellation_controller = CancellationController()
    scheduler = ReminderScheduler()
    session_context = SessionContextStore()
    health_service = JarvisHealthService(settings)
    safety_guard = ExecutionGuard(event_bus=event_bus, cancellation_controller=cancellation_controller)
    SafetyAuditLogger(settings.logging.audit_log_path).attach(event_bus)

    memory = MemoryManager(
        short_term=ShortTermMemory(),
        long_term=LongTermMemory(settings.memory_db_path.with_suffix(".long_term.json")),
        semantic=SemanticMemory(settings.memory_db_path.with_suffix(".semantic.json"), require_embeddings=False),
    )

    tool_catalog = build_tool_catalog()
    brain = Brain(
        llm_client=llm_client if llm_client is not None else LLMClient(),
        tool_catalog=tool_catalog,
    )
    planner = Planner(brain=brain, tool_catalog=tool_catalog)
    executor = ToolExecutor(
        registry=registry,
        validator=ToolValidator(),
        guard=safety_guard,
        sandbox=ExecutionSandbox(),
        settings=settings,
        memory=memory,
        scheduler=scheduler,
        health_service=health_service,
        event_bus=event_bus,
        cancellation_controller=cancellation_controller,
    )
    
    # Phase 4 Wire-up: DecisionEngine and its sub-components
    enricher = ContextEnricher(
        short_term_memory=memory.short_term,
        long_term_memory=memory.long_term,
        semantic_memory=memory.semantic,
        # A proper system state provider can be hooked up if needed
    )
    rule_loader = RuleLoader(settings.memory_db_path.with_suffix(".rules.yaml"))
    rule_loader.start_watchdog()
    template_bank = IntentTemplateBank(memory.semantic)
    decision_engine = DecisionEngine(
        normalizer=InputNormalizer(),
        prescreen=SafetyPreScreen(),
        cache=IntentCache(),
        rule_engine=RuleEngine(rule_loader, enricher),
        embedding_matcher=EmbeddingMatcher(template_bank, enricher),
        enricher=enricher
    )
    
    # Phase 5: FeedbackLoop
    feedback_loop = FeedbackLoop(
        rules_path=settings.memory_db_path.with_suffix(".rules.yaml"),
        template_bank=template_bank,
    )

    orchestrator = JarvisOrchestrator(
        planner=planner,
        executor=executor,
        memory=memory,
        settings=settings,
        decision_engine=decision_engine,
        feedback_loop=feedback_loop,
        event_bus=event_bus,
        scheduler=scheduler,
        session_context=session_context,
        health_service=health_service,
        cancellation_controller=cancellation_controller,
    )
    logger.info(
        "Application bootstrap completed.",
        extra={
            "event": "bootstrap.completed",
            "environment": settings.environment,
            "memory_db_path": str(settings.memory_db_path),
        },
    )
    return JarvisApplication(
        orchestrator=orchestrator,
        settings=settings,
        health_service=health_service,
    )
