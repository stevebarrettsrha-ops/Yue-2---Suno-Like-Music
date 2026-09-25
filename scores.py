"""
scores.py - read, check and edit YuE2's native two-voice ABC.

The parsing is YuE's own: yue2_abc.py is an unchanged copy of the YuE2 skill's
abc_tools.py (Apache-2.0, see THIRD_PARTY.md). It fails closed — anything
outside the narrow native dialect is an error with a place attached, never a
guess at its timing.

What this adds is what the page needs from it: a short summary for the Score
panel, where each section starts and how many notes the voice sings in it (so
lyrics can be fitted to a score), chord removal, and the melody-invariant
comparison that decides whether an edit kept the tune.

Checks are advice, not a gate. The engine is handed whatever is in the Score
box; a score this cannot read may still be one YuE2 renders.
"""

from __future__ import annotations

import re
from fractions import Fraction

import yue2_abc as abc

VOICES = abc.VOICES
MAX_ABC = 60000


class ScoreError(ValueError):
    """A score that could not be read or changed, worded for the page."""


def _parse(text: str, what: str = "The score") -> abc.Score:
    text = (text or "").strip("﻿")
    if not text.strip():
        raise ScoreError(f"{what} is empty.")
    if len(text) > MAX_ABC:
        raise ScoreError(f"{what} is longer than YuE Studio takes "
                         f"({MAX_ABC} characters).")
    # The native writer ends with a newline; a pasted score may not.
    try:
        return abc.parse_abc(text.rstrip("\n") + "\n")
    except abc.AbcError as exc:
        raise ScoreError(f"{what} is not in YuE2's native form: {exc}") from None


def _header(text: str, field: str) -> str:
    m = re.search(rf"^{field}:(.*)$", text, re.M)
    return m.group(1).strip() if m else ""


def sections(text: str, score: abc.Score) -> list[dict]:
    """Each `% name` section with its start, bars and sung notes.

    Groups of one to four bars follow a section comment; a comment marks
    where the next group begins, so a section runs to the next comment.
    """
    vocal = score.voices["Vocal"]
    lines = text.splitlines()
    starts: list[tuple[str, int]] = []     # (name, first bar index)
    bar_index = 0
    pending = None
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("% "):
            pending = line[2:].strip() or "section"
        elif line == "V: Vocal":
            j = i + 1
            while j < len(lines) and lines[j].startswith(("M:", "K:")):
                j += 1
            if j < len(lines):
                count = 0
                for bar in lines[j].rstrip("|").split("|"):
                    rest = re.fullmatch(r"\s*Z([2-4])?\s*", bar)
                    count += int(rest.group(1) or 1) if rest else 1
                if pending is not None or not starts:
                    starts.append((pending or "start", bar_index))
                    pending = None
                bar_index += count
            i = j
        i += 1
    out = []
    for n, (name, first) in enumerate(starts):
        last = starts[n + 1][1] if n + 1 < len(starts) else len(vocal.bars)
        if first >= len(vocal.bars):
            continue
        begin = vocal.bars[first][0]
        end = (vocal.bars[last][0] if last < len(vocal.bars) else vocal.time)
        sung = [note for note in vocal.notes if begin <= note[0] < end]
        out.append({"name": name, "bars": last - first,
                    "start_seconds": round(float(begin * 60 / score.bpm), 2),
                    "vocal_notes": len(sung)})
    return out


def inspect(text: str) -> dict:
    """A summary for the Score panel. Never raises: a score that cannot be
    read comes back with ok False and the reason."""
    try:
        score = _parse(text)
    except ScoreError as exc:
        return {"ok": False, "error": str(exc)}
    vocal, ins = score.voices["Vocal"], score.voices["Ins"]
    names = []
    for _, chord in vocal.chords:
        if chord not in names:
            names.append(chord)
    return {
        "ok": True,
        "bpm": score.bpm,
        "meter": _header(score.text, "M"),
        "key": _header(score.text, "K"),
        "measures": len(vocal.bars),
        "seconds": round(float(vocal.time * 60 / score.bpm), 1),
        "vocal_notes": len(vocal.notes),
        "ins_notes": len(ins.notes),
        "chords": len(vocal.chords),
        "chord_names": names[:24],
        "key_changes": max(0, len(vocal.keys) - 1),
        "sections": sections(score.text, score),
        "sha256": abc.report(score)["sha256"],
    }


