from __future__ import annotations


class _MemoryGraph:
    def __init__(self) -> None:
        self.clear_all()

    def clear_all(self) -> None:
        self._stats = {
            "total_nodes": 0,
            "total_edges": 0,
            "node_types": {"app": 0, "action": 0},
        }

    def stats(self) -> dict[str, object]:
        return dict(self._stats)


graph = _MemoryGraph()

