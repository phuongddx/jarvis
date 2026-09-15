"""Tests for the jarvis Homebrew formula generator."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "generate_homebrew_formula.py"
spec = importlib.util.spec_from_file_location("generate_homebrew_formula", SCRIPT)
generator = importlib.util.module_from_spec(spec)
spec.loader.exec_module(generator)


def _checksums() -> dict[str, str]:
    return {
        platform: f"{index + 10:064x}"
        for index, platform in enumerate(generator.PLATFORMS)
    }


def test_render_contains_every_platform_url_and_checksum():
    checksums = _checksums()
    formula = generator.render("0.11.0", "v0.11.0", checksums)

    assert 'version "' not in formula
    assert 'depends_on "universal-ctags"' in formula
    for platform, digest in checksums.items():
        url = (
            "https://github.com/jarvis-intelligence/homebrew-jarvis"
            f"/releases/download/v0.11.0/jarvis_0.11.0_{platform}.tar.gz"
        )
        assert f'url "{url}"\n      sha256 "{digest}"' in formula


def test_render_installs_both_names_and_ctags_shim():
    formula = generator.render("0.11.0", "v0.11.0", _checksums())

    assert 'libexec.install Dir["libexec/*"]' in formula
    assert 'Dir["jarvis/libexec/*"]' not in formula
    assert 'bin.install_symlink libexec/"jarvis" => "jarvis"' in formula
    assert 'bin.install_symlink libexec/"jarvis" => "jarvis-server"' in formula
    assert 'formula_opt_bin("universal-ctags")/"ctags"' in formula
    assert 'Formula["universal-ctags"]' not in formula
    assert 'exec "#{ctags}" "$@"' in formula
    assert "chmod 0755, libexec/" in formula
    assert "0o755" not in formula
    assert 'shell_output("#{bin}/jarvis --version")' in formula
    assert "{{ctags}}" not in formula
    assert "{{bin}}" not in formula


def test_render_uses_the_public_tap_release_urls():
    formula = generator.render("0.11.0", "v0.11.0", _checksums())

    for platform in generator.PLATFORMS:
        url = (
            "https://github.com/jarvis-intelligence/homebrew-jarvis"
            f"/releases/download/v0.11.0/jarvis_0.11.0_{platform}.tar.gz"
        )
        assert url in formula


@pytest.mark.parametrize(
    ("version", "release_tag"),
    [
        ("latest", "vlatest"),
        ("0.11", "v0.11"),
        ("0.11.0-beta.1", "v0.11.0-beta.1"),
    ],
)
def test_render_rejects_non_semantic_version_or_empty_tag(version, release_tag):
    with pytest.raises(ValueError, match=version):
        generator.render(version, release_tag, _checksums())


def test_render_rejects_missing_unexpected_and_malformed_checksums():
    missing = _checksums()
    missing.pop("linux_arm64")
    with pytest.raises(ValueError, match="linux_arm64"):
        generator.render("0.11.0", "v0.11.0", missing)

    unexpected = _checksums() | {"windows_amd64": "0" * 64}
    with pytest.raises(ValueError, match="windows_amd64"):
        generator.render("0.11.0", "v0.11.0", unexpected)

    malformed = _checksums()
    malformed["darwin_amd64"] = malformed["darwin_amd64"].upper()
    with pytest.raises(ValueError, match="darwin_amd64"):
        generator.render("0.11.0", "v0.11.0", malformed)

    malformed["darwin_amd64"] = "0" * 63
    with pytest.raises(ValueError, match="darwin_amd64"):
        generator.render("0.11.0", "v0.11.0", malformed)


def test_cli_reads_sidecars_and_writes_formula_to_stdout(tmp_path, capsys):
    for platform, digest in _checksums().items():
        name = f"jarvis_0.11.0_{platform}.tar.gz"
        (tmp_path / f"{name}.sha256").write_text(f"{digest}  {name}\n")

    result = generator.main(
        [
            "--version", "0.11.0",
            "--release-tag", "v0.11.0",
            "--checksum-dir", str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 0
    assert captured.out == generator.render("0.11.0", "v0.11.0", _checksums())
    assert captured.err == ""


def test_cli_reports_missing_or_invalid_sidecar_on_stderr(tmp_path, capsys):
    for platform, digest in _checksums().items():
        if platform == "linux_amd64":
            continue
        name = f"jarvis_0.11.0_{platform}.tar.gz"
        (tmp_path / f"{name}.sha256").write_text(f"{digest}  {name}\n")

    result = generator.main(
        [
            "--version", "0.11.0",
            "--release-tag", "v0.11.0",
            "--checksum-dir", str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "jarvis_0.11.0_linux_amd64.tar.gz.sha256" in captured.err


def test_render_rejects_empty_release_tag():
    with pytest.raises(ValueError, match="release tag must not be empty"):
        generator.render("0.11.0", "", _checksums())


def test_cli_rejects_bad_version_before_reading_checksums(tmp_path, capsys):
    result = generator.main(
        [
            "--version", "latest",
            "--release-tag", "vlatest",
            "--checksum-dir", str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.out == ""
    assert "version must be semantic X.Y.Z" in captured.err
