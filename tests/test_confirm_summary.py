"""Testes do resumo do diálogo de confirmação.

O diálogo é a trava de segurança antes de qualquer remoção: se ele não couber na
tela ou descrever o plano de forma imprecisa, a trava não protege ninguém.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ultranx.core.sanitizer import build_plan
from ultranx.ui.main_window import MainWindow


@pytest.fixture()
def sd(tmp_path: Path) -> Path:
    """Cartão com pastas de raiz removíveis e muitos filhos dentro de switch/."""
    for legacy in ("atmosphere", "bootloader", "config"):
        (tmp_path / legacy).mkdir()
    (tmp_path / "payload.bin").write_bytes(b"p")

    (tmp_path / "switch").mkdir()
    for index in range(30):
        (tmp_path / "switch" / f"app{index:02d}").mkdir()
    (tmp_path / "switch" / "JKSV").mkdir()
    (tmp_path / "switch" / "prod.keys").write_text("k", encoding="utf-8")

    (tmp_path / "Nintendo").mkdir()
    (tmp_path / "tico" / "roms").mkdir(parents=True)
    return tmp_path


def test_nested_items_are_collapsed_into_a_count(sd: Path):
    """30 filhos de switch/ viram uma linha, não 30 — é o que cabe na tela."""
    removals, _, _ = MainWindow._summarize_plan(build_plan(sd), sd)
    lines = removals.splitlines()

    assert len(lines) <= 8
    assert any("switch/ — 30 item(ns) de dentro" in line for line in lines)
    assert any("(a pasta permanece)" in line for line in lines)


def test_root_removals_are_listed_individually(sd: Path):
    removals, _, _ = MainWindow._summarize_plan(build_plan(sd), sd)
    for expected in ("atmosphere", "bootloader", "config", "payload.bin"):
        assert f"• {expected}" in removals


def test_preserved_list_comes_from_the_plan(sd: Path):
    """Não é texto fixo: o que aparece é o que o plano realmente preserva."""
    _, preserved, _ = MainWindow._summarize_plan(build_plan(sd), sd)

    assert "switch/JKSV" in preserved
    assert "switch/prod.keys" in preserved
    assert "Nintendo" in preserved
    assert "tico" in preserved


def test_detailed_text_has_every_item(sd: Path):
    plan = build_plan(sd)
    _, _, detailed = MainWindow._summarize_plan(plan, sd)

    assert "REMOVER:" in detailed
    assert "PRESERVAR:" in detailed
    for item in plan.items:
        assert str(item.path.relative_to(sd)) in detailed
    for kept in plan.preserved:
        assert str(kept.relative_to(sd)) in detailed


def test_summary_of_clean_card(tmp_path: Path):
    (tmp_path / "Nintendo").mkdir()
    removals, preserved, _ = MainWindow._summarize_plan(build_plan(tmp_path), tmp_path)

    assert "nada a remover" in removals
    assert "Nintendo" in preserved
