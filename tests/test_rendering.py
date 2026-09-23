from rich.cells import cell_len
import pytest

from mini_notes.rendering import layout_document, line_for_anchor, wrap_text


@pytest.mark.parametrize('width', [12, 24, 44, 48, 76, 108])
@pytest.mark.parametrize('text', [
    'The office had a quiet afternoon. A small note waited beside the keyboard, untouched.',
    '她推开门，看见桌上的信。“这封信是你的？”窗外的雨停了，灯光映在窗上。',
    '这是 mixed content with English words，and numbers 2026，保持原文与顺序。',
    'Café déjà vu: we are testing long-hyphenated-words and extraordinarilylongtokens.',
])
def test_wrap_preserves_source_and_column_bounds(text, width):
    lines = wrap_text(text, width)
    assert ''.join(line for line, _ in lines) == text
    assert all(cell_len(line) <= width for line, _ in lines)
    for line, offset in lines:
        assert text[offset:offset+len(line)] == line
    # A word that can fit on a row should not be split across a row boundary.
    for (before, _), (after, _) in zip(lines, lines[1:]):
        if before and after and before[-1].isascii() and before[-1].isalpha() and after[0].isascii() and after[0].isalpha():
            word = before.split()[-1] + after.split()[0]
            assert cell_len(word) > width


def test_cjk_common_punctuation_not_at_forbidden_boundary():
    text = '她推开门，看见桌上的信。“这封信是你的？”窗外的雨停了，灯光映在窗上。' * 8
    for width in range(14, 85):
        lines = [line for line, _ in wrap_text(text, width)]
        assert ''.join(lines) == text
        assert all(not row or row[0] not in '，。！？；：、）》】”' for row in lines)
        assert all(not row or row[-1] not in '（《【“' for row in lines)
        assert all(cell_len(row) <= width for row in lines)


def test_explicit_lines_keep_correct_source_offsets():
    text = 'first line\nsecond line\r\nthird line'
    lines = wrap_text(text, 40)
    assert lines == [('first line', 0), ('second line', 11), ('third line', 24)]


def test_anchor_follows_same_paragraph_on_resize_and_cache_reuses_layout():
    paragraphs = ('first paragraph '*20, '第二段，保留语义阅读位置。'*40, 'third paragraph '*20)
    rows = layout_document(paragraphs, 76)
    assert layout_document(paragraphs, 76) is rows
    narrower = layout_document(paragraphs, 44)
    index = line_for_anchor(narrower, {'paragraph': 1, 'offset': 150})
    assert narrower[index].paragraph == 1
    assert narrower[index].offset <= 150
    next_rows = [r for r in narrower[index+1:] if r.paragraph == 1]
    assert not next_rows or next_rows[0].offset > 150


def test_combining_characters_flags_and_tabs_remain_intact():
    from mini_notes.rendering import graphemes
    assert graphemes('🇨🇳👩‍💻é') == ['🇨🇳', '👩‍💻', 'é']
    text = 'The Café had a naïve visitor. 🇨🇳\t👩‍💻'
    for width in (8, 12, 24):
        rows = wrap_text(text, width)
        assert ''.join(row for row, _ in rows) == text
        assert all(cell_len(row.replace('\t', '    ')) <= width for row, _ in rows)
        assert all('🇨' not in row or '🇨🇳' in row for row, _ in rows)
