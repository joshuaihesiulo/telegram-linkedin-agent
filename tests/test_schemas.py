"""Pydantic gates: analysis + draft validation, 3000-char limit."""
import pytest

from app.schemas import DraftPost, VideoAnalysis


def _analysis(**kw):
    base = dict(
        transcript="hello world",
        key_points=["a", "b", "c"],
        main_takeaway="takeaway",
        tone="direct",
    )
    base.update(kw)
    return VideoAnalysis.model_validate(base)


def test_analysis_requires_3_key_points():
    with pytest.raises(Exception):
        _analysis(key_points=["only-one"])


def test_analysis_no_invented_fields_needed():
    a = _analysis(uncertain_items=["unsure name at 1:23"])
    assert a.uncertain_items == ["unsure name at 1:23"]


def test_draft_hashtag_normalization():
    d = DraftPost(hook="H", body="B", cta="C",
                  hashtags=["ai", "#video", "marketing"])
    assert d.hashtags == ["#ai", "#video", "#marketing"]
    assert d.full_text().endswith("#ai #video #marketing")


def test_draft_char_count():
    d = DraftPost(hook="H" * 10, body="B" * 800, cta="C" * 20,
                  hashtags=["#a", "#b", "#c"])
    assert d.char_count() == len(d.full_text())
    assert d.char_count() < 3000
