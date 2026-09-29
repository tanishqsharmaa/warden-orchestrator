"""Extractive context window compressor for warden-orchestrator."""

import re

from warden_orchestrator.models import Passage

BOILERPLATE_PATTERNS = [
    re.compile(r"(?i)\b(?:confidential|internal\s+use\s+only|all\s+rights\s+reserved|do\s+not\s+distribute|proprietary\s+information|standard\s+disclaimer[:\s]*)[^\.\n]*[\.\n]?", re.IGNORECASE),
]


class ContextCompressor:
    """Compresses candidate context from ~1,500 down to <=800 tokens by stripping boilerplate and deduplicating spans."""

    def __init__(self) -> None:
        self._sentence_split = re.compile(r"(?<=[.!?])\s+")

    def _estimate_tokens(self, text: str) -> int:
        """Estimate token count (approx 1.3 tokens per whitespace-separated word)."""
        words = text.split()
        if not words:
            return 0
        return int(len(words) * 1.33) + 1

    def _clean_text(self, text: str) -> str:
        """Remove generic HR boilerplate and redundant notices."""
        cleaned = text
        for pat in BOILERPLATE_PATTERNS:
            cleaned = pat.sub("", cleaned)
        return re.sub(r"\s+", " ", cleaned).strip()

    def compress_passages(
        self,
        passages: list[Passage],
        target_tokens: int = 800,
        max_tokens: int = 1500,
    ) -> tuple[str, int]:
        """Compress retrieved passages, deduplicate overlapping sentences, and format citation anchors."""
        if not passages:
            return "", 0

        seen_sentences: set[str] = set()
        formatted_blocks: list[str] = []
        current_token_count = 0

        for p in passages:
            cleaned_raw = self._clean_text(p.content)
            sentences = self._sentence_split.split(cleaned_raw) if cleaned_raw else []

            retained_sentences: list[str] = []
            for s in sentences:
                s_strip = s.strip()
                if not s_strip:
                    continue
                # Normalize for fuzzy deduplication
                norm_key = re.sub(r"\W+", "", s_strip.lower())
                if len(norm_key) < 15:  # Keep short structural fragments without dedup
                    retained_sentences.append(s_strip)
                    continue

                if norm_key not in seen_sentences:
                    seen_sentences.add(norm_key)
                    retained_sentences.append(s_strip)

            # Fallback if cleaning or deduplication stripped everything but raw passage existed
            if not retained_sentences and p.content.strip():
                # Retain raw truncated passage
                retained_content = p.content.strip()[:300]
            else:
                retained_content = " ".join(retained_sentences)

            if not retained_content:
                continue

            block = f"[Doc: {p.doc_id}, Chunk: {p.chunk_index}]\n{retained_content}"
            block_tokens = self._estimate_tokens(block)

            if current_token_count + block_tokens > target_tokens and formatted_blocks:
                # Target budget reached; stop accumulating further passages
                break

            if current_token_count + block_tokens > max_tokens:
                break

            formatted_blocks.append(block)
            current_token_count += block_tokens

        compressed_text = "\n\n".join(formatted_blocks)
        total_tokens = self._estimate_tokens(compressed_text)
        return compressed_text, total_tokens
