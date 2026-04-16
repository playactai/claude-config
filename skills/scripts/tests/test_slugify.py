"""Regression tests for planner._slugify.

Guards bug_011 from the 2026-04-17 ultrareview: truncating to 60 chars before
rstrip() leaves a trailing hyphen when the boundary lands on one, producing
filenames like 2026-04-17-foo-.md.
"""

import pytest

from skills.planner.orchestrator.planner import _slugify


class TestSlugifyBasics:
    def test_lowercases_and_hyphenates_spaces(self):
        assert _slugify("My Plan") == "my-plan"

    def test_collapses_runs_of_separators(self):
        assert _slugify("a   b") == "a-b"
        assert _slugify("a---b") == "a-b"
        assert _slugify("a__b") == "a-b"

    def test_strips_leading_and_trailing_hyphens(self):
        assert _slugify("--foo--") == "foo"
        assert _slugify("!!!foo!!!") == "foo"

    def test_empty_input_returns_plan(self):
        assert _slugify("") == "plan"

    def test_all_non_alphanumeric_returns_plan(self):
        assert _slugify("!!!") == "plan"
        assert _slugify("---") == "plan"
        assert _slugify("///") == "plan"


class TestSlugifyTruncation:
    """The bug_011 fix: rstrip AFTER truncation."""

    def test_truncation_at_hyphen_does_not_leave_trailing_hyphen(self):
        """Bug report's reproducer: 59 'a' + ' b' → ..a-b with [:60] landing on '-'."""
        slug = _slugify("a" * 59 + " b")
        assert not slug.endswith("-"), slug
        # Sanity: still bounded.
        assert len(slug) <= 60

    @pytest.mark.parametrize("word_len", [2, 3, 4, 5, 6, 7, 8])
    def test_hyphen_boundary_sweep_never_leaves_trailing_hyphen(self, word_len: int):
        """For a range of repeated-word widths, the 60-char cut sometimes lands
        on a hyphen. None of the outputs may end in '-'.
        """
        word = "a" * word_len
        text = " ".join([word] * 20)  # Long enough to trigger truncation.
        slug = _slugify(text)
        assert len(slug) <= 60
        assert not slug.endswith("-"), (word_len, slug)

    def test_slug_length_capped_at_60(self):
        """Output is bounded by 60 characters regardless of input length."""
        for length in range(50, 200, 5):
            slug = _slugify("a" * length)
            assert len(slug) <= 60
