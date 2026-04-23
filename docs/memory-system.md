# Jarvis Memory System

## Memory Modules

Implemented in:
- `jarvis/core/memory/short_term.py`
- `jarvis/core/memory/long_term.py`
- `jarvis/core/memory/semantic.py`
- `jarvis/core/memory/memory_manager.py`

## Memory Types

### Short-Term Memory

Purpose:
- keep recent interactions inside the active session
- maintain immediate conversational continuity

Implementation:
- in-memory ring buffer
- stores `user`, `assistant`, `timestamp`, and optional metadata

### Long-Term Memory

Purpose:
- persist preferences and important facts across sessions
- support structured preference lookup

Implementation:
- JSON-backed store
- separate preference map plus fact records

Key methods:
- `save_preference(key, value)`
- `get_preference(key)`
- `remember(namespace, content, category)`
- `recall(query, namespace, limit)`

### Semantic Memory

Purpose:
- retrieve relevant past knowledge by similarity instead of exact keyword match

Implementation:
- required real embeddings from a local transformer model or embedding API
- cosine similarity retrieval
- persistent ChromaDB or JSON-backed vector storage

Key methods:
- `store_memory(text)`
- `retrieve_similar(query)`

## Memory Manager

Central coordination lives in `jarvis/core/memory/memory_manager.py`.

Responsibilities:
- update STM after every interaction
- extract and persist preferences from high-signal user statements
- store important facts in LTM
- store relevant memory-worthy entries in semantic memory
- build query-specific memory context for the brain
- enforce privacy/write-policy rules

## LLM Context Injection

The brain payload now includes:

```json
{
  "session_context": {
    "last_action": "search_youtube",
    "last_target": "python tutorials",
    "last_app": "youtube"
  },
  "memory_context": {
    "short_term": [
      {"user": "play music", "assistant": "What kind of music?"}
    ],
    "long_term": {
      "preferences": {
        "favorite_music": "lofi",
        "preferred_browser": "chrome"
      },
      "facts": [
        {
          "namespace": "system",
          "category": "fact",
          "content": "User studies better with background music."
        }
      ]
    },
    "semantic": [
      {
        "text": "User prefers calm lofi playlists.",
        "score": 0.92
      }
    ]
  }
}
```

## Personalization Example

Stored long-term preference:

```json
{
  "preferred_browser": "chrome",
  "favorite_music": "lofi"
}
```

Behavior:
- `search youtube` -> planner injects `browser_app: "chrome"`
- `play music` with a generic music query -> planner can personalize to `lofi music`

## Memory Write Policy

Stored:
- explicit preferences
- important facts
- high-signal remembered information
- selected automation/workflow knowledge

Not stored:
- random small talk
- temporary noise
- obvious secrets or sensitive credentials

## Privacy Controls

Available manager methods:
- `delete_preference(key)`
- `clear_short_term()`
- `reset_all()`

Sensitive-looking content is blocked from long-term and semantic storage.

## Memory Lifecycle

1. User sends request
2. STM keeps recent interactions in-session
3. Memory manager retrieves relevant preferences, facts, and semantic matches
4. Brain receives structured memory context
5. Planner builds a plan
6. Personalization layer applies stored preferences to plan details
7. After execution, STM updates immediately
8. High-signal facts/preferences are written to LTM and semantic memory
