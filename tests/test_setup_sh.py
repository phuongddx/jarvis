"""Tests for setup.sh — the dependency bootstrapper.

Each test sources setup.sh with JARVIS_SETUP_SOURCED=1 (which suppresses
main()) and then calls one function, so functions are tested in isolation
without performing a real install.
"""

import hashlib
import shutil
import subprocess
import tarfile
from pathlib import Path

import pytest

SETUP_SH = Path(__file__).parent.parent / "setup.sh"

# Prefer dash over sh. macOS /bin/sh is bash in POSIX mode and still ACCEPTS
# bashisms -- verified: `[[ ]]` and `arr=(a b c)` both work under it. Testing
# with it would give false confidence. dash rejects both, matching what a
# Debian/Ubuntu user gets from `curl | sh`.
# Install with `brew install dash` if missing.
POSIX_SH = shutil.which("dash") or "sh"


def run_func(snippet: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    """Source setup.sh, then run `snippet`. Returns the completed process."""
    full_env = {"JARVIS_SETUP_SOURCED": "1", "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    if env:
        full_env.update(env)
    return subprocess.run(
        [POSIX_SH, "-c", f". {SETUP_SH}\n{snippet}"],
        capture_output=True,
        text=True,
        env=full_env,
    )


def test_dash_is_available_for_honest_bashism_detection():
    """Guard the guard: if dash is missing, these tests silently weaken.

    macOS /bin/sh accepts [[ ]] and arrays, so falling back to it means
    bashisms pass locally and break only for Linux users.
    """
    assert POSIX_SH.endswith("dash"), (
        "dash not found -- run `brew install dash`. Without it these tests "
        "run under macOS /bin/sh, which accepts bashisms and cannot catch "
        "the Linux-only breakage this suite exists to prevent."
    )


def test_setup_helper_is_documented_for_source_checkout_use():
    content = SETUP_SH.read_text()
    assert "raw.githubusercontent.com" not in content
    assert "setup.sh | sh" not in content
    assert "jarvis source checkout" in content


def test_detect_os_maps_darwin():
    result = run_func('uname() { echo Darwin; }\ndetect_os')
    assert result.returncode == 0
    assert result.stdout.strip() == "darwin"


def test_detect_os_maps_linux():
    result = run_func('uname() { echo Linux; }\ndetect_os')
    assert result.returncode == 0
    assert result.stdout.strip() == "linux"


def test_detect_os_rejects_windows_with_message():
    result = run_func('uname() { echo MINGW64_NT-10.0; }\ndetect_os')
    assert result.returncode != 0
    assert "not supported" in (result.stdout + result.stderr).lower()


def test_detect_arch_maps_apple_silicon():
    result = run_func('uname() { echo arm64; }\ndetect_arch')
    assert result.returncode == 0
    assert result.stdout.strip() == "arm64"


def test_detect_arch_maps_x86_64_to_amd64():
    result = run_func('uname() { echo x86_64; }\ndetect_arch')
    assert result.returncode == 0
    assert result.stdout.strip() == "amd64"


def test_detect_arch_maps_aarch64_to_arm64():
    """Linux reports aarch64 where macOS reports arm64."""
    result = run_func('uname() { echo aarch64; }\ndetect_arch')
    assert result.returncode == 0
    assert result.stdout.strip() == "arm64"


def test_detect_arch_rejects_unknown():
    result = run_func('uname() { echo riscv64; }\ndetect_arch')
    assert result.returncode != 0
    assert "not supported" in (result.stdout + result.stderr).lower()


def test_sourcing_does_not_run_main():
    """The guard must prevent a real install when the script is sourced."""
    result = run_func('echo sourced-ok')
    assert result.returncode == 0
    assert "sourced-ok" in result.stdout
    # main() would print a banner; it must not appear
    assert "jarvis setup" not in result.stdout.lower()


# ----------------------------------------------- install dir / PATH wiring ----


def test_bin_dir_defaults_under_home():
    result = run_func('bin_dir', env={"HOME": "/tmp/fake-home"})
    assert result.stdout.strip() == "/tmp/fake-home/.jarvis/bin"


def test_bin_dir_respects_override():
    result = run_func('bin_dir', env={"JARVIS_BIN_DIR": "/custom/bin"})
    assert result.stdout.strip() == "/custom/bin"


def test_ensure_bin_dir_creates_directory(tmp_path):
    target = tmp_path / "nested" / "bin"
    result = run_func('ensure_bin_dir', env={"JARVIS_BIN_DIR": str(target)})
    assert result.returncode == 0
    assert target.is_dir()


def test_ensure_bin_dir_is_idempotent(tmp_path):
    target = tmp_path / "bin"
    env = {"JARVIS_BIN_DIR": str(target)}
    assert run_func('ensure_bin_dir', env=env).returncode == 0
    assert run_func('ensure_bin_dir', env=env).returncode == 0
    assert target.is_dir()


def test_shell_rc_path_picks_zshrc_for_zsh():
    result = run_func('shell_rc_path', env={"SHELL": "/bin/zsh", "HOME": "/tmp/h"})
    assert result.stdout.strip() == "/tmp/h/.zshrc"


def test_shell_rc_path_picks_bashrc_for_bash():
    result = run_func('shell_rc_path', env={"SHELL": "/bin/bash", "HOME": "/tmp/h"})
    assert result.stdout.strip() == "/tmp/h/.bashrc"


def test_shell_rc_path_empty_for_unknown_shell():
    result = run_func('shell_rc_path', env={"SHELL": "/usr/bin/fish", "HOME": "/tmp/h"})
    assert result.stdout.strip() == ""


def test_ensure_on_path_appends_export_line(tmp_path):
    home = tmp_path
    rc = home / ".zshrc"
    rc.write_text("# existing content\n")
    bin_path = tmp_path / "bin"
    result = run_func(
        'ensure_on_path',
        env={"HOME": str(home), "SHELL": "/bin/zsh", "JARVIS_BIN_DIR": str(bin_path)},
    )
    assert result.returncode == 0
    content = rc.read_text()
    assert "# existing content" in content, "must not clobber existing rc content"
    assert str(bin_path) in content
    # $PATH must remain LITERAL so it expands at shell startup. If it expanded
    # at install time, the rc would freeze today's PATH forever.
    assert 'export PATH="' in content
    assert ':$PATH"' in content, "$PATH must not be expanded when written"


def test_ensure_on_path_is_idempotent(tmp_path):
    """Running twice must not duplicate the export line."""
    home = tmp_path
    rc = home / ".zshrc"
    rc.write_text("")
    bin_path = tmp_path / "bin"
    env = {"HOME": str(home), "SHELL": "/bin/zsh", "JARVIS_BIN_DIR": str(bin_path)}
    run_func('ensure_on_path', env=env)
    run_func('ensure_on_path', env=env)
    assert rc.read_text().count(str(bin_path)) == 1


def test_ensure_on_path_skips_when_already_on_path(tmp_path):
    """If the dir is already on PATH, don't touch the rc file at all."""
    home = tmp_path
    rc = home / ".zshrc"
    rc.write_text("")
    bin_path = tmp_path / "bin"
    env = {
        "HOME": str(home),
        "SHELL": "/bin/zsh",
        "JARVIS_BIN_DIR": str(bin_path),
        "PATH": f"{bin_path}:/usr/bin:/bin",
    }
    run_func('ensure_on_path', env=env)
    assert rc.read_text() == ""


# ------------------------------------------------- download / verify helper ----


def test_have_cmd_true_for_existing_binary():
    assert run_func('have_cmd sh && echo yes').stdout.strip() == "yes"


def test_have_cmd_false_for_missing_binary():
    result = run_func('have_cmd definitely-not-a-real-binary-xyz && echo yes || echo no')
    assert result.stdout.strip() == "no"


def test_sha256_of_matches_hashlib(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"jarvis")
    expected = hashlib.sha256(b"jarvis").hexdigest()
    result = run_func(f'sha256_of {f}')
    assert result.stdout.strip() == expected


def test_verify_sha256_accepts_correct_digest(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"jarvis")
    digest = hashlib.sha256(b"jarvis").hexdigest()
    result = run_func(f'verify_sha256 {f} {digest} && echo ok')
    assert result.returncode == 0
    assert "ok" in result.stdout


def test_verify_sha256_rejects_wrong_digest(tmp_path):
    f = tmp_path / "data.bin"
    f.write_bytes(b"jarvis")
    result = run_func(f'verify_sha256 {f} {"0" * 64} || echo rejected')
    assert "rejected" in result.stdout
    assert "checksum" in (result.stdout + result.stderr).lower()


def test_install_raw_binary_installs_and_marks_executable(tmp_path):
    """scip-java ships a bare launcher, not a tarball — no extraction step."""
    payload = tmp_path / "launcher"
    payload.write_text("#!/bin/sh\necho hello-from-launcher\n")
    sha_path = tmp_path / "launcher.sha256"
    digest = hashlib.sha256(payload.read_bytes()).hexdigest()
    sha_path.write_text(f"{digest}  launcher\n")

    bin_path = tmp_path / "bin"
    result = run_func(
        f'install_raw_binary file://{payload} file://{sha_path} mytool',
        env={"JARVIS_BIN_DIR": str(bin_path), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert result.returncode == 0, result.stderr
    installed = bin_path / "mytool"
    assert installed.is_file()
    assert installed.stat().st_mode & 0o111, "must be executable"
    assert "hello-from-launcher" in installed.read_text()


def test_install_raw_binary_refuses_on_checksum_mismatch(tmp_path):
    payload = tmp_path / "launcher"
    payload.write_text("#!/bin/sh\ntrue\n")
    sha_path = tmp_path / "launcher.sha256"
    sha_path.write_text(f"{'0' * 64}  launcher\n")

    bin_path = tmp_path / "bin"
    result = run_func(
        f'install_raw_binary file://{payload} file://{sha_path} mytool',
        env={"JARVIS_BIN_DIR": str(bin_path), "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"},
    )
    assert result.returncode != 0
    assert not (bin_path / "mytool").exists(), "must not install an unverified binary"


# ------------------------------------------------- shared installer helpers ----


def test_already_installed_finds_binary_in_bin_dir_not_on_path(tmp_path):
    """Idempotency must not depend on the install dir being on PATH.

    setup.sh writes to ~/.jarvis/bin and only appends it to the shell rc --
    so during the very run that creates it (and any re-run in the same shell)
    the dir is NOT yet on PATH. A PATH-only presence check re-downloads
    everything on every re-run.
    """
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    stub = bin_path / "scip"
    stub.write_text("#!/bin/sh\ntrue\n")
    stub.chmod(0o755)
    result = run_func(
        'already_installed scip && echo FOUND || echo MISSING',
        env={"JARVIS_BIN_DIR": str(bin_path), "PATH": "/usr/bin:/bin"},
    )
    assert "FOUND" in result.stdout


def test_already_installed_falls_back_to_path_lookup(tmp_path):
    """npm-installed indexers land in npm's global bin, not ours."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    stub = fake_bin / "scip-typescript"
    stub.write_text("#!/bin/sh\ntrue\n")
    stub.chmod(0o755)
    result = run_func(
        'already_installed scip-typescript && echo FOUND || echo MISSING',
        env={
            "JARVIS_BIN_DIR": str(tmp_path / "empty-bin"),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )
    assert "FOUND" in result.stdout


def test_already_installed_reports_missing_when_truly_absent(tmp_path):
    result = run_func(
        'already_installed scip && echo FOUND || echo MISSING',
        env={"JARVIS_BIN_DIR": str(tmp_path / "empty"), "PATH": "/usr/bin:/bin"},
    )
    assert "MISSING" in result.stdout


# ------------------------------------------------ scip-java installer ----


def test_install_scip_java_warns_and_returns_zero_without_java(tmp_path):
    """The launcher is inert without a JVM, so a missing `java` is a soft skip."""
    empty_bin = tmp_path / "empty"
    empty_bin.mkdir()
    result = subprocess.run(
        [POSIX_SH, "-c", f'. {SETUP_SH}\ninstall_scip_java'],
        capture_output=True,
        text=True,
        env={
            "JARVIS_SETUP_SOURCED": "1",
            "PATH": f"{empty_bin}",
            "JARVIS_BIN_DIR": str(tmp_path / "bin"),
        },
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0
    combined = (result.stdout + result.stderr).lower()
    assert "scip-java" in combined
    assert "java" in combined


def test_install_scip_java_never_mentions_docker(tmp_path):
    """Docker was never the install path — the image entrypoint is jshell."""
    assert "SCIP_JAVA_IMAGE" not in SETUP_SH.read_text()
    assert "docker pull" not in SETUP_SH.read_text()


# --------------------------------------------------------- npm-based indexers ----


def test_install_npm_indexer_warns_and_continues_without_npm(tmp_path):
    """Missing npm is a soft skip with instructions, not a hard failure."""
    empty_bin = tmp_path / "empty"
    empty_bin.mkdir()
    result = run_func(
        'install_npm_indexer scip-typescript @sourcegraph/scip-typescript',
        env={"PATH": f"{empty_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 0
    combined = (result.stdout + result.stderr).lower()
    assert "npm" in combined


def test_install_npm_indexer_skips_when_binary_present(tmp_path):
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    stub = fake_bin / "scip-typescript"
    stub.write_text("#!/bin/sh\ntrue\n")
    stub.chmod(0o755)
    result = run_func(
        'install_npm_indexer scip-typescript @sourcegraph/scip-typescript',
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 0
    combined = (result.stdout + result.stderr).lower()
    assert "already" in combined or "skip" in combined


def test_install_npm_indexer_invokes_npm_with_correct_package(tmp_path):
    """Stub npm and assert the exact package name passed to it."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    log = tmp_path / "npm-args.txt"
    npm_stub = fake_bin / "npm"
    npm_stub.write_text(f'#!/bin/sh\necho "$@" > {log}\n')
    npm_stub.chmod(0o755)
    result = run_func(
        'install_npm_indexer scip-python @sourcegraph/scip-python',
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, result.stderr
    assert "@sourcegraph/scip-python" in log.read_text()
    assert "-g" in log.read_text()


def test_scip_typescript_wrapper_uses_sourcegraph_package(tmp_path):
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    log = tmp_path / "args.txt"
    npm_stub = fake_bin / "npm"
    npm_stub.write_text(f'#!/bin/sh\necho "$@" > {log}\n')
    npm_stub.chmod(0o755)
    result = run_func(
        'install_scip_typescript',
        env={"PATH": f"{fake_bin}:/usr/bin:/bin"},
    )
    assert result.returncode == 0
    assert "@sourcegraph/scip-typescript" in log.read_text()


# ------------------------------------------------------ scip-swift installer ----


def _stage_scip_swift_release(
    tmp_path: Path, tag: str = "v0.3.0", digest: str | None = None,
    decoy_assets: list[str] | None = None,
) -> str:
    """Stage a local tarball (member `scip-swift`) plus a GitHub-shaped
    releases/latest JSON pointing at it; return the file:// API URL.

    Mirrors api.github.com's pretty-printed shape (tag_name, assets[] with
    name/digest/browser_download_url) so the sed/grep extraction in
    setup.sh exercises its real parsing path. `digest` overrides the real
    hash so a test can serve a tampered checksum. `decoy_assets` stages
    extra tarballs -- each with its own CORRECT digest and a stub printing
    a decoy marker -- listed BEFORE the real asset: the wrong-platform-
    asset-listed-first shape WR-01's name-anchored selection guards
    against (first-match extraction would pick the decoy and "verify" it).
    """
    stage = tmp_path / "stage"
    stage.mkdir()
    (stage / "scip-swift").write_text("#!/bin/sh\necho '0.3.0 (swift 6.2.4)'\n")
    tar_path = tmp_path / "scip-swift.tar.gz"
    with tarfile.open(tar_path, "w:gz") as tf:
        tf.add(stage / "scip-swift", arcname="scip-swift")
    real = hashlib.sha256(tar_path.read_bytes()).hexdigest()

    assets_json = ""
    for i, name in enumerate(decoy_assets or []):
        decoy_stage = tmp_path / f"decoy-stage-{i}"
        decoy_stage.mkdir()
        (decoy_stage / "scip-swift").write_text(
            "#!/bin/sh\necho '0.3.0-decoy-do-not-install'\n"
        )
        decoy_tar = tmp_path / f"decoy-{i}.tar.gz"
        with tarfile.open(decoy_tar, "w:gz") as tf:
            tf.add(decoy_stage / "scip-swift", arcname="scip-swift")
        decoy_digest = hashlib.sha256(decoy_tar.read_bytes()).hexdigest()
        assets_json += (
            "    {\n"
            f'      "name": "{name}",\n'
            f'      "digest": "sha256:{decoy_digest}",\n'
            f'      "browser_download_url": "file://{decoy_tar}"\n'
            "    },\n"
        )

    api_json = tmp_path / "latest.json"
    api_json.write_text(
        "{\n"
        f'  "tag_name": "{tag}",\n'
        '  "assets": [\n'
        f"{assets_json}"
        "    {\n"
        f'      "name": "scip-swift-{tag.lstrip("v")}.tar.gz",\n'
        f'      "digest": "sha256:{real if digest is None else digest}",\n'
        f'      "browser_download_url": "file://{tar_path}"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    return f"file://{api_json}"


def test_install_scip_swift_skips_on_linux_without_failing(tmp_path):
    """Swift indexing needs Xcode; a Linux skip is by design, not an error."""
    result = run_func(
        'install_scip_swift linux amd64',
        env={"JARVIS_BIN_DIR": str(tmp_path / "bin"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0, "skip-by-design must not be a failure"
    combined = (result.stdout + result.stderr).lower()
    assert "not available" in combined or "macos" in combined


def test_install_scip_swift_skips_on_intel_mac(tmp_path):
    """Only an arm64 asset is published upstream."""
    result = run_func(
        'install_scip_swift darwin amd64',
        env={"JARVIS_BIN_DIR": str(tmp_path / "bin"), "PATH": "/usr/bin:/bin"},
    )
    assert result.returncode == 0
    assert "not available" in (result.stdout + result.stderr).lower()


def test_scip_swift_repo_is_the_org_not_the_personal_owner():
    """The repo moved owners and GitHub serves no redirect for the old path.

    While this pointed at `phuongddx/scip-swift` the download URL 404'd, so
    `--only scip-swift` failed on every macOS arm64 host and Swift indexing was
    unavailable to all users. Nothing else caught it: no other test reads this
    variable, and setup-smoke.yml did not exercise the scip-swift install.
    """
    repo = run_func('echo "$SCIP_SWIFT_REPO"').stdout.strip()
    assert repo == "jarvis-intelligence/scip-swift", f"wrong owner: {repo}"


def test_version_ge_compares_numeric_fields_not_strings():
    """0.10.0 > 0.3.0 must be GE: numeric field compare, not lexicographic."""
    assert run_func("version_ge 0.10.0 0.3.0").returncode == 0
    assert run_func("version_ge v0.3.0 0.2.1").returncode == 0, "v prefix is tolerated"
    assert run_func("version_ge 0.2.2 0.3.0").returncode != 0
    assert run_func("version_ge 0.3.0 0.3.0").returncode == 0, "equal counts as GE"


def test_install_scip_swift_resolves_and_installs_latest(tmp_path):
    """Tag, asset URL, and digest all come from the API JSON (seam-served)."""
    api_url = _stage_scip_swift_release(tmp_path, tag="v0.3.0")
    bin_path = tmp_path / "bin"
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(bin_path),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode == 0, result.stderr
    installed = bin_path / "scip-swift"
    assert installed.is_file()
    assert installed.stat().st_mode & 0o111
    # The seam tarball's stub must be what landed, not some other source.
    probe = subprocess.run([str(installed), "--version"], capture_output=True, text=True)
    assert "0.3.0" in probe.stdout


def test_install_scip_swift_fails_loudly_when_latest_below_floor(tmp_path):
    """v0.2.x ignores --build-tool xcodebuild; below-floor must be fatal."""
    api_url = _stage_scip_swift_release(tmp_path, tag="v0.2.1")
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(tmp_path / "bin"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode != 0
    combined = result.stdout + result.stderr
    assert "0.3.0" in combined, "must name the floor"
    assert "no good release exists yet" in combined
    assert not (tmp_path / "bin" / "scip-swift").exists()


def test_install_scip_swift_fails_on_digest_mismatch(tmp_path):
    """A tampered digest must fail the install before anything is installed."""
    api_url = _stage_scip_swift_release(tmp_path, digest="0" * 64)
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(tmp_path / "bin"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode != 0
    assert "checksum mismatch" in (result.stdout + result.stderr).lower()
    assert not (tmp_path / "bin" / "scip-swift").exists()


def test_install_scip_swift_rejects_tag_with_shell_metacharacters(tmp_path):
    """Untrusted API JSON: a tag carrying a payload never reaches shell use."""
    payload = tmp_path / "pwned"
    api_url = _stage_scip_swift_release(tmp_path, tag=f"v0.3.0; touch {payload}")
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(tmp_path / "bin"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode != 0
    assert not payload.exists(), "shape validation must fire before any use"
    assert not (tmp_path / "bin" / "scip-swift").exists()


def test_install_scip_swift_picks_macos_asset_over_linux_asset_listed_first(tmp_path):
    """WR-01: extraction must anchor on the macOS asset's NAME, not on
    whichever asset is listed first. A linux tarball listed first carries
    its own consistent (url, digest) pair, so first-match extraction would
    download it, digest-verify it, and install the wrong-platform binary --
    the failure surfacing only later as an exec-format error."""
    api_url = _stage_scip_swift_release(
        tmp_path, tag="v0.3.0", decoy_assets=["scip-swift-0.3.0-linux-amd64.tar.gz"]
    )
    bin_path = tmp_path / "bin"
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(bin_path),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode == 0, result.stderr
    installed = bin_path / "scip-swift"
    assert installed.is_file()
    probe = subprocess.run([str(installed), "--version"], capture_output=True, text=True)
    assert "0.3.0" in probe.stdout
    assert "decoy" not in probe.stdout.lower(), "the linux asset must never be installed"



def test_install_scip_swift_fails_loudly_when_no_macos_asset_exists(tmp_path):
    """WR-01: a release shipping only non-macos assets must be refused at
    metadata time, not downloaded and installed as a wrong-platform binary."""
    api_json = tmp_path / "linux-only.json"
    api_json.write_text(
        "{\n"
        '  "tag_name": "v0.3.0",\n'
        '  "assets": [\n'
        "    {\n"
        '      "name": "scip-swift-0.3.0-linux-amd64.tar.gz",\n'
        f'      "digest": "sha256:{"0" * 64}",\n'
        '      "browser_download_url": "file:///nonexistent/scip-swift-0.3.0-linux-amd64.tar.gz"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(tmp_path / "bin"),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "SCIP_SWIFT_API_URL": f"file://{api_json}",
        },
    )
    assert result.returncode != 0
    assert "macos" in (result.stdout + result.stderr).lower()
    assert not (tmp_path / "bin" / "scip-swift").exists()



def test_install_scip_swift_pairs_digest_and_url_from_the_same_asset(tmp_path):
    """WR-01's cross-asset half: a non-.tar.gz asset listed first would
    contribute its digest while the URL came from the .tar.gz asset -- a
    guaranteed checksum mismatch and hard install failure. Name-anchored
    selection never mixes fields across assets."""
    api_url = _stage_scip_swift_release(
        tmp_path, tag="v0.3.0", decoy_assets=["scip-swift-0.3.0-darwin-amd64.zip"]
    )
    bin_path = tmp_path / "bin"
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(bin_path),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode == 0, result.stderr
    installed = bin_path / "scip-swift"
    assert installed.is_file()
    probe = subprocess.run([str(installed), "--version"], capture_output=True, text=True)
    assert "0.3.0" in probe.stdout
    assert "decoy" not in probe.stdout.lower()


def test_install_scip_swift_skips_when_installed_version_is_current(tmp_path):
    """Version-aware skip: an installed binary already at latest is skipped."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    stub = fake_bin / "scip-swift"
    stub.write_text("#!/bin/sh\necho '0.3.0 (swift 6.2.4)'\n")
    stub.chmod(0o755)
    api_url = _stage_scip_swift_release(tmp_path, tag="v0.3.0")
    bin_path = tmp_path / "bin"
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(bin_path),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode == 0, result.stderr
    combined = (result.stdout + result.stderr).lower()
    assert "already" in combined or "skip" in combined
    assert not (bin_path / "scip-swift").exists()


def test_install_scip_swift_reinstalls_when_installed_version_outdated(tmp_path):
    """Auto-roll must upgrade a stale v0.1.2 — presence-skip would strand it."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    stub = fake_bin / "scip-swift"
    stub.write_text("#!/bin/sh\necho '0.1.2 (swift 6.1.2)'\n")
    stub.chmod(0o755)
    api_url = _stage_scip_swift_release(tmp_path, tag="v0.3.0")
    bin_path = tmp_path / "bin"
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(bin_path),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode == 0, result.stderr
    assert (bin_path / "scip-swift").is_file()


def test_install_scip_swift_reinstalls_when_installed_version_unparseable(tmp_path):
    """WR-02: version_ge treats a comparison error as equality, so a
    garbage first --version token (a "name version" format, a wrapper that
    prints a warning line first) must NOT count as current -- that skip is
    what strands the upgrade: setup.sh skips, the runtime floor says
    "re-run setup.sh", which skips again."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    stub = fake_bin / "scip-swift"
    stub.write_text("#!/bin/sh\necho 'scip-swift version 0.3.0 (swift 6.2.4)'\n")
    stub.chmod(0o755)
    api_url = _stage_scip_swift_release(tmp_path, tag="v0.3.0")
    bin_path = tmp_path / "bin"
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(bin_path),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode == 0, result.stderr
    assert (bin_path / "scip-swift").is_file(), "unparseable must mean reinstall, not skip"


def test_install_scip_swift_reinstalls_when_installed_version_carries_suffix(tmp_path):
    """WR-02, the equal-tri-version case: "0.3.0-rc1" vs latest v0.3.0
    short-circuits every field compare as false and falls through to
    "greater or equal" -- keeping the rc build on skip. The stray-character
    check (mirroring _tag validation) must treat it as unparseable."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    stub = fake_bin / "scip-swift"
    stub.write_text("#!/bin/sh\necho '0.3.0-rc1 (swift 6.2.4)'\n")
    stub.chmod(0o755)
    api_url = _stage_scip_swift_release(tmp_path, tag="v0.3.0")
    bin_path = tmp_path / "bin"
    result = run_func(
        "install_scip_swift darwin arm64",
        env={
            "JARVIS_BIN_DIR": str(bin_path),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "SCIP_SWIFT_API_URL": api_url,
        },
    )
    assert result.returncode == 0, result.stderr
    assert (bin_path / "scip-swift").is_file(), "a suffixed version must not satisfy the skip gate"


def test_install_scip_swift_linux_skips_before_any_fetch(tmp_path):
    """Platform gate first: Linux exits 0 without touching the API at all."""
    result = run_func(
        "install_scip_swift linux amd64",
        env={
            "JARVIS_BIN_DIR": str(tmp_path / "bin"),
            "PATH": "/usr/bin:/bin",
            "SCIP_SWIFT_API_URL": "file:///nonexistent-latest.json",
        },
    )
    assert result.returncode == 0
    combined = (result.stdout + result.stderr).lower()
    assert "not available" in combined


# ------------------------------------------------ orchestration / flags / summary ----


def test_help_flag_exits_zero_and_prints_usage():
    result = subprocess.run(
        [POSIX_SH, str(SETUP_SH), "--help"],
        capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin", "HOME": "/tmp"},
    )
    assert result.returncode == 0
    assert "usage" in result.stdout.lower()
    assert "--only" in result.stdout


def test_parse_args_sets_only():
    result = run_func('parse_args --only scip-typescript; echo "ONLY=$ONLY"')
    assert "ONLY=scip-typescript" in result.stdout


def test_removed_common_installers_are_rejected():
    for removed in ("scip", "zoekt", "ctags", "jarvis-mcp"):
        result = run_func(f"parse_args --only {removed}")
        assert result.returncode != 0
        assert "--only must be one of:" in result.stdout + result.stderr


def test_usage_lists_only_language_installers():
    text = SETUP_SH.read_text()
    start = text.index("Options:")
    end = text.index("Environment:", start)
    usage = text[start:end]
    for retained in (
        "scip-swift", "scip-typescript", "scip-python", "scip-java", "bash-shim"
    ):
        assert retained in usage
    for removed in ("scip,", "zoekt,", "ctags,", "jarvis-mcp"):
        assert removed not in usage


def test_common_installer_functions_are_removed():
    text = SETUP_SH.read_text()
    for function in (
        "install_scip()", "install_zoekt()", "install_ctags()",
        "install_jarvis_mcp()", "link_universal_ctags()",
        "install_tarball_binary()", "confirm()",
    ):
        assert function not in text


def test_parse_args_sets_force():
    result = run_func('parse_args --force; echo "FORCE=$FORCE"')
    assert "FORCE=1" in result.stdout


def test_parse_args_rejects_unknown_flag():
    result = run_func('parse_args --bogus || echo rejected')
    assert "rejected" in result.stdout


def test_record_and_print_summary_roundtrip():
    result = run_func(
        'SUMMARY=""; record scip installed; record zoekt skipped; print_summary'
    )
    assert "scip" in result.stdout
    assert "installed" in result.stdout
    assert "zoekt" in result.stdout
    assert "skipped" in result.stdout


def test_only_flag_runs_single_installer(tmp_path):
    """--only scip-python must not attempt the other npm installer."""
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    npm_log = tmp_path / "npm-called.txt"
    npm_stub = fake_bin / "npm"
    npm_stub.write_text(f'#!/bin/sh\necho called >> {npm_log}\n')
    npm_stub.chmod(0o755)
    # Pre-place scip-python so the selected installer skips without network.
    install_bin = tmp_path / "bin"
    install_bin.mkdir()
    scip_python = install_bin / "scip-python"
    scip_python.write_text("#!/bin/sh\ntrue\n")
    scip_python.chmod(0o755)

    result = subprocess.run(
        [POSIX_SH, str(SETUP_SH), "--only", "scip-python"],
        capture_output=True, text=True,
        env={
            "PATH": f"{fake_bin}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "JARVIS_BIN_DIR": str(install_bin),
            "SHELL": "/bin/zsh",
        },
        stdin=subprocess.DEVNULL,
    )
    assert result.returncode == 0, result.stderr
    assert not npm_log.exists(), "--only scip-python must not run npm installers"


def _fake_bash(tmp_path, name: str, version_line: str) -> str:
    """A stand-in bash that answers --version and nothing else."""
    path = tmp_path / name
    path.write_text(f'#!/bin/sh\n[ "$1" = "--version" ] && echo "{version_line}"\n')
    path.chmod(0o755)
    return str(path)


def test_shim_dir_defaults_under_jarvis_home():
    result = run_func("shim_dir", env={"HOME": "/home/someone"})
    assert result.stdout.strip() == "/home/someone/.jarvis/shims"


def test_shim_dir_follows_data_dir_not_bin_dir(tmp_path):
    """Must key off JARVIS_DATA_DIR: config.shim_dir() reads that one, and a
    shim the Python side cannot find is worse than no shim."""
    result = run_func(
        "shim_dir",
        env={"HOME": "/home/someone",
             "JARVIS_DATA_DIR": str(tmp_path),
             "JARVIS_BIN_DIR": "/should/be/ignored"},
    )
    assert result.stdout.strip() == f"{tmp_path}/shims"


def test_bash_at_least_44_accepts_bash_5(tmp_path):
    fake = _fake_bash(tmp_path, "bash5", "GNU bash, version 5.3.15(1)-release (arm64-apple-darwin25)")
    assert run_func(f'bash_at_least_44 "{fake}"').returncode == 0


def test_bash_at_least_44_accepts_exactly_44(tmp_path):
    fake = _fake_bash(tmp_path, "bash44", "GNU bash, version 4.4.0(1)-release")
    assert run_func(f'bash_at_least_44 "{fake}"').returncode == 0


def test_bash_at_least_44_rejects_macos_bash_32(tmp_path):
    fake = _fake_bash(tmp_path, "bash32", "GNU bash, version 3.2.57(1)-release (arm64-apple-darwin25)")
    assert run_func(f'bash_at_least_44 "{fake}"').returncode != 0


def test_bash_at_least_44_rejects_unparseable_version(tmp_path):
    fake = _fake_bash(tmp_path, "weird", "not a version string at all")
    assert run_func(f'bash_at_least_44 "{fake}"').returncode != 0


def test_bash_at_least_44_rejects_missing_binary(tmp_path):
    assert run_func(f'bash_at_least_44 "{tmp_path}/nope"').returncode != 0


def test_install_bash_shim_is_a_noop_on_linux(tmp_path):
    result = run_func(
        'install_bash_shim linux',
        env={"HOME": str(tmp_path), "JARVIS_DATA_DIR": str(tmp_path)},
    )
    assert result.returncode == 0
    assert not (tmp_path / "shims" / "bash").exists()


def test_install_bash_shim_links_default_bash_when_it_is_already_modern(tmp_path):
    """A modern default bash must still be linked into the shim dir -- setup.sh's
    own PATH at install time is not necessarily the PATH the indexer subprocess
    will inherit later (a GUI-launched MCP server, launchd, a stripped-env shell),
    so "already modern here, skip" leaves those contexts with no shim and the
    original bug. Baking the resolved default into the shim dir removes the
    PATH-context dependency entirely."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    modern = _fake_bash(bindir, "bash", "GNU bash, version 5.3.15(1)-release")
    result = run_func(
        'install_bash_shim darwin',
        env={"HOME": str(tmp_path), "JARVIS_DATA_DIR": str(tmp_path),
             "PATH": f"{bindir}:/usr/bin:/bin"},
    )
    assert result.returncode == 0
    link = tmp_path / "shims" / "bash"
    assert link.is_symlink()
    assert link.resolve() == Path(modern).resolve()


def test_install_bash_shim_links_candidate_when_default_is_old(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bash(bindir, "bash", "GNU bash, version 3.2.57(1)-release")
    modern = _fake_bash(tmp_path, "modern-bash", "GNU bash, version 5.3.15(1)-release")
    result = run_func(
        f'BASH_SHIM_CANDIDATES="{modern}"\ninstall_bash_shim darwin',
        env={"HOME": str(tmp_path), "JARVIS_DATA_DIR": str(tmp_path),
             "PATH": f"{bindir}:/usr/bin:/bin"},
    )
    assert result.returncode == 0
    link = tmp_path / "shims" / "bash"
    assert link.is_symlink()
    assert link.resolve() == Path(modern).resolve()


def test_install_bash_shim_advises_install_when_no_modern_bash(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _fake_bash(bindir, "bash", "GNU bash, version 3.2.57(1)-release")
    result = run_func(
        f'BASH_SHIM_CANDIDATES="{tmp_path}/absent"\ninstall_bash_shim darwin',
        env={"HOME": str(tmp_path), "JARVIS_DATA_DIR": str(tmp_path),
             "PATH": f"{bindir}:/usr/bin:/bin"},
    )
    assert result.returncode == 0, "must not fail setup for users who never index Java"
    assert "brew install bash" in result.stdout + result.stderr
    assert not (tmp_path / "shims" / "bash").exists()
