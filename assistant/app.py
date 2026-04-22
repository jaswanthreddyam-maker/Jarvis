from __future__ import annotations

from pathlib import Path

from assistant.action_engine import ActionEngine
from assistant.cancellation_controller import CancellationController
from assistant.error_handler import ErrorHandler
from assistant.event_bus import EventBus
from assistant.memory.memory import MemoryManager
from assistant.orchestrator import Orchestrator
from assistant.permissions import PermissionManager
from assistant.personality import Personality
from assistant.planner import GoalPlanner
from assistant.plugins.system_plugin import SystemPlugin
from assistant.runtime_observer import RuntimeObserver
from assistant.scope_validator import ActionScopeValidator
from assistant.scheduler import Scheduler
from assistant.settings import load_settings
from assistant.state_manager import TaskStateManager
from assistant.streaming_response_handler import StreamingResponseHandler
from assistant.actions.registry import ActionRegistry
from assistant.workflow_memory import WorkflowMemory

from assistant.global_state import GlobalAppState
from assistant.performance_manager import PerformanceManager
from assistant.metrics_collector import MetricsCollector
from assistant.health_monitor import HealthMonitor
import assistant.anomaly_detector # Side-effects only (EventBus sub)
import assistant.session_store    # Side-effects only (EventBus sub)


class JarvisAssistant:
    def __init__(
        self,
        orchestrator: Orchestrator,
        state_manager: TaskStateManager,
        stream_handler: StreamingResponseHandler | None = None,
        perf_manager: PerformanceManager | None = None,
        health_monitor: HealthMonitor | None = None,
        metrics_collector: MetricsCollector | None = None,
    ) -> None:
        self._orchestrator = orchestrator
        self._state_manager = state_manager
        self._stream_handler = stream_handler
        self._perf_manager = perf_manager
        self._health_monitor = health_monitor
        self._metrics_collector = metrics_collector

    def subscribe_runtime_event(self, event_name: str, callback) -> None:
        event_bus = getattr(self._orchestrator, "_event_bus", None)
        if event_bus is not None:
            event_bus.subscribe(event_name, callback)

    def unsubscribe_runtime_event(self, event_name: str, callback) -> None:
        event_bus = getattr(self._orchestrator, "_event_bus", None)
        if event_bus is not None:
            event_bus.unsubscribe(event_name, callback)

    def begin_execution(self, request_id: int | str | None = None) -> None:
        cancellation_controller = getattr(self._orchestrator, "_cancellation_controller", None)
        if cancellation_controller is not None:
            cancellation_controller.begin(request_id)

    def cancel_active(self, request_id: int | str | None = None) -> bool:
        cancellation_controller = getattr(self._orchestrator, "_cancellation_controller", None)
        cancelled = False
        if cancellation_controller is not None:
            cancelled = cancellation_controller.cancel(request_id)
        self._orchestrator.interrupt()
        return cancelled

    def finish_execution(self, request_id: int | str | None = None) -> None:
        cancellation_controller = getattr(self._orchestrator, "_cancellation_controller", None)
        if cancellation_controller is not None:
            cancellation_controller.finish(request_id)

    def handle_text(self, user_input: str) -> tuple[str, dict[str, object] | None]:
        response, task = self._orchestrator.handle_text(user_input)
        snapshot = None
        if task is not None:
            snapshot = self._state_manager.snapshot(task.task_id)
        return response, snapshot

    def poll_notifications(self) -> list[str]:
        return self._orchestrator.poll_notifications()

    def subscribe_stream(self, callback) -> None:
        if self._stream_handler is not None:
            self._stream_handler.subscribe(callback)

    def unsubscribe_stream(self, callback) -> None:
        if self._stream_handler is not None:
            self._stream_handler.unsubscribe(callback)


def build_assistant(memory_db_path: Path | None = None) -> JarvisAssistant:
    settings = load_settings(memory_db_path=memory_db_path)
    event_bus = EventBus()
    cancellation_controller = CancellationController()
    scope_validator = ActionScopeValidator()
    scheduler = Scheduler(event_bus)
    memory = MemoryManager(settings.memory_db_path, settings.db_schema_path)
    personality = Personality.from_file(settings.personality_path)
    state_manager = TaskStateManager()
    permissions = PermissionManager(settings.permissions_path)
    error_handler = ErrorHandler(settings.retry_policy_path)
    workflow_store_path = settings.memory_db_path.with_suffix(".workflows.json")
    workflow_store = WorkflowMemory(workflow_store_path)
    observer = RuntimeObserver()
    registry = ActionRegistry()
    SystemPlugin().register(registry)
    action_engine = ActionEngine(
        registry=registry,
        permissions=permissions,
        event_bus=event_bus,
        memory=memory,
        scheduler=scheduler,
        settings=settings,
        observer=observer,
        cancellation_controller=cancellation_controller,
        scope_validator=scope_validator,
    )
    # ── Orchestrator components ──
    planner = GoalPlanner(
        action_engine=action_engine,
        memory=memory,
        workflow_store=workflow_store,
        observer=observer,
    )
    permissions.set_confirmation_callback(planner.confirm_action)
    global_state = GlobalAppState.get_instance(event_bus)
    stream_handler = StreamingResponseHandler(event_bus)
    
    perf_manager = PerformanceManager(global_state, event_bus, settings)
    perf_manager.start()

    metrics_collector = MetricsCollector()
    metrics_collector.start()

    # Note: LifecycleManager needs the bridge/app instances. We initialize it here without them 
    # and attach it later in the UI bridge, or we just pass None for now.
    
    health_monitor = HealthMonitor()
    health_monitor.start()

    # ── Intelligence Systems ─────────────────────────────────────────
    # Start predictive + proactive engines (background threads)
    try:
        from assistant.predictive_engine import predictor
        predictor.start()
    except Exception:
        pass

    try:
        from assistant.proactive_engine import proactive
        proactive.start()
    except Exception:
        pass

    # Memory graph singleton is auto-initialized on import
    try:
        import assistant.memory_graph  # noqa: F401 — triggers singleton init
    except Exception:
        pass

    orchestrator = Orchestrator(
        planner=planner,
        action_engine=action_engine,
        state_manager=state_manager,
        error_handler=error_handler,
        event_bus=event_bus,
        scheduler=scheduler,
        memory=memory,
        personality=personality,
        cancellation_controller=cancellation_controller,
    )
    return JarvisAssistant(
        orchestrator, 
        state_manager, 
        stream_handler,
        perf_manager, 
        health_monitor, 
        metrics_collector
    )
