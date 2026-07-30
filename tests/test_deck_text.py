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


def rows_of(cells: list[str], rows: int, columns: int) -> list[list[str]]:
    """Reassemble each visual line from the cells that carry it."""
    lines: list[list[str]] = []
    for row in range(rows):
        row_cells = cells[row * columns : (row + 1) * columns]
        for slot in range(PREVIEW_LINES_PER_ROW):
            parts = []
            for text in row_cells:
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


def test_the_margin_is_a_literal_space_at_each_end() -> None:
    """Arithmetic rather than geometry: every key draws from its own left edge
    and holds the same number of characters, so nothing is left over to push
    text away from an interior seam."""
    cells, _ = plan_preview(TEXT, 3, 3, SIZE)
    lines = rows_of(cells, 3, 3)
    assert lines[0][0].startswith(" "), "left margin"
    assert "".join(lines[0]).endswith(" "), "right margin"


def test_every_key_carries_the_same_number_of_line_slots() -> None:
    """So a key with one line puts it where its neighbours put their first,
    rather than centring it between the two."""
    cells, _ = plan_preview("alpha beta", 3, 3, SIZE)
    assert {len(text.split("\n")) for text in cells} == {PREVIEW_LINES_PER_ROW}


def test_a_word_broken_across_lines_is_hyphenated() -> None:
    cells, _ = plan_preview("supercalifragilisticexpialidocious" * 2, 3, 3, SIZE)
    lines = ["".join(parts) for parts in rows_of(cells, 3, 3)]
    assert lines[0].rstrip().endswith("-"), lines[0]


def test_a_clean_break_is_not_hyphenated() -> None:
    cells, _ = plan_preview("alpha beta gamma delta epsilon zeta eta theta", 3, 3, SIZE)
    for line in ("".join(parts) for parts in rows_of(cells, 3, 3)):
        assert not line.rstrip().endswith("-"), line


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
    assert any(all(part.strip() for part in text.split("\n")) for text in cells)


def test_lines_break_at_words_when_they_can() -> None:
    """Best effort: the row is cut at column boundaries by character count, but
    where a *line* ends should still land on a space."""
    cells, _ = plan_preview("alpha beta gamma delta epsilon zeta", 3, 3, SIZE)
    for parts in rows_of(cells, 3, 3):
        line = "".join(parts).strip()
        if line:
            assert not line.endswith(" ")
            assert "  " not in line


def test_a_short_reply_is_never_set_smaller_than_a_long_one() -> None:
    """Not strictly larger: the size is quantised to whichever points leave the
    columns flush, so two lengths can legitimately land on the same one. What
    must never happen is a short reply being shrunk more than a long one."""
    _, small = plan_preview(TEXT, 3, 3, SIZE)
    _, large = plan_preview("Remove it.", 3, 3, SIZE)
    assert large >= small


def test_text_that_cannot_fit_at_full_size_is_set_smaller() -> None:
    _, big = plan_preview("Remove it.", 3, 3, SIZE)
    _, small = plan_preview(TEXT * 4, 3, 3, SIZE)
    assert small < big


def reading_order(cells: list[str], rows: int, columns: int) -> str:
    """Everything shown, in the order a human reads it.

    Not the order the cells come in: a cell holds both of its row's lines, so
    concatenating cells interleaves line one with line two.
    """
    return "".join("".join(parts) for parts in rows_of(cells, rows, columns))


def test_a_word_longer_than_a_line_is_split_rather_than_dropped() -> None:
    monster = "supercalifragilisticexpialidocious" * 3
    cells, _ = plan_preview(monster, 3, 3, SIZE)
    shown = reading_order(cells, 3, 3)
    # Where the break lands depends on the platform's font metrics, so assert
    # the properties rather than the position: the word starts at the start,
    # every fragment survives, and each break is marked.
    assert shown.lstrip().startswith("supercalifragilisti"), shown
    assert shown.replace("-", "").replace(" ", "") == monster
    assert shown.count("-") >= 1


def test_text_too_long_for_the_block_is_marked_as_clipped() -> None:
    cells, _ = plan_preview("word " * 500, 3, 3, SIZE)
    assert reading_order(cells, 3, 3).rstrip().endswith("…")


def test_an_empty_reply_leaves_the_block_blank() -> None:
    cells, _ = plan_preview("", 3, 3, SIZE)
    assert all(not text.strip() for text in cells)
    assert len(cells) == 9


def test_a_degenerate_block_does_not_explode() -> None:
    assert plan_preview(TEXT, 0, 3, SIZE)[0] == []
    single, _ = plan_preview("hello", 1, 1, SIZE)
    assert len(single) == 1


# ------------------------------------------------------- flush against edges


def ink_extent(face_text: str, point: int) -> tuple[int, int]:
    """Leftmost and rightmost columns of a rendered cell that contain ink."""
    from herdr_streamdeck.deck import ButtonFace, compose_foreground

    face = ButtonFace(summary=face_text, summary_size=point)
    alpha = compose_foreground(SIZE, face).getchannel("A").tobytes()
    columns = [
        x for x in range(SIZE[0]) if any(alpha[y * SIZE[0] + x] > 64 for y in range(SIZE[1]))
    ]
    return (columns[0], columns[-1]) if columns else (-1, -1)


@pytest.mark.parametrize("point", [13, 15, 17, 19, 20, 22])
def test_a_full_line_reaches_both_edges_of_the_key(point: int) -> None:
    """A key is only a whole number of characters wide if the advance happens to
    divide its width -- which depends on the type size and on the platform's
    rasteriser. Whatever it does not divide by used to sit unused on the right
    of every key. Characters are placed individually so the row spans the key
    exactly, at any size.
    """
    from PIL import Image, ImageDraw

    from herdr_streamdeck.deck import load_font

    draw = ImageDraw.Draw(Image.new("RGB", SIZE))
    per_key = int(SIZE[0] // draw.textlength("M", font=load_font(point)))
    left, right = ink_extent("M" * per_key, point)

    assert 0 <= left <= 2, f"{point}pt starts {left}px in"
    assert right >= SIZE[0] - 3, f"{point}pt stops {SIZE[0] - right}px short"


def test_a_leading_space_still_indents() -> None:
    """The margin is a real character, so it still holds its column."""
    left, _ = ink_extent(" MMMMMM", 17)
    assert left > 6, "the margin space collapsed"
