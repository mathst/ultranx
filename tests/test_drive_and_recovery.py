"""Testes do Drive Detector e do módulo Recovery & Safety."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

import pytest

from ultranx.core import drive_detector, recovery
from ultranx.core.errors import (
    DriveDisconnectedError,
    DriveError,
    NetworkError,
    SanitizerError,
)

FakePartition = namedtuple("FakePartition", "device mountpoint fstype opts")
FakeUsage = namedtuple("FakeUsage", "total used free percent")


@pytest.fixture()
def switch_sd(tmp_path: Path) -> Path:
    (tmp_path / "atmosphere").mkdir()
    (tmp_path / "Nintendo").mkdir()
    (tmp_path / "packetVersion.txt").write_text("1.4.2\n", encoding="utf-8")
    return tmp_path


def _patch_psutil(monkeypatch, partitions: list[FakePartition]) -> None:
    monkeypatch.setattr(
        drive_detector.psutil, "disk_partitions", lambda all=False: partitions
    )
    monkeypatch.setattr(
        drive_detector.psutil,
        "disk_usage",
        lambda path: FakeUsage(64 * 1024**3, 1024**3, 63 * 1024**3, 1.5),
    )


# --- leitura de versão ------------------------------------------------------


def test_read_local_version(switch_sd: Path):
    assert drive_detector.read_local_version(switch_sd) == "1.4.2"


def test_read_local_version_missing_file(tmp_path: Path):
    assert drive_detector.read_local_version(tmp_path) is None


def test_read_local_version_empty_file(tmp_path: Path):
    (tmp_path / "packetVersion.txt").write_text("   \n", encoding="utf-8")
    assert drive_detector.read_local_version(tmp_path) is None


# --- identificação da raiz --------------------------------------------------


def test_looks_like_switch_root(switch_sd: Path):
    assert drive_detector.looks_like_switch_root(switch_sd)


def test_blank_dir_is_not_switch_root(tmp_path: Path):
    assert not drive_detector.looks_like_switch_root(tmp_path)


def test_marker_detection_is_case_insensitive(tmp_path: Path):
    (tmp_path / "ATMOSPHERE").mkdir()
    assert drive_detector.looks_like_switch_root(tmp_path)


# --- varredura --------------------------------------------------------------


def test_scan_filters_by_fstype_and_removability(monkeypatch, switch_sd: Path):
    _patch_psutil(
        monkeypatch,
        [
            FakePartition("E:\\", str(switch_sd), "exFAT", "rw,removable"),
            FakePartition("C:\\", str(switch_sd), "NTFS", "rw,fixed"),
            FakePartition("D:\\", str(switch_sd), "FAT32", "rw,fixed"),
        ],
    )
    candidates = drive_detector.scan_removable_drives()

    assert len(candidates) == 1
    assert candidates[0].fstype == "exFAT"
    assert candidates[0].is_switch_root
    assert candidates[0].local_version == "1.4.2"


def test_scan_accepts_linux_media_mountpoints(monkeypatch, switch_sd: Path):
    monkeypatch.setattr(
        drive_detector,
        "_is_removable",
        lambda partition: True,
    )
    _patch_psutil(monkeypatch, [FakePartition("/dev/sdb1", str(switch_sd), "vfat", "rw")])
    assert len(drive_detector.scan_removable_drives()) == 1


def test_scan_survives_psutil_failure(monkeypatch):
    def boom(all=False):  # noqa: ANN001, ARG001
        raise OSError("sem acesso")

    monkeypatch.setattr(drive_detector.psutil, "disk_partitions", boom)
    assert drive_detector.scan_removable_drives() == ()


def test_detect_switch_root_requires_single_match(monkeypatch, switch_sd: Path):
    _patch_psutil(
        monkeypatch,
        [FakePartition("E:\\", str(switch_sd), "exFAT", "rw,removable")],
    )
    detected = drive_detector.detect_switch_root()
    assert detected is not None
    assert detected.mountpoint == switch_sd.resolve()


def test_detect_switch_root_ambiguous_returns_none(monkeypatch, tmp_path: Path):
    first, second = tmp_path / "a", tmp_path / "b"
    for root in (first, second):
        (root / "atmosphere").mkdir(parents=True)
    _patch_psutil(
        monkeypatch,
        [
            FakePartition("E:\\", str(first), "exFAT", "rw,removable"),
            FakePartition("F:\\", str(second), "exFAT", "rw,removable"),
        ],
    )
    assert drive_detector.detect_switch_root() is None


# --- seleção manual ---------------------------------------------------------


def test_validate_manual_root_accepts_writable_dir(tmp_path: Path):
    candidate = drive_detector.validate_manual_root(tmp_path)
    assert candidate.mountpoint == tmp_path.resolve()
    assert not (tmp_path / ".ultranx-write-test").exists()


def test_validate_manual_root_rejects_missing(tmp_path: Path):
    with pytest.raises(DriveError):
        drive_detector.validate_manual_root(tmp_path / "ausente")


def test_validate_manual_root_rejects_file(tmp_path: Path):
    target = tmp_path / "arquivo.txt"
    target.write_text("x", encoding="utf-8")
    with pytest.raises(DriveError):
        drive_detector.validate_manual_root(target)


def test_candidate_label_includes_version(monkeypatch, switch_sd: Path):
    _patch_psutil(
        monkeypatch, [FakePartition("E:\\", str(switch_sd), "exFAT", "rw,removable")]
    )
    label = drive_detector.scan_removable_drives()[0].label
    assert "SD Switch" in label
    assert "1.4.2" in label


# --- recovery ---------------------------------------------------------------


def test_failure_report_uses_domain_guidance(switch_sd: Path):
    report = recovery.build_failure_report(
        NetworkError("caiu a rede"), switch_sd, "download"
    )
    assert report.message == "caiu a rede"
    assert "conexão" in report.guidance
    assert "download" in report.as_text()


def test_failure_report_wraps_unexpected_exception(switch_sd: Path):
    report = recovery.build_failure_report(ValueError("bug"), switch_sd, "extração")
    assert "ValueError" in report.message
    assert report.sd_dirty


def test_disconnection_always_marks_sd_dirty(switch_sd: Path):
    report = recovery.build_failure_report(
        DriveDisconnectedError("removeu o cartão"), switch_sd, "inspeção"
    )
    assert report.sd_dirty
    assert "estado parcial" in report.as_text()


def test_network_failure_during_inspection_keeps_sd_clean(switch_sd: Path):
    report = recovery.build_failure_report(NetworkError("timeout"), switch_sd, "inspeção")
    assert not report.sd_dirty


def test_preserve_log_copies_into_whitelisted_dir(
    monkeypatch, switch_sd: Path, tmp_path: Path
):
    fake_log = tmp_path / "ultranx.log"
    fake_log.write_text("linha de log\n", encoding="utf-8")
    monkeypatch.setattr(recovery, "log_file_path", lambda: fake_log)

    copied = recovery.preserve_log(switch_sd, "limpeza")

    assert copied is not None
    assert copied.parent.name == recovery.LOG_COPY_DIR
    assert copied.read_text(encoding="utf-8") == "linha de log\n"


def test_preserve_log_returns_none_without_sd(monkeypatch, tmp_path: Path):
    fake_log = tmp_path / "ultranx.log"
    fake_log.write_text("x", encoding="utf-8")
    monkeypatch.setattr(recovery, "log_file_path", lambda: fake_log)
    assert recovery.preserve_log(None, "erro") is None
    assert recovery.preserve_log(tmp_path / "ausente", "erro") is None


def test_preserve_log_returns_none_when_log_missing(
    monkeypatch, switch_sd: Path, tmp_path: Path
):
    monkeypatch.setattr(recovery, "log_file_path", lambda: tmp_path / "nao-existe.log")
    assert recovery.preserve_log(switch_sd, "erro") is None


def test_finalize_media_confirms_readable_card(switch_sd: Path):
    assert recovery.finalize_media(switch_sd)


def test_eject_guidance_is_actionable():
    assert "cart" in recovery.eject_guidance()


def test_sanitizer_error_guidance_mentions_whitelist():
    report = recovery.build_failure_report(SanitizerError("falhou"), None, "limpeza")
    assert "whitelist" in report.guidance
