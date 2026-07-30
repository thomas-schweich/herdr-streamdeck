"""Preview layout: text spread across a block of keys.

The block has to read as one screen. That means characters never straddle a
physical gap, the interior seams carry no margin so a broken word is separated
by the bezel and nothing else, and every key uses one type size.
"""

from __future__ import annotations

import pytest
from PIL import Image, ImageDraw

from herdr_streamdeck.deck import (
    PREVIEW_LINES_PER_ROW,
    load_font,
    plan_preview,
)

SIZE = (72, 72)
TEXT = "Remove the legacy /v1/login endpoint now that nothing references it anywhere."


def rows_of(cells: list[tuple[str, float]], rows: int, columns: int) -> list[list[str]]:
    """Reassemble each visual line from the cells that carry it."""
    lines: list[list[str]] = []
    for row in range(rows):
        row_cells = cells[row * columns : (row + 1) * columns]
        for slot in range(PREVIEW_LINES_PER_ROW):
            parts = []
            for text, _ in row_cells:
                split = text.split("\n")
                parts.append(split[slot] if slot < len(split) else "")
            lines.append(parts)
    return lines


def test_the_text_survives_the_journey_intact() -> None:
    cells, _ = plan_preview(TEXT, 3, 3, SIZE)
    joined = " ".join(
        "".join(parts).strip() for parts in rows_of(cells, 3, 3) if "".join(parts).strip()
    )
    assert " ".join(joined.split()) == TEXT


def test_only_the_first_column_is_inset() -> None:
    """Interior seams carry no margin, so a word broken across two keys is
    separated by the bezel and nothing more."""
    cells, _ = plan_preview(TEXT, 3, 3, SIZE)
    first, middle, last = (inset for _, inset in cells[:3])
    assert first > 0
    assert middle == 0.0 and last == 0.0


def test_the_margin_is_exactly_one_character_wide() -> None:
    """Which is what puts the text flush against the interior seams: the first
    column ends at its right edge, the last begins at its left."""
    cells, point = plan_preview(TEXT, 3, 3, SIZE)
    draw = ImageDraw.Draw(Image.new("RGB", SIZE))
    advance = draw.textlength("M", font=load_font(point))
    assert cells[0][1] * SIZE[0] == pytest.approx(advance, abs=0.5)


def test_the_size_chosen_leaves_the_columns_nearly_flush() -> None:
    """Alignment is a property of the size: at 72px a 17pt advance leaves 0.4px
    over seven characters where 19pt leaves 3.4px over six. The smaller size is
    the tighter one, so the fitter looks past the largest that merely fits."""
    _, point = plan_preview(TEXT, 3, 3, SIZE)
    draw = ImageDraw.Draw(Image.new("RGB", SIZE))
    advance = draw.textlength("M", font=load_font(point))
    slack = SIZE[0] - int(SIZE[0] // advance) * advance
    assert slack < 2.0, f"{point}pt leaves {slack:.1f}px of slack per key"


def test_two_lines_fit_on_each_row() -> None:
    """One line wasted most of a 72px key."""
    cells, _ = plan_preview(TEXT, 3, 3, SIZE)
    assert any(len(text.split("\n")) == 2 for text, _ in cells)


def test_lines_break_at_words_when_they_can() -> None:
    """Best effort: the row is cut at column boundaries by character count, but
    where a *line* ends should still land on a space."""
    cells, _ = plan_preview("alpha beta gamma delta epsilon zeta", 3, 3, SIZE)
    for parts in rows_of(cells, 3, 3):
        line = "".join(parts).strip()
        if line:
            assert not line.endswith(" ")
            assert "  " not in line


def test_a_short_reply_gets_a_big_size() -> None:
    _, small = plan_preview(TEXT, 3, 3, SIZE)
    _, large = plan_preview("Remove it.", 3, 3, SIZE)
    assert large > small


def reading_order(cells: list[tuple[str, float]], rows: int, columns: int) -> str:
    """Everything shown, in the order a human reads it.

    Not the order the cells come in: a cell holds both of its row's lines, so
    concatenating cells interleaves line one with line two.
    """
    return "".join("".join(parts) for parts in rows_of(cells, rows, columns))


def test_a_word_longer_than_a_line_is_split_rather_than_dropped() -> None:
    monster = "supercalifragilisticexpialidocious" * 3
    cells, _ = plan_preview(monster, 3, 3, SIZE)
    assert reading_order(cells, 3, 3).startswith("supercalifragilistic")


def test_text_too_long_for_the_block_is_marked_as_clipped() -> None:
    cells, _ = plan_preview("word " * 500, 3, 3, SIZE)
    assert reading_order(cells, 3, 3).rstrip().endswith("…")


def test_an_empty_reply_leaves_the_block_blank() -> None:
    cells, _ = plan_preview("", 3, 3, SIZE)
    assert all(text == "" for text, _ in cells)
    assert len(cells) == 9


def test_a_degenerate_block_does_not_explode() -> None:
    assert plan_preview(TEXT, 0, 3, SIZE)[0] == []
    single, _ = plan_preview("hello", 1, 1, SIZE)
    assert len(single) == 1
