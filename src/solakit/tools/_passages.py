"""Deterministic local passage ranking (BM25) over converted Markdown."""

import math
import re

WORD = re.compile(r"[\w']+")


def chunks(text: str, size: int = 800) -> list[tuple[int, str]]:
    """Paragraph-aligned chunks of about ``size`` characters with offsets."""
    result, start, current = [], 0, ""
    position = 0
    for paragraph in re.split(r"(\n\s*\n)", text):
        if current and len(current) + len(paragraph) > size:
            result.append((start, current))
            start, current = position, ""
        current += paragraph
        position += len(paragraph)
        while len(current) > size * 2:
            result.append((start, current[:size]))
            start += size
            current = current[size:]
    if current.strip():
        result.append((start, current))
    return [(offset, chunk.strip()) for offset, chunk in result if chunk.strip()]


def rank(text: str, query: str, k: int = 5) -> list[dict]:
    terms = [t.casefold() for t in WORD.findall(query) if len(t) > 1]
    pieces = chunks(text)
    if not terms or not pieces:
        return []
    docs = [[w.casefold() for w in WORD.findall(chunk)] for _, chunk in pieces]
    average = sum(map(len, docs)) / len(docs) or 1
    scores = []
    for (offset, chunk), words in zip(pieces, docs, strict=True):
        score = 0.0
        for term in set(terms):
            frequency = words.count(term)
            if not frequency:
                continue
            containing = sum(term in doc for doc in docs)
            idf = math.log(1 + (len(docs) - containing + 0.5) / (containing + 0.5))
            score += (
                idf
                * frequency
                * 2.2
                / (frequency + 1.2 * (0.25 + 0.75 * len(words) / average))
            )
        if score > 0:
            scores.append((score, offset, chunk))
    scores.sort(key=lambda item: (-item[0], item[1]))
    return [
        {"offset": offset, "score": round(score, 3), "text": chunk}
        for score, offset, chunk in scores[:k]
    ]
