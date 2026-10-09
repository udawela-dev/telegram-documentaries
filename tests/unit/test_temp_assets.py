"""Unit tests for TempAssets (Phase 7)."""
from __future__ import annotations

import logging
from pathlib import Path

import pytest

from src.temp_assets import TempAssets, TempAssetsError


def test_track_and_purge_happy_path(tmp_path):
    assets = TempAssets()
    p1 = tmp_path / "a.wav"
    p2 = tmp_path / "b.ogg"
    p1.write_bytes(b"1")
    p2.write_bytes(b"2")
    assets.track(101, p1)
    assets.track(101, p2)
    assets.purge(101)
    assert not p1.exists()
    assert not p2.exists()


def test_purge_already_missing_file_logs_and_continues(tmp_path, caplog):
    caplog.set_level(logging.INFO)
    assets = TempAssets()
    p = tmp_path / "gone.wav"
    assets.track(101, p)
    assets.purge(101)
    assert not p.exists()
    # logged
    assert any("temp_asset_unlinked" in rec.message or "temp_asset_purge_failed" in rec.message for rec in caplog.records)


def test_purge_non_missing_failure_is_logged_and_continues(tmp_path, caplog, monkeypatch):
    """A purge unlink failure (e.g. PermissionError) is logged loud and never
    raised — the sweep must never kill the loop."""
    caplog.set_level(logging.ERROR)
    assets = TempAssets()
    p = tmp_path / "stuck.wav"
    p.write_bytes(b"x")

    def _deny(self):
        raise PermissionError("stuck file")

    monkeypatch.setattr(Path, "unlink", _deny)
    assets.track(101, p)
    assets.purge(101)  # must not raise
    assert any(
        "event=temp_asset_purge_failed" in r.message and "path=" in r.message
        for r in caplog.records
    )


def test_wrong_typed_chat_id_raises():
    assets = TempAssets()
    with pytest.raises(TempAssetsError, match="chat_id must be int"):
        assets.track("1", "/tmp/x")  # type: ignore
    with pytest.raises(TempAssetsError, match="chat_id must be int"):
        assets.purge("1")  # type: ignore


def test_two_chats_never_cross_purge(tmp_path):
    assets = TempAssets()
    a = tmp_path / "a.wav"
    b = tmp_path / "b.wav"
    a.write_bytes(b"a")
    b.write_bytes(b"b")
    assets.track(201, a)
    assets.track(202, b)
    assets.purge(201)
    assert not a.exists()
    assert b.exists()
