"""Testes do Version Inspector com HTTP mockado (nenhuma rede real é tocada)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import requests

from ultranx.config import MODALITY_FULL, MODALITY_STANDARD, Settings
from ultranx.core import version_inspector
from ultranx.core.errors import NetworkError, RemoteDataError
from ultranx.core.version_inspector import compare_versions, inspect

SHA = "a" * 64


@pytest.fixture()
def settings() -> Settings:
    return Settings(base_url="https://host/rox", http_timeout=5.0, skip_hash_check=False)


class FakeResponse:
    """Dublê mínimo de ``requests.Response``."""

    def __init__(self, text: str = "", status: int = 200) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            error.response = self  # type: ignore[attr-defined]
            raise error

    def json(self):
        return json.loads(self.text)


def _manifest(**overrides) -> str:
    document = {
        "version": "1.4.2",
        "packages": {
            MODALITY_STANDARD: {
                "url": "https://host/rox/rox-standard-1.4.2.zip",
                "sha256": SHA,
                "size": 100,
            },
            MODALITY_FULL: {
                "url": "rox-full-1.4.2.zip",
                "sha256": SHA,
                "size": 200,
            },
        },
    }
    document.update(overrides)
    return json.dumps(document)


def _route(monkeypatch, version_body: str, manifest_body: str | None):
    def fake_get(url, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        if url.endswith("packetVersion.txt"):
            return FakeResponse(version_body)
        if url.endswith("manifest.json"):
            if manifest_body is None:
                raise requests.exceptions.ConnectionError("sem manifest")
            return FakeResponse(manifest_body)
        raise AssertionError(f"URL inesperada: {url}")

    monkeypatch.setattr(version_inspector.requests, "get", fake_get)


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("1.4.2", "1.4.1", 1),
        ("1.4.1", "1.4.2", -1),
        ("1.4.2", "1.4.2", 0),
        ("v1.4.2", "1.4.2", 0),
        ("1.10.0", "1.9.0", 1),
        ("2.0", "1.99.99", 1),
    ],
)
def test_compare_versions(left, right, expected):
    assert compare_versions(left, right) == expected


def test_inspect_with_manifest(monkeypatch, settings, tmp_path: Path):
    (tmp_path / "packetVersion.txt").write_text("1.0.0\n", encoding="utf-8")
    _route(monkeypatch, "1.4.2\n", _manifest())

    report = inspect(tmp_path, settings)

    assert report.local_version == "1.0.0"
    assert report.remote_version == "1.4.2"
    assert report.manifest_available
    assert report.update_available
    assert not report.is_downgrade
    assert report.available_modalities == (MODALITY_STANDARD, MODALITY_FULL)
    assert report.package_for(MODALITY_STANDARD).sha256 == SHA


def test_relative_manifest_url_is_resolved_against_base(monkeypatch, settings, tmp_path):
    _route(monkeypatch, "1.4.2", _manifest())
    report = inspect(tmp_path, settings)
    assert report.package_for(MODALITY_FULL).url == (
        "https://host/rox/rox-full-1.4.2.zip"
    )


def test_inspect_without_manifest_falls_back_to_convention(
    monkeypatch, settings, tmp_path: Path
):
    _route(monkeypatch, "1.4.2", None)
    report = inspect(tmp_path, settings)

    assert not report.manifest_available
    assert report.package_for(MODALITY_STANDARD).sha256 is None
    assert report.package_for(MODALITY_STANDARD).url.endswith("rox-standard-1.4.2.zip")


def test_missing_local_version_means_update_available(monkeypatch, settings, tmp_path):
    _route(monkeypatch, "1.4.2", _manifest())
    report = inspect(tmp_path, settings)
    assert report.local_version is None
    assert report.update_available
    assert not report.is_downgrade


def test_downgrade_is_flagged(monkeypatch, settings, tmp_path: Path):
    (tmp_path / "packetVersion.txt").write_text("2.0.0", encoding="utf-8")
    _route(monkeypatch, "1.4.2", _manifest())
    report = inspect(tmp_path, settings)
    assert report.is_downgrade
    assert not report.update_available


def test_invalid_sha256_is_discarded(monkeypatch, settings, tmp_path: Path):
    body = _manifest(
        packages={MODALITY_STANDARD: {"url": "a.zip", "sha256": "xyz", "size": 10}}
    )
    _route(monkeypatch, "1.4.2", body)
    report = inspect(tmp_path, settings)
    assert report.package_for(MODALITY_STANDARD).sha256 is None


def test_manifest_without_usable_entries_degrades_to_convention(
    monkeypatch, settings, tmp_path: Path
):
    """Entrada sem 'url' é descartada; manifest vazio cai para a convenção."""
    body = _manifest(packages={MODALITY_STANDARD: {"sha256": SHA}})
    _route(monkeypatch, "1.4.2", body)

    report = inspect(tmp_path, settings)

    assert not report.manifest_available
    assert report.available_modalities == (MODALITY_STANDARD, MODALITY_FULL)
    assert report.package_for(MODALITY_STANDARD).sha256 is None


def test_partially_valid_manifest_keeps_only_valid_entry(
    monkeypatch, settings, tmp_path: Path
):
    body = _manifest(
        packages={
            MODALITY_STANDARD: {"url": "ok.zip", "sha256": SHA, "size": 10},
            MODALITY_FULL: {"sha256": SHA},
        }
    )
    _route(monkeypatch, "1.4.2", body)

    report = inspect(tmp_path, settings)

    assert report.manifest_available
    assert report.available_modalities == (MODALITY_STANDARD,)


def test_unknown_modality_raises(monkeypatch, settings, tmp_path: Path):
    _route(monkeypatch, "1.4.2", _manifest())
    report = inspect(tmp_path, settings)
    with pytest.raises(RemoteDataError):
        report.package_for("inexistente")


def test_empty_remote_version_raises(monkeypatch, settings, tmp_path: Path):
    _route(monkeypatch, "\n  \n", _manifest())
    with pytest.raises(RemoteDataError):
        inspect(tmp_path, settings)


def test_non_numeric_remote_version_raises(monkeypatch, settings, tmp_path: Path):
    _route(monkeypatch, "<html>erro</html>", _manifest())
    with pytest.raises(RemoteDataError):
        inspect(tmp_path, settings)


def test_http_error_becomes_network_error(monkeypatch, settings, tmp_path: Path):
    def fake_get(url, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        return FakeResponse("nope", status=503)

    monkeypatch.setattr(version_inspector.requests, "get", fake_get)
    with pytest.raises(NetworkError):
        inspect(tmp_path, settings)


def test_timeout_becomes_network_error(monkeypatch, settings, tmp_path: Path):
    def fake_get(url, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        raise requests.exceptions.Timeout("lento")

    monkeypatch.setattr(version_inspector.requests, "get", fake_get)
    with pytest.raises(NetworkError):
        inspect(tmp_path, settings)


def test_malformed_manifest_json_degrades(monkeypatch, settings, tmp_path: Path):
    _route(monkeypatch, "1.4.2", "{ isso não é json")
    report = inspect(tmp_path, settings)
    assert not report.manifest_available
