from __future__ import annotations

import json


class RuleBasedClaudeAgent:
    """A deterministic local planner shim used by the current offline control plane."""

    def chat(self, prompt: str, context: str = "") -> str:
        return json.dumps({"prompt": prompt, "context": context})


class ClaudeAgent:
    def __init__(self, model: str = "llama3") -> None:
        self.model = model

    async def chat(self, prompt: str, context: str = "") -> str:
        try:
            import ollama
        except ImportError:
            return json.dumps({"error": "ollama_not_installed", "prompt": prompt, "context": context})

        client = ollama.AsyncClient()
        system_prompt = "You are a planning and reasoning agent. Return strict JSON."
        try:
            response = await client.chat(
                model=self.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": f"Context:\n{context}\n\nPrompt:\n{prompt}"},
                ],
            )
            return response["message"]["content"]
        except Exception as exc:  # pragma: no cover - optional integration
            return json.dumps({"error": str(exc)})
