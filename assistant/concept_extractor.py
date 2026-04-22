"""Concept Extractor — identifies core concepts from user input.

Uses lightweight NLP/heuristics to pull out key nouns and concepts.
"""
from __future__ import annotations

import re
import logging

logger = logging.getLogger("Jarvis.ConceptExtractor")

# Common stop words to filter out
STOP_WORDS = {
    "explain", "what", "is", "how", "does", "do", "why", "who", "was", "are", 
    "the", "a", "an", "and", "or", "but", "if", "then", "else", "when", "where",
    "can", "you", "me", "tell", "about", "describe", "show", "give", "details",
    "on", "in", "at", "by", "for", "with", "of", "to", "from", "as", "into"
}

class ConceptExtractor:
    
    @staticmethod
    def extract(text: str) -> list[str]:
        """Extract core concepts from a natural language query."""
        # 1. Clean the text
        clean_text = re.sub(r'[^\w\s-]', '', text.lower())
        
        # 2. Tokenize and filter
        words = clean_text.split()
        concepts = []
        
        # 3. N-gram heuristic (look for 1 or 2 word concepts)
        i = 0
        while i < len(words):
            if words[i] in STOP_WORDS:
                i += 1
                continue
                
            # Try 2-gram first
            if i < len(words) - 1 and words[i+1] not in STOP_WORDS:
                bigram = f"{words[i]} {words[i+1]}"
                concepts.append(bigram)
                i += 2
            else:
                concepts.append(words[i])
                i += 1
                
        # 4. Deduplicate
        return list(set(concepts))

# Singleton
extractor = ConceptExtractor()
