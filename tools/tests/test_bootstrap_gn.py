"""GN host recovery uses tiny executables, never a Chromium compilation."""
from pathlib import Path
import subprocess
import sys
from unittest import mock

import pytest

from tools import bootstrap_gn


GOOD_GN = '#!/bin/sh\n[ "$1" = --version ]\n'


def put(path, contents, *, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(contents, bytes):
        path.write_bytes(contents)
    else:
        path.write_text(contents)
    if executable:
        path.chmod(0o755)
    return path


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / "source with spaces"
    out = src / "out/Default"
    out.mkdir(parents=True)
    for name in ("retained.o", "build.ninja", ".ninja_log", ".ninja_deps"):
        put(out / name, name)
    put(src / "out/Release/gn_build/gn", b"foreign GN", executable=True)
    put(src / "out/Release/gn_build/retained.o", b"foreign GN object")
    put(src / "out/Release/gn_build/.ninja_log", b"donor log")
    return src, out


def snapshot(src):
    return {str(path.relative_to(src)): (path.read_bytes(), path.stat().st_mtime_ns)
            for path in src.rglob("*") if path.is_file() and path.name != "gn"}


def bootstrap(src, candidate=GOOD_GN, *, exit_code=0, executable=True, linked=False):
    put(src / "tools/gn/bootstrap/bootstrap.py", f'''import os, sys
from pathlib import Path
src = Path.cwd()
args = sys.argv[1:]
assert args[-1] == '--skip-generate-buildfiles'
build = src / args[args.index('--build-path') + 1]
assert build.parent == src / 'out' and build.name.startswith('.chromix-gn-')
assert not list(build.iterdir()), 'must not reuse donor GN intermediates'
output = Path(args[args.index('-o') + 1])
assert output == build / 'gn'
assert os.environ['NINJA'] == 'selected-ninja'
assert os.environ['CXXFLAGS'] == '-Wno-deprecated-declarations'
assert (src / 'out/Release/gn_build/gn').read_bytes() == b'foreign GN'
if {linked!r}:
    output.symlink_to(src / 'out/Release/gn_build/gn')
else:
    output.write_text({candidate!r})
    output.chmod(0o755 if {executable!r} else 0o644)
raise SystemExit({exit_code})
''')


@pytest.mark.parametrize("output", ["Default", "Chromix"])
def test_runnable_gn_is_preserved_without_bootstrap(tree, output):
    src, _ = tree
    out = src / "out" / output
    out.mkdir(exist_ok=True)
    gn = put(out / "gn", GOOD_GN, executable=True)
    before = (gn.read_bytes(), gn.stat().st_mtime_ns, gn.stat().st_ino)
    bootstrap_gn.prepare(src, out)
    assert (gn.read_bytes(), gn.stat().st_mtime_ns, gn.stat().st_ino) == before
    assert not list((src / "out").glob(".chromix-gn-*"))


@pytest.mark.parametrize("old", [None, b"\xcf\xfa\xed\xfe\x0c\x00\x00\x01foreign", '#!/bin/sh\nexit 126\n'])
def test_missing_or_unrunnable_gn_uses_fresh_build_and_preserves_chromium(tree, monkeypatch, old):
    src, out = tree
    monkeypatch.setenv("NINJA", "selected-ninja")
    monkeypatch.setenv("CXXFLAGS", "-Wno-deprecated-declarations")
    if old is not None:
        put(out / "gn", old, executable=True)
    bootstrap(src)
    before = snapshot(src)
    bootstrap_gn.prepare(src, out)
    assert (out / "gn").read_text() == GOOD_GN
    assert snapshot(src) == before
    assert (src / "out/Release/gn_build/gn").read_bytes() == b"foreign GN"
    assert not list((src / "out").glob(".chromix-gn-*"))
    # A resumed stage must use the new GN without rebuilding it.
    inode = (out / "gn").stat().st_ino
    bootstrap_gn.prepare(src, out)
    assert (out / "gn").stat().st_ino == inode


@pytest.mark.parametrize("options", [
    {"exit_code": 17}, {"candidate": "#!/bin/sh\nexit 126\n"},
    {"candidate": "not an executable"}, {"executable": False}, {"linked": True},
    {"candidate": '#!/bin/sh\ncase "$0" in */Default/gn) exit 1;; *) exit 0;; esac\n'},
])
def test_bootstrap_failure_never_replaces_old_gn_and_cleans_only_temporary_files(tree, monkeypatch, options):
    src, out = tree
    monkeypatch.setenv("NINJA", "selected-ninja")
    monkeypatch.setenv("CXXFLAGS", "-Wno-deprecated-declarations")
    old = put(out / "gn", "#!/bin/sh\nexit 1\n", executable=True)
    before_gn = old.read_bytes(), old.stat().st_mtime_ns
    bootstrap(src, **options)
    before = snapshot(src)
    with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
        bootstrap_gn.prepare(src, out)
    assert snapshot(src) == before
    assert (old.read_bytes(), old.stat().st_mtime_ns) == before_gn
    assert not list((src / "out").glob(".chromix-gn-*"))


@pytest.mark.parametrize("name", ["gn", "out", "source"])
def test_linked_paths_fail_without_touching_target(tree, tmp_path, name):
    src, out = tree
    target = put(tmp_path / "external", "untouched")
    if name == "gn":
        (out / "gn").symlink_to(target)
    elif name == "out":
        out.rename(src / "out/real")
        out.symlink_to(src / "out/real", target_is_directory=True)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(src, target_is_directory=True)
        src, out = alias, alias / "out/Default"
    with pytest.raises(ValueError):
        bootstrap_gn.prepare(src, out)
    assert target.read_text() == "untouched"


def test_output_outside_supported_build_directory_is_rejected(tree):
    src, out = tree
    for target in (src / "out/Release", src / "tools", out / "../Default", src.parent / "other"):
        with pytest.raises(ValueError):
            bootstrap_gn.prepare(src, target)


def test_probe_deadline_and_execution_errors(tree):
    src, out = tree
    gn = put(out / "gn", GOOD_GN, executable=True)
    for failure in (OSError("Bad CPU type in executable"), subprocess.TimeoutExpired("gn", 20)):
        with mock.patch.object(bootstrap_gn.subprocess, "run", side_effect=failure) as run:
            assert not bootstrap_gn.runnable(gn)
            assert run.call_args.kwargs["timeout"] == 20
            assert run.call_args.args[0] == [str(gn), "--version"]


def test_rollback_failure_preserves_old_gn_backup(tree, monkeypatch):
    src, out = tree
    monkeypatch.setenv("NINJA", "selected-ninja")
    monkeypatch.setenv("CXXFLAGS", "-Wno-deprecated-declarations")
    original = put(out / "gn", "#!/bin/sh\nexit 1\n", executable=True)
    old_bytes, old_inode = original.read_bytes(), original.stat().st_ino
    bootstrap(src, '#!/bin/sh\ncase "$0" in */Default/gn) exit 1;; *) exit 0;; esac\n')
    replace = bootstrap_gn.os.replace

    def failing_rollback(source, destination):
        if Path(source).name == "previous-gn":
            raise OSError("injected rollback I/O error")
        return replace(source, destination)

    with mock.patch.object(bootstrap_gn.os, "replace", side_effect=failing_rollback):
        with pytest.raises(RuntimeError, match="recovery files preserved") as error:
            bootstrap_gn.prepare(src, out)
    directories = list((src / "out").glob(".chromix-gn-*"))
    assert len(directories) == 1
    backup = directories[0] / "previous-gn"
    assert backup.read_bytes() == old_bytes and backup.stat().st_ino == old_inode
    assert str(directories[0]) in str(error.value)
    assert "installed GN cannot execute" in str(error.value)
    assert "injected rollback I/O error" in str(error.value)


def test_cli_reports_bootstrap_error(tree):
    src, out = tree
    result = subprocess.run([sys.executable, bootstrap_gn.__file__, "--src", str(src), "--out", str(out)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 1
    assert "GN preparation failed" in result.stderr
    assert not list((src / "out").glob(".chromix-gn-*"))
