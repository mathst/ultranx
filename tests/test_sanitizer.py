"""Testes do Safe Sanitizer — a whitelist é o invariante crítico do projeto."""

from __future__ import annotations

from pathlib import Path

import pytest

from ultranx.core.errors import DriveDisconnectedError
from ultranx.core.sanitizer import (
    CleanupItem,
    CleanupPlan,
    build_plan,
    execute_plan,
    is_protected,
)


@pytest.fixture()
def sd(tmp_path: Path) -> Path:
    """Cartão sintético com pastas removíveis e protegidas."""
    for legacy in ("atmosphere", "bootloader", "switch", "config", "sept"):
        (tmp_path / legacy / "sub").mkdir(parents=True)
        (tmp_path / legacy / "sub" / "arquivo.bin").write_bytes(b"legado")

    (tmp_path / "Nintendo" / "Contents").mkdir(parents=True)
    (tmp_path / "emummc").mkdir()
    (tmp_path / "tico" / "roms").mkdir(parents=True)
    (tmp_path / "tico" / "roms" / "jogo.gba").write_bytes(b"rom")
    (tmp_path / "themes" / "ThemezerNX").mkdir(parents=True)
    (tmp_path / "themes" / "ThemezerNX" / "t.szs").write_bytes(b"tema")
    (tmp_path / "mods2" / "jogo").mkdir(parents=True)
    (tmp_path / "MinhaPastaCustom").mkdir()

    # Dentro de switch/: dado do usuário convive com apps que o pacote repõe.
    (tmp_path / "switch" / "JKSV").mkdir(parents=True)
    (tmp_path / "switch" / "JKSV" / "backup.sav").write_bytes(b"save")
    (tmp_path / "switch" / "EdiZon").mkdir()
    (tmp_path / "switch" / "NX-Activity-Log").mkdir()
    (tmp_path / "switch" / "tinfoil").mkdir()
    (tmp_path / "switch" / "prod.keys").write_text("chave", encoding="utf-8")
    (tmp_path / "switch" / "daybreak.nro").write_bytes(b"app")

    (tmp_path / "config" / "sys-clk").mkdir(parents=True)
    (tmp_path / "config" / "sys-clk" / "config.ini").write_text("x", encoding="utf-8")

    (tmp_path / "payload.bin").write_bytes(b"payload")
    (tmp_path / "homebrew.nro").write_bytes(b"nro")
    (tmp_path / "packetVersion.txt").write_text("1.0.0\n", encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize(
    "protected",
    ["Nintendo", "emummc", "tico", "themes", "mods2", "homebrew.nro", "MinhaPastaCustom"],
)
def test_whitelist_entries_are_protected(sd: Path, protected: str):
    assert is_protected(sd, sd / protected)


@pytest.mark.parametrize(
    "nested",
    ["tico/roms", "tico/roms/jogo.gba", "themes/ThemezerNX", "Nintendo/Contents"],
)
def test_nested_whitelist_paths_are_protected(sd: Path, nested: str):
    assert is_protected(sd, sd / nested)


def test_case_variants_are_protected(sd: Path):
    assert is_protected(sd, sd / "NINTENDO")
    assert is_protected(sd, sd / "TICO" / "ROMS")


@pytest.mark.parametrize(
    "removable",
    [
        "atmosphere",
        "bootloader",
        "switch",  # a pasta em si não é protegida: é limpa item a item
        "switch/tinfoil",
        "config",
        "config/sys-clk",
        "switch/daybreak.nro",
        "sept",
        "payload.bin",
        "packetVersion.txt",
    ],
)
def test_legacy_entries_are_not_protected(sd: Path, removable: str):
    assert not is_protected(sd, sd / removable)


@pytest.mark.parametrize(
    "protected",
    [
        "switch/JKSV",
        "switch/JKSV/backup.sav",
        "switch/EdiZon",
        "switch/NX-Activity-Log",
        "switch/prod.keys",
    ],
)
def test_user_data_inside_removable_dirs_is_protected(sd: Path, protected: str):
    """Keys e saves não são repostos por nenhum pacote."""
    assert is_protected(sd, sd / protected)


def test_keys_are_protected_at_any_depth(sd: Path):
    assert is_protected(sd, sd / "switch" / "qualquer" / "title.keys")
    assert is_protected(sd, sd / "atmosphere" / "fundo" / "prod.keys")


def test_root_itself_is_protected(sd: Path):
    assert is_protected(sd, sd)


def test_paths_outside_root_are_protected(sd: Path):
    assert is_protected(sd, sd.parent / "documentos-importantes")


def test_traversal_is_protected(sd: Path):
    assert is_protected(sd, sd / ".." / "fora")


def test_build_plan_selects_only_legacy(sd: Path):
    plan = build_plan(sd)
    removed = {item.path.name.casefold() for item in plan.items}
    assert removed == {
        "atmosphere",
        "bootloader",
        "sept",
        "payload.bin",
        "packetversion.txt",
        "config",
        # switch/ não é removida por inteiro: entram só os filhos descartáveis.
        "sub",
        "tinfoil",
        "daybreak.nro",
    }
    preserved = {path.name.casefold() for path in plan.preserved}
    assert {
        "nintendo",
        "emummc",
        "tico",
        "themes",
        "mods2",
        "homebrew.nro",
        "minhapastacustom",
        "jksv",
        "edizon",
        "nx-activity-log",
        "prod.keys",
    } <= preserved


def test_execute_plan_removes_and_preserves(sd: Path):
    plan = build_plan(sd)
    removed = execute_plan(plan)

    assert len(removed) == len(plan.items)
    assert not (sd / "atmosphere").exists()
    assert not (sd / "payload.bin").exists()
    assert not (sd / "switch" / "tinfoil").exists()
    assert not (sd / "switch" / "daybreak.nro").exists()
    assert (sd / "Nintendo" / "Contents").is_dir()
    assert (sd / "tico" / "roms" / "jogo.gba").read_bytes() == b"rom"
    assert (sd / "themes" / "ThemezerNX" / "t.szs").exists()
    assert (sd / "homebrew.nro").exists()
    # switch/ sobrevive, so perde o conteudo que o pacote repoe.
    assert (sd / "switch").is_dir()
    assert (sd / "switch" / "JKSV" / "backup.sav").read_bytes() == b"save"
    assert (sd / "switch" / "prod.keys").exists()
    assert (sd / "switch" / "EdiZon").is_dir()
    assert (sd / "switch" / "NX-Activity-Log").is_dir()
    # config/ é substituída pelo pacote: presets antigos não podem sobrar.
    assert not (sd / "config").exists()


def test_execute_plan_reports_progress(sd: Path):
    plan = build_plan(sd)
    events: list[tuple[int, int, str]] = []
    execute_plan(plan, progress=lambda *args: events.append(args))

    assert len(events) == len(plan.items)
    assert events[0][0] == 1
    assert events[-1][0] == events[-1][1] == len(plan.items)


def test_execute_plan_honours_cancellation(sd: Path):
    plan = build_plan(sd)
    calls = {"n": 0}

    def cancel_after_first() -> bool:
        calls["n"] += 1
        return calls["n"] > 1

    removed = execute_plan(plan, should_cancel=cancel_after_first)
    assert len(removed) == 1


def test_execute_plan_guard_blocks_forged_plan(sd: Path):
    """Camada 3: um plano forjado contra a whitelist não remove nada."""
    forged = CleanupPlan(
        sd_root=sd,
        items=(
            CleanupItem(sd / "Nintendo", True, "forjado"),
            CleanupItem(sd / "tico" / "roms", True, "forjado"),
            CleanupItem(sd.parent / "fora", True, "forjado"),
        ),
        preserved=(),
    )
    assert execute_plan(forged) == ()
    assert (sd / "Nintendo").is_dir()
    assert (sd / "tico" / "roms").is_dir()


def test_build_plan_on_missing_root_raises(tmp_path: Path):
    with pytest.raises(DriveDisconnectedError):
        build_plan(tmp_path / "cartao-ausente")


def test_plan_describe_mentions_root(sd: Path):
    assert str(sd) in build_plan(sd).describe()


def test_empty_plan_describe(tmp_path: Path):
    (tmp_path / "Nintendo").mkdir()
    plan = build_plan(tmp_path)
    assert plan.is_empty
    assert "já está limpo" in plan.describe()
