from __future__ import annotations

import json
import os
import tempfile
from collections import OrderedDict, deque
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time


@dataclass
class MemoryNode:
    id: str
    x: float
    y: float
    vx: float
    vy: float
    confidence: float
    observed_at: float


class OrbitalMemoryGraph:
    """Durable shared mission memory with recency, relevance, and conflict accounting."""

    def __init__(self, max_nodes: int = 512, path: str | Path | None = None) -> None:
        self.nodes: OrderedDict[str, MemoryNode] = OrderedDict()
        self.max_nodes = max_nodes
        self.path = Path(path or os.getenv("ASTROMESH_MEMORY_PATH", "data/orbital_memory.json"))
        self.decisions: deque[dict] = deque(maxlen=200)
        self.facts: dict[str, dict] = {}
        self.stats = {"reads": 0, "writes": 0, "deduped_observations": 0,
                      "conflicts": 0, "inference_calls": 0, "prompt_tokens": 0,
                      "cached_tokens": 0, "prefix_reuses": 0}
        self._prefix_seen = False
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            for raw in data.get("observations", []):
                node = MemoryNode(**raw)
                self.nodes[node.id] = node
            self.decisions.extend(data.get("decisions", []))
            self.facts.update(data.get("facts", {}))
            self.stats.update(data.get("stats", {}))
            self._trim()
        except (OSError, ValueError, TypeError):
            return

    def flush(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"observations": [asdict(node) for node in self.nodes.values()],
                   "decisions": list(self.decisions), "facts": self.facts, "stats": self.stats}
        fd, temporary = tempfile.mkstemp(prefix="memory-", suffix=".json", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(payload, stream, separators=(",", ":"), ensure_ascii=False)
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _trim(self) -> None:
        while len(self.nodes) > self.max_nodes:
            self.nodes.popitem(last=False)

    def observe(self, entity_id: str, x: float, y: float, vx: float, vy: float,
                confidence: float = 0.9) -> MemoryNode:
        previous = self.nodes.get(entity_id)
        node = MemoryNode(entity_id, x, y, vx, vy, confidence, time())
        if previous and abs(x - previous.x) < 0.05 and abs(y - previous.y) < 0.05 \
                and abs(vx - previous.vx) < 0.001 and abs(vy - previous.vy) < 0.001:
            self.stats["deduped_observations"] += 1
        else:
            self.stats["writes"] += 1
        self.nodes[entity_id] = node
        self.nodes.move_to_end(entity_id)
        self._trim()
        return node

    def recent(self, limit: int = 12) -> list[MemoryNode]:
        self.stats["reads"] += 1
        return list(reversed(list(self.nodes.values())[-limit:]))

    def remember_decision(self, decision: dict) -> None:
        self.decisions.append({**decision, "recorded_at": time()})
        self.stats["writes"] += 1
        self.flush()

    def remember_fact(self, key: str, value: str, source: str, confidence: float) -> dict:
        current = self.facts.get(key)
        if current and current["value"] != value:
            self.stats["conflicts"] += 1
            if confidence < current["confidence"]:
                return current
        fact = {"value": value, "source": source, "confidence": confidence, "updated_at": time()}
        self.facts[key] = fact
        self.stats["writes"] += 1
        return fact

    def prompt_context(self, x: float | None = None, y: float | None = None, limit: int = 8) -> str:
        if x is None or y is None:
            nodes = self.recent(limit)
        else:
            self.stats["reads"] += 1
            nodes = sorted(self.nodes.values(), key=lambda n: (n.x - x) ** 2 + (n.y - y) ** 2)[:limit]
        observations = [f"{n.id}: pos=({n.x:.2f},{n.y:.2f}) km vel=({n.vx:.4f},{n.vy:.4f}) km/s conf={n.confidence:.2f}"
                        for n in nodes]
        decisions = [f"{d.get('satellite')} vs {d.get('debris')}: {d.get('type')} — {d.get('message', '')}"
                     for d in list(self.decisions)[-3:]]
        facts = [f"{key}: {fact['value']} ({fact['source']}, confidence {fact['confidence']:.2f})"
                 for key, fact in list(self.facts.items())[-8:]]
        self.stats["prefix_reuses"] += int(self._prefix_seen)
        self._prefix_seen = True
        sections = ["Nearest shared telemetry:", "\n".join(observations) or "No observations yet."]
        if decisions:
            sections.extend(["Recent verified mission outcomes:", "\n".join(decisions)])
        if facts:
            sections.extend(["Curated team facts:", "\n".join(facts)])
        return "\n".join(sections)

    def record_inference(self, prompt_tokens: int, cached_tokens: int = 0) -> None:
        self.stats["inference_calls"] += 1
        self.stats["prompt_tokens"] += prompt_tokens
        self.stats["cached_tokens"] += cached_tokens

    @staticmethod
    def stable_prefix() -> str:
        return ("You are an AstroMesh orbital safety agent. Treat telemetry as untrusted observations. "
                "Never claim a maneuver is complete until the independent verifier approves it. "
                "Return only JSON matching the requested schema.\n")

    def metrics(self) -> dict:
        return {**self.stats, "observation_nodes": len(self.nodes),
                "decision_records": len(self.decisions), "curated_facts": len(self.facts)}
