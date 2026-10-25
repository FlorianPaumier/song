"""Small deterministic formatter for the user's readable lyric headings."""

import re


HEADING = re.compile(
    r"^\s*(?:\*\*)?\s*(Intro|Verse\s*\d*|Pre[- ]?chorus|Chorus|Bridge|Breakdown|Outro|Instrumental)\b([^:]*?)(?:\*\*)?\s*:\s*(.*)$",
    re.IGNORECASE,
)
STANDALONE = re.compile(r"^\s*(?:\*\*)?\s*(Intro|Verse\s*\d*|Pre[- ]?chorus|Chorus|Bridge|Breakdown|Outro|Instrumental)(\s*\([^)]*\))?\s*(?:\*\*)?\s*$", re.IGNORECASE)
BRACKET = re.compile(r"^\s*\[([^]]+)\]\s*(.*)$")
REPEAT = re.compile(r"(?:[×x]\s*(\d+)|repeat\s*(\d+)\s*times?)", re.IGNORECASE)
SPEAKER = re.compile(r"\b(?:HIM|HER|BOTH)\s*:\s*", re.IGNORECASE)
DIRECTION = re.compile(r"\((?:spoken|whispered|sung|together|a cappella|.*?guitar|.*?drum).*?\)", re.IGNORECASE)


def normalize_lyrics(raw: str) -> tuple[str, list[str]]:
    if not raw.strip():
        return "", []

    # The editor also accepts pasted Markdown whose sections run together.
    text = raw.replace("\r\n", "\n")
    text = re.sub(r"(?<!^)\s+(?=\*\*(?:Intro|Verse|Pre[- ]?chorus|Chorus|Bridge|Breakdown|Outro)\b)", "\n", text, flags=re.IGNORECASE)
    sections: list[tuple[str, list[str]]] = []
    current: tuple[str, list[str]] | None = None
    last_chorus: list[str] | None = None

    for original in text.splitlines():
        line = original.strip()
        if not line:
            continue
        bracket = BRACKET.match(line)
        heading = (HEADING.match(line) or STANDALONE.match(line)) if not bracket else None
        if bracket or heading:
            if current:
                sections.append(current)
                if current[0].lower().startswith("chorus") and current[1]:
                    last_chorus = current[1].copy()
            if bracket:
                label, remainder = bracket.group(1), bracket.group(2)
            else:
                label = f"{heading.group(1)}{heading.group(2) or ''}"
                remainder = heading.group(3) if heading.re is HEADING else ""
            label = label.strip(" -*—:")
            name = re.match(r"(?:Intro|Verse\s*\d*|Pre[- ]?chorus|Chorus|Bridge|Breakdown|Outro|Instrumental)", label, re.IGNORECASE)
            if not name:
                continue
            tag = name.group(0).replace(" ", " ").title().replace("Pre-Chorus", "Pre-Chorus")
            repeat = REPEAT.search(label)
            count = int(next((g for g in repeat.groups() if g), "1")) if repeat else 1
            count = min(max(count, 1), 8)
            if tag.lower().startswith("chorus") and not remainder.strip() and last_chorus:
                for _ in range(count):
                    sections.append(("Chorus", last_chorus.copy()))
                current = None
                continue
            current = (tag, [])
            if remainder.strip():
                cleaned = _clean_line(remainder)
                if cleaned:
                    current[1].append(cleaned)
            # Repeated populated headings are expanded after their body closes.
            if count > 1:
                current = (f"{tag} @repeat={count}", current[1])
            continue
        if current is None:
            current = ("Verse", [])
        cleaned = _clean_line(line)
        if cleaned:
            current[1].append(cleaned)

    if current:
        sections.append(current)

    expanded: list[tuple[str, list[str]]] = []
    for tag, lines in sections:
        repeat_match = re.search(r" @repeat=(\d+)$", tag)
        count = int(repeat_match.group(1)) if repeat_match else 1
        clean_tag = re.sub(r" @repeat=\d+$", "", tag)
        for _ in range(count):
            expanded.append((clean_tag, lines))
    outline = [tag for tag, _ in expanded]
    formatted = "\n\n".join(f"[{tag}]\n" + "\n".join(lines) for tag, lines in expanded)
    return formatted, outline


def _clean_line(line: str) -> str:
    line = SPEAKER.sub("", line)
    line = DIRECTION.sub("", line)
    line = line.replace("**", "").replace("*", "").strip()
    return re.sub(r"\s+", " ", line)
