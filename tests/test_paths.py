"""Testes dos helpers de caminho (contenção, case-insensitivity, traversal)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ultranx.core.paths import (
    human_size,
    is_within,
    join_within,
    matches_subpath,
    normalized_parts,
    raw_parts,
    relative_parts,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Atmosphere/Contents", ("atmosphere", "contents")),
        ("Atmosphere\\Contents", ("atmosphere", "contents")),
        ("./themes//ThemezerNX/", ("themes", "themezernx")),
        ("", ()),
        ("../etc/passwd", ()),
        ("themes/../../etc", ()),
    ],
)
def test_normalized_parts_casefolds_and_rejects_traversal(raw, expected):
    assert normalized_parts(raw) == expected


def test_raw_parts_preserves_original_case():
    assert raw_parts("Themes/ThemezerNX") == ("Themes", "ThemezerNX")


def test_is_within_accepts_root_and_children(tmp_path: Path):
    child = tmp_path / "atmosphere" / "contents"
    assert is_within(tmp_path, tmp_path)
    assert is_within(tmp_path, child)


def test_is_within_rejects_sibling_and_parent(tmp_path: Path):
    sibling = tmp_path.parent / "outro-cartao"
    assert not is_within(tmp_path, sibling)
    assert not is_within(tmp_path, tmp_path.parent)


def test_relative_parts_returns_empty_when_outside(tmp_path: Path):
    assert relative_parts(tmp_path, tmp_path.parent / "x") == ()


def test_relative_parts_of_root_is_empty(tmp_path: Path):
    assert relative_parts(tmp_path, tmp_path) == ()


def test_join_within_blocks_zip_slip(tmp_path: Path):
    assert join_within(tmp_path, "../../evil.bin") is None
    assert join_within(tmp_path, "") is None


def test_join_within_keeps_original_case_inside_root(tmp_path: Path):
    target = join_within(tmp_path, "Themes/ThemezerNX/theme.szs")
    assert target is not None
    assert target.name == "theme.szs"
    assert "ThemezerNX" in str(target)
    assert is_within(tmp_path, target)


def test_matches_subpath():
    assert matches_subpath(("tico", "roms", "gba"), ("tico", "roms"))
    assert matches_subpath(("tico", "roms"), ("tico", "roms"))
    assert not matches_subpath(("tico",), ("tico", "roms"))
    assert not matches_subpath(("themes", "outro"), ("themes", "themezernx"))


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, "0 B"), (512, "512 B"), (1024, "1.0 KB"), (1024 * 1024, "1.0 MB")],
)
def test_human_size(value, expected):
    assert human_size(value) == expected


def test_human_size_clamps_negatives():
    assert human_size(-5) == "0 B"