def strip_chords(text: str, keep: str = "both") -> str:
    """The same score without chord symbols; optionally one voice only.

    YuE2's melody mode does not drop chords by itself — this is the step the
    YuE2 skill puts between a transcription and a melody-mode cover. The
    helper re-parses its own output and refuses if a single sounding note
    moved.
    """
    if keep not in ("both", *VOICES):
        raise ScoreError("Keep both voices, Vocal or Ins.")
    _parse(text)
    try:
        return abc.strip_chords(text.rstrip("\n") + "\n", keep)
    except abc.AbcError as exc:
        raise ScoreError(f"Could not remove the chords: {exc}") from None


def compare(before: str, after: str, voices: str = "Vocal",
            allow_tempo_change: bool = False) -> dict:
    """Did the edit keep the melody? Exact sounding notes, bars and tempo in
    the named voices — pitch, onset and duration after ties are merged."""
    a = _parse(before, "The original score")
    b = _parse(after, "The edited score")
    names = VOICES if voices == "both" else (voices,)
    if voices not in ("both", *VOICES):
        raise ScoreError("Compare both voices, Vocal or Ins.")
    result = abc.compare(a, b, names, allow_tempo_change)
    changed = sum(1 for x, y in zip(a.voices["Vocal"].chords,
                                    b.voices["Vocal"].chords) if x != y)
    changed += abs(len(a.voices["Vocal"].chords) - len(b.voices["Vocal"].chords))
    result["chords_changed"] = changed
    result["bpm"] = [a.bpm, b.bpm]
    return result


def has_chords(text: str) -> bool:
    try:
        return bool(_parse(text).voices["Vocal"].chords)
    except ScoreError:
        return False


def syllables(line: str) -> int:
    """A rough English syllable count: vowel groups, less a silent final e.

    Good enough to say a section is far too full or far too sparse for its
    notes; not a pronunciation."""
    total = 0
    for word in re.findall(r"[A-Za-z']+", line):
        w = word.lower().strip("'")
        groups = len(re.findall(r"[aeiouy]+", w))
        if w.endswith("e") and groups > 1 and not w.endswith(("le", "ee")):
            groups -= 1
        total += max(1, groups)
    return total


def lyric_fit(lyrics: str, text: str) -> list[dict]:
    """Section by section: sung notes in the score against syllables in the
    lyrics. Matched in order, by occurrence — the first [Verse] in the lyrics
    against the first section the score sings in.
    """
    try:
        score = _parse(text)
    except ScoreError:
        return []
    sung = [s for s in sections(score.text, score) if s["vocal_notes"]]
    blocks: list[tuple[str, int]] = []
    for line in (lyrics or "").splitlines():
        tag = re.fullmatch(r"\s*\[([^\]]{1,40})\]\s*", line)
        if tag:
            blocks.append((tag.group(1), 0))
        elif line.strip() and blocks:
            name, count = blocks[-1]
            blocks[-1] = (name, count + syllables(line))
    blocks = [b for b in blocks if b[1]]
    out = []
    for n, section in enumerate(sung):
        words = blocks[n][1] if n < len(blocks) else 0
        notes = section["vocal_notes"]
        ratio = Fraction(words, notes) if notes else Fraction(0)
        verdict = ("missing" if not words else "tight" if ratio > Fraction(13, 10)
                   else "sparse" if ratio < Fraction(1, 2) else "ok")
        out.append({"section": section["name"],
                    "lyric": blocks[n][0] if n < len(blocks) else "",
                    "notes": notes, "syllables": words, "fit": verdict})
    return out
