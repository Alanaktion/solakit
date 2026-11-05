"""Bounded recall context and explicit, configurable acknowledgement skipping."""

from solakit.core.config import MemoryConfig


def emoji_only(text: str) -> bool:
    # Recognize common pictographs, flags, ZWJ/variation sequences and keycaps;
    # ordinary symbols, digits and punctuation alone are not acknowledgements.
    seen = False
    position = 0
    while position < len(text):
        char = text[position]
        code = ord(char)
        if char.isspace() or code in {0x200D, 0xFE0E, 0xFE0F}:
            position += 1
            continue
        if char in "0123456789#*":
            end = position + 1
            if end < len(text) and ord(text[end]) == 0xFE0F:
                end += 1
            if end >= len(text) or ord(text[end]) != 0x20E3:
                return False
            seen = True
            position = end + 1
            continue
        if (
            0x1F000 <= code <= 0x1FAFF
            or 0x2600 <= code <= 0x27BF
            or 0x2194 <= code <= 0x2199
            or 0x23E9 <= code <= 0x23F3
            or 0x23F8 <= code <= 0x23FA
            or 0x25FB <= code <= 0x25FE
            or 0x2B05 <= code <= 0x2B07
            or code
            in {
                0xA9,
                0xAE,
                0x203C,
                0x2049,
                0x2122,
                0x2139,
                0x21A9,
                0x21AA,
                0x231A,
                0x231B,
                0x2328,
                0x23CF,
                0x24C2,
                0x25AA,
                0x25AB,
                0x25B6,
                0x25C0,
                0x2934,
                0x2935,
                0x2B1B,
                0x2B1C,
                0x2B50,
                0x2B55,
                0x3030,
                0x303D,
                0x3297,
                0x3299,
            }
        ):
            seen = True
            position += 1
            continue
        return False
    return seen


def trivial_turn(text: str, config: MemoryConfig) -> bool:
    normalized = text.strip().casefold()
    return (
        config.recall_skip_trivial
        and 0 < len(normalized) <= 12
        and "?" not in normalized
        and (
            normalized in {s.strip().casefold() for s in config.recall_trivial_phrases}
            or emoji_only(normalized)
        )
    )


def recall_query(history: list[dict], incoming: str, config: MemoryConfig) -> str:
    recent = [
        message["content"]
        for message in history
        if message.get("role") in {"user", "assistant"}
        and isinstance(message.get("content"), str)
        and message["content"]
    ]
    context = recent[-config.recall_window :] if config.recall_window else []
    return "\n".join([*context, incoming or "Image attachment"])[
        -config.recall_query_max_chars :
    ]
