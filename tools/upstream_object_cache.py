#!/usr/bin/env python3
"""Conservative Linux reuse of upstream Chromium compilation outputs.

Call prepare(src, donor, platform, arch, work) after canonical GN generation.
It returns status, counts and reasons, and never builds or changes either tree.
Use GN cc_wrapper="python3 /absolute/repo/tools/upstream_object_cache.py compile --".
The wrapper must run from WORK/src/out/Chromix; unsupported actions pass through.

WORK/.upstream-objects owns copied objects, manifests and per-output receipts.
Keep the COMPLETE donor source tree (including out/Default/generated inputs)
unchanged during the first build stage. Prefer WORK/upstream-cache. The parent
may remove it before handoff; later attempts then miss. Relative donor locations
survive WORK relocation. Recorded headers cannot reproduce negative lookups.
Ninja metadata must retain nanoseconds. Only a pinned source receipt may opt in
to prepare(..., allow_truncated_mtimes=True) for GNU tar's whole-second files.
That mode requires every donor input/toolchain mtime strictly before the log's
whole-second cutoff; same-second inputs miss. The default requires exact object
mtimes. This trusts a completed, unmodified donor, not forged metadata.
Full LLVM/sysroot verification still runs per candidate at compile time. This
conservative scan cost is accepted for the initial eligible C subset.
Only the canonical compiler is executed, including for donor preprocessing.
Ninja v5/v6 logs use the 64-bit 0xDECAFBADDECAFBAD command-hash seed;
0xDECAFBAD is Ninja's unrelated 32-bit path-hash seed. Ninja 1.13's v7 logs
use rapidhash: metadata restoration supports v7, but optional object reuse still
requires v5/v6 command hashes. Dependency logs require v4.
Modules, PCH, profiles, response files, external inputs and unknown flags miss;
canonical flags are never rewritten to increase eligibility.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path

CLANG = Path("third_party/llvm-build/Release+Asserts")
CACHE = ".upstream-objects"
SCHEMA = 2
NANOSECOND = 1_000_000_000
INERT_C_MODULE_OPTIONS = {
    "-fmodule-file-home-is-cwd", "-fmodules-cache-path=/not_exist_dummy_dir",
}
MODULE_NAME = re.compile(r"-fmodule-name=//[A-Za-z0-9_./+-]+:[A-Za-z0-9_.+-]+")
PREPROCESS_TIMEOUT = 120
NINJA_TIMEOUT = 120
ENVIRONMENT_INPUTS = (
    "CPATH", "C_INCLUDE_PATH", "CPLUS_INCLUDE_PATH", "OBJC_INCLUDE_PATH",
    "COMPILER_PATH", "GCC_EXEC_PREFIX", "LIBRARY_PATH", "SDKROOT",
    "SOURCE_DATE_EPOCH", "DEPENDENCIES_OUTPUT", "SUNPRO_DEPENDENCIES",
    "CCC_OVERRIDE_OPTIONS", "CCC_ADD_ARGS", "CLANG_CONFIG_FILE_SYSTEM_DIR",
    "CLANG_CONFIG_FILE_USER_DIR", "CLANG_CONFIG_FILE", "CLANG_MODULE_CACHE_PATH",
    "LD_PRELOAD", "LD_LIBRARY_PATH",
)
# Unknown driver flags are misses: many seemingly harmless flags read extra files.
SIMPLE_FLAGS = {
    "-c", "-MD", "-MMD", "-pipe", "-pthread", "-ansi", "-pedantic",
    "-pedantic-errors", "-w", "-nostdinc", "-nostdinc++", "-nostdlibinc", "-nobuiltininc",
    "-no-canonical-prefixes", "-ffile-compilation-dir=.", "-fPIC", "-fpic",
    "-fPIE", "-fpie", "-fcolor-diagnostics", "-fno-color-diagnostics",
    "-fdiagnostics-show-inlining-chain", "-fdiagnostics-print-source-range-info",
    "-gsimple-template-names", "-gline-tables-only", "-gcolumn-info",
    "-gno-column-info", "-gdwarf-4", "-gdwarf-5", "-gpubnames",
    "-Wa,--crel,--allow-experimental-crel",
}
FEATURES = (
    "delete-null-pointer-checks|strict-overflow|ident|math-errno|strict-aliasing|"
    "unwind-tables|asynchronous-unwind-tables|merge-all-constants|lifetime-dse|"
    "omit-frame-pointer|data-sections|function-sections|unique-section-names|"
    "exceptions|rtti|sized-deallocation|complete-member-pointers|whole-program-vtables|"
    "split-lto-unit|short-wchar|signed-char|unsigned-char|stack-clash-protection|"
    "addrsig|integrated-as|plt|common|standalone-debug|eliminate-unused-debug-types"
)
VALUE_FLAGS = re.compile(
    r"(?:-O[0123szg]|-g[0123]?|-std=[a-zA-Z0-9+]+|"
    r"-f(?:no-)?(?:" + FEATURES + r")|"
    r"-fstack-protector(?:-strong|-all)?|-fno-stack-protector|"
    r"-f(?:fp-contract|visibility|visibility-inlines|trivial-auto-var-init|lto)=[a-z0-9-]+|"
    r"-fvisibility-inlines-hidden|--target=[a-zA-Z0-9_-]+|"
    r"-m(?:32|64|sse[0-9.]*|ssse3|avx[0-9.]*|no-[a-z0-9-]+)|"
    r"-m(?:arch|cpu|tune|fpu|float-abi)=[a-zA-Z0-9_.+-]+|"
    r"-f(?:sanitize|sanitize-trap|sanitize-ignore-for-ubsan-feature)=[a-zA-Z0-9,_-]+)"
)
LLVM_OPTIONS = re.compile(
    r"-(?:instcombine-lower-dbg-declare|split-threshold-for-reg-with-hint|"
    r"inlinehint-threshold|import-instr-limit)=[0-9]+"
)
FILE_OPTIONS = ("--warning-suppression-mappings=", "-fsanitize-ignorelist=",
                "-fsanitize-blacklist=")
INCLUDE_OPTIONS = ("-isystem", "-iquote", "-idirafter", "-include", "-imacros", "-I")


class Miss(ValueError):
    """The original compiler must handle this action."""


def digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def key(output: str) -> str:
    return hashlib.sha256(output.encode()).hexdigest()


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, sort_keys=True)
            stream.write("\n")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path: Path) -> dict:
    result = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise Miss("invalid cache manifest")
    return result


def relative_path(value: str, cwd: Path, root: Path, output: bool = False) -> Path:
    if not value or Path(value).is_absolute() or re.search(r"[\s\x00\\:$`|;&<>@]", value):
        raise Miss("absolute or unsupported path")
    if output and (".." in Path(value).parts or Path(value).as_posix() != value):
        raise Miss("output traversal")
    path = Path(os.path.abspath(cwd / value))
    if not path.is_relative_to(root):
        raise Miss("path traversal outside source")
    if not path.resolve().is_relative_to(root):
        raise Miss("external symlink")
    # Output symlinks must never redirect the object or depfile writes.
    if output:
        current = root
        for part in path.relative_to(root).parts:
            current /= part
            if current.is_symlink():
                raise Miss("symlinked output")
    return path


def split_command(command: str) -> list[str]:
    argv = shlex.split(command)
    if (len(argv) >= 5 and Path(argv[0]).name in ("python3", "python")
            and Path(argv[1]).name == "upstream_object_cache.py"
            and argv[2:4] == ["compile", "--"]):
        return argv[4:]
    return argv


def action(argv: list[str], cwd: Path, root: Path) -> dict:
    if not argv or argv[0] not in (
        f"../../{CLANG}/bin/clang", f"../../{CLANG}/bin/clang++"
    ):
        raise Miss("nonrelative or unsupported compiler")
    if any(re.search(r"[\x00\n\r$`|;&<>@]", arg) or arg.startswith("/")
           or (re.search(r"=[\"']?/", arg) and not MODULE_NAME.fullmatch(arg)
               and arg != "-fmodules-cache-path=/not_exist_dummy_dir") for arg in argv):
        raise Miss("absolute path, response file or shell syntax")
    if "-ffile-compilation-dir=." not in argv:
        raise Miss("missing deterministic compilation directory")
    result = {"files": [], "trees": [], "source": None, "output": None, "depfile": None}
    inert_c_modules = False
    i = 1
    while i < len(argv):
        arg = argv[i]
        if arg == "-Xclang":
            if i + 1 == len(argv) or argv[i + 1] not in INERT_C_MODULE_OPTIONS:
                raise Miss("unsupported Clang frontend option")
            inert_c_modules = True
            i += 2
            continue
        if MODULE_NAME.fullmatch(arg):
            inert_c_modules = True
            i += 1
            continue
        if arg.startswith(("-include-pch", "-include-pth")):
            raise Miss("unsupported PCH input")
        if arg in ("-march=native", "-mcpu=native", "-mtune=native", "-fno-integrated-as"):
            raise Miss("host-dependent code generation")
        if arg in ("-o", "-MF"):
            field = "output" if arg == "-o" else "depfile"
            if result[field] is not None or i + 1 == len(argv):
                raise Miss("ambiguous output/depfile")
            result[field] = argv[i + 1]
            relative_path(argv[i + 1], cwd, cwd, output=True)
            i += 2
            continue
        if arg.startswith("-fcrash-diagnostics-dir="):
            relative_path(arg.split("=", 1)[1], cwd, root)
            i += 1
            continue
        if arg == "-mllvm":
            if i + 1 == len(argv) or not LLVM_OPTIONS.fullmatch(argv[i + 1]):
                raise Miss("unsupported LLVM option")
            i += 2
            continue
        if arg in ("-D", "-U"):
            if i + 1 == len(argv) or argv[i + 1].startswith("-"):
                raise Miss("invalid macro argument")
            i += 2
            continue
        if arg.startswith(("-D", "-U")) and len(arg) > 2:
            i += 1
            continue
        matched = False
        for prefix in INCLUDE_OPTIONS + ("--sysroot=",) + FILE_OPTIONS:
            if arg == prefix or arg.startswith(prefix) and len(arg) > len(prefix):
                if arg == prefix:
                    if i + 1 == len(argv):
                        raise Miss("missing path argument")
                    value = argv[i + 1]
                    i += 1
                else:
                    value = arg[len(prefix):]
                path = relative_path(value, cwd, root)
                if prefix == "--sysroot=":
                    result["trees"].append(path.relative_to(root).as_posix())
                elif prefix in FILE_OPTIONS or prefix in ("-include", "-imacros"):
                    result["files"].append(value)
                matched = True
                break
        if matched:
            i += 1
            continue
        if arg in SIMPLE_FLAGS or VALUE_FLAGS.fullmatch(arg) or re.fullmatch(r"-W(?![alp],)[a-zA-Z0-9_=+.,-]+", arg):
            i += 1
            continue
        if not arg.startswith("-") and Path(arg).suffix in (".c", ".cc", ".cpp", ".cxx"):
            if result["source"] is not None:
                raise Miss("multiple sources")
            relative_path(arg, cwd, root)
            result["source"] = arg
            i += 1
            continue
        raise Miss(f"unsupported flag/input: {arg}")
    if argv.count("-c") != 1 or sum(argv.count(x) for x in ("-MD", "-MMD")) != 1:
        raise Miss("not a single dependency-producing compile")
    if not all(result[x] for x in ("source", "output", "depfile")):
        raise Miss("missing source/output/depfile")
    if not result["output"].endswith(".o") or result["output"] == result["depfile"]:
        raise Miss("unsupported output")
    if inert_c_modules and (not argv[0].endswith("/clang") or Path(result["source"]).suffix != ".c"):
        raise Miss("inert module options require non-module C")
    return result


def murmur_hash64a(command: bytes, seed: int = 0xDECAFBADDECAFBAD) -> int:
    """Ninja's 64-bit command hash, not its 32-bit path hash seed."""
    multiplier, mask = 0xC6A4A7935BD1E995, (1 << 64) - 1
    value = (seed ^ (len(command) * multiplier)) & mask
    end = len(command) // 8 * 8
    for (word,) in struct.iter_unpack("<Q", command[:end]):
        word = word * multiplier & mask
        word ^= word >> 47
        word = word * multiplier & mask
        value = (value ^ word) * multiplier & mask
    if end != len(command):
        value ^= int.from_bytes(command[end:], "little")
        value = value * multiplier & mask
    value ^= value >> 47
    value = value * multiplier & mask
    return value ^ (value >> 47)


def ninja_log(path: Path) -> dict:
    """Read v5/v6/v7 metadata, retaining opaque hashes and the last output record."""
    result = {}
    with path.open(encoding="utf-8") as stream:
        header = stream.readline(129)
        versions = {"# ninja log v5\n": 5, "# ninja log v6\n": 6, "# ninja log v7\n": 7}
        if header not in versions:
            suffix = " [truncated]" if len(header) > 128 else ""
            raise Miss(f"unsupported Ninja log (requires v5, v6 or v7): {header[:128].rstrip()!r}{suffix}")
        for line in stream:
            fields = line.rstrip("\n").split("\t")
            if not line.endswith("\n") or len(fields) != 5:
                raise Miss("malformed Ninja log")
            start, end, mtime, output, command_hash = fields
            if (not all(re.fullmatch(r"[0-9]+", value) for value in (start, end, mtime))
                    or int(end) < int(start) or not 0 < int(mtime) < 1 << 63):
                raise Miss("invalid Ninja log timestamp")
            if not output or "\x00" in output:
                raise Miss("invalid Ninja log output")
            if not re.fullmatch(r"[0-9a-fA-F]{1,16}", command_hash):
                raise Miss("invalid Ninja log command hash")
            result[output] = (int(mtime), int(command_hash, 16), versions[header])
    return result


def ninja_deps(path: Path) -> dict:
    """Read v4 path/checksum and dependency records, retaining latest records."""
    paths, records = [], {}
    with path.open("rb") as stream:
        if stream.read(16) != b"# ninjadeps\n\x04\x00\x00\x00":
            raise Miss("unsupported Ninja deps (requires v4)")
        while header := stream.read(4):
            if len(header) != 4:
                raise Miss("truncated Ninja deps header")
            size, = struct.unpack("<I", header)
            dependency, size = bool(size & 0x80000000), size & 0x7fffffff
            if size < 4 or size % 4 or size > 1024 * 1024:
                raise Miss("invalid Ninja deps record length")
            payload = stream.read(size)
            if len(payload) != size:
                raise Miss("truncated Ninja deps record")
            if dependency:
                if size < 12:
                    raise Miss("invalid Ninja dependency record")
                output, low, high, *ids = struct.unpack(f"<{size // 4}I", payload)
                if output >= len(paths) or any(index >= len(paths) for index in ids):
                    raise Miss("invalid Ninja dependency id")
                records[paths[output]] = (low | high << 32, [paths[index] for index in ids])
            else:
                checksum, = struct.unpack("<I", payload[-4:])
                if checksum != (~len(paths) & 0xffffffff):
                    raise Miss("invalid Ninja path checksum")
                name = payload[:-4].rstrip(b"\x00")
                if not name or b"\x00" in name:
                    raise Miss("invalid Ninja path")
                paths.append(name.decode("utf-8"))
    return records


def stamp(path: Path) -> list[int]:
    info = path.stat()
    return [info.st_size, info.st_mtime_ns, info.st_ctime_ns, info.st_ino,
            stat.S_IMODE(info.st_mode)]


def tree_link(path: Path, root: Path) -> dict:
    target = os.readlink(path)
    try:
        resolved = path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise Miss("dangling or cyclic toolchain/sysroot symlink") from error
    if os.path.isabs(target) or not resolved.is_relative_to(root):
        raise Miss("external toolchain/sysroot symlink")
    if not (resolved.is_file() or resolved.is_dir()):
        raise Miss("special toolchain/sysroot symlink")
    return {"link": target, "kind": "directory" if resolved.is_dir() else "file"}


def tree_inventory(root: Path) -> dict:
    if not root.is_dir() or root.is_symlink():
        raise Miss("missing or symlinked toolchain/sysroot")
    result = {}
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, onerror=walk_error, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                # Record aliases; os.walk must never descend through them.
                result[relative] = tree_link(path, root)
            elif path.is_dir():
                result[relative] = {"directory": stat.S_IMODE(path.stat().st_mode)}
            elif path.is_file():
                result[relative] = {"sha256": digest(path), "stamp": stamp(path)}
            else:
                raise Miss("special toolchain/sysroot file")
    if not result:
        raise Miss("empty toolchain/sysroot")
    return result


def tree_identity(entries: dict) -> dict:
    return {name: ({"sha256": entry["sha256"], "mode": entry["stamp"][-1]}
                   if "sha256" in entry else entry) for name, entry in entries.items()}


def paired_tree(src: Path, donor: Path, relative: str) -> dict:
    canonical = tree_inventory(src / relative)
    upstream = tree_inventory(donor / relative)
    if tree_identity(canonical) != tree_identity(upstream):
        raise Miss(f"different complete tree: {relative}")
    return {"canonical": canonical, "donor": upstream}


def verify_tree(root: Path, expected: dict, *, freshness: dict | None = None) -> None:
    # Full scans are intentional; aliases are checked without following them.
    if not root.is_dir() or root.is_symlink():
        raise Miss("missing or symlinked toolchain/sysroot")
    actual_names = set()
    def walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, onerror=walk_error, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            relative = path.relative_to(root).as_posix()
            actual_names.add(relative)
            entry = expected.get(relative)
            if entry is None:
                raise Miss("toolchain/sysroot tree changed")
            if "link" in entry:
                if not path.is_symlink() or tree_link(path, root) != entry:
                    raise Miss("toolchain/sysroot symlink changed")
            elif "directory" in entry:
                if path.is_symlink() or not path.is_dir() or stat.S_IMODE(path.stat().st_mode) != entry["directory"]:
                    raise Miss("toolchain/sysroot directory changed")
            elif path.is_symlink() or not path.is_file():
                raise Miss("toolchain/sysroot file changed")
            elif stamp(path) != entry["stamp"] and (
                digest(path) != entry["sha256"] or stamp(path)[-1] != entry["stamp"][-1]
            ):
                raise Miss("toolchain/sysroot content changed")
            if freshness is not None and "sha256" in entry:
                if not input_is_fresh(path.stat().st_mtime_ns, freshness):
                    raise Miss("stale donor toolchain/sysroot")
    if actual_names != set(expected):
        raise Miss("toolchain/sysroot tree incomplete")


def host_ninja() -> str:
    for name in ("/usr/bin/ninja", "/bin/ninja", "/usr/local/bin/ninja"):
        if Path(name).is_file() and os.access(name, os.X_OK):
            return name
    raise Miss("trusted host Ninja unavailable")


def compdb(cwd: Path) -> list[dict]:
    ninja = host_ninja()
    def run(*args):
        return subprocess.run([ninja, "-t", *args], cwd=cwd, check=True,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True, timeout=NINJA_TIMEOUT).stdout
    rules = [line for line in run("rules").splitlines()
             if re.fullmatch(r"(?:[a-zA-Z0-9_]+_)?(?:cc|cxx)", line)]
    if not rules:
        return []
    result = json.loads(run("compdb", *rules))
    if not isinstance(result, list):
        raise Miss("invalid compilation database")
    return result


def dependency_name(value: str, cwd: Path, root: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        if not path.is_relative_to(root) or not path.resolve().is_relative_to(root):
            raise Miss("external dependency")
        value = os.path.relpath(path, cwd)
    path = relative_path(value, cwd, root)
    if not path.is_file() or path.suffix in (".pch", ".gch", ".pcm", ".hmap"):
        raise Miss("missing or unsupported dependency")
    return os.path.relpath(path, cwd)


def object_times(actual: int, dependency: int, logged: int, version: int,
                 allow_truncated_mtimes: bool) -> dict:
    if version not in (5, 6, 7):
        raise Miss("unsupported Ninja log timestamp version")
    # v6/v7 use command-start time (or restat mtime); v5 uses output mtime.
    # Metadata retains nanoseconds even when a trusted GNU tar archive does not.
    if (actual != dependency and not (
            allow_truncated_mtimes and actual == dependency // NANOSECOND * NANOSECOND)
            or dependency < logged or version == 5 and dependency != logged):
        raise Miss("stale Ninja object/dependency mtime")
    cutoff = logged // NANOSECOND * NANOSECOND if allow_truncated_mtimes else logged
    return {"object_mtime": actual, "dependency_mtime": dependency, "log_mtime": logged,
            "freshness_cutoff": cutoff, "coarse_mtimes": allow_truncated_mtimes}


def input_is_fresh(mtime: int, record: dict) -> bool:
    cutoff = record["freshness_cutoff"]
    return mtime < cutoff if record["coarse_mtimes"] else mtime <= cutoff


def input_record(name: str, cwd: Path, root: Path, freshness: dict) -> dict:
    name = dependency_name(name, cwd, root)
    path = cwd / name
    if not input_is_fresh(path.stat().st_mtime_ns, freshness):
        raise Miss("stale donor input")
    return {"path": name, "sha256": digest(path), "stamp": stamp(path)}


def prepare(src: Path, donor: Path, platform: str, arch: str, work: Path, *,
            allow_truncated_mtimes: bool = False) -> dict:
    """Prepare candidates; enable coarse mtimes only for a pinned source receipt."""
    report = {"status": "miss", "counts": {"candidates": 0, "prepared": 0,
              "rejected": 0, "bytes_staged": 0}, "reasons": [], "rejections": {}}
    cache = work.resolve() / CACHE
    reasons = Counter()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        # Disable any previous generation before validating the new donor.
        write_json(cache / "manifest.json", {"schema": SCHEMA, "status": "miss"})
        if platform != "linux" or not sys.platform.startswith("linux"):
            raise Miss(f"unsupported platform: {platform}; Linux only")
        if arch not in ("x64", "arm64"):
            raise Miss(f"unsupported architecture: {arch}")
        src, donor, work = src.resolve(), donor.resolve(), work.resolve()
        if src != work / "src" or src == donor or donor.is_relative_to(src):
            raise Miss("requires independent donor and WORK/src")
        cwd = donor / "out/Default"
        if not (src / "out/Chromix/build.ninja").is_file():
            raise Miss("canonical GN generation required")
        trees = {CLANG.as_posix(): paired_tree(src, donor, CLANG.as_posix())}
        tree_mtimes = {}
        logs, dependencies = ninja_log(cwd / ".ninja_log"), ninja_deps(cwd / ".ninja_deps")
        database = compdb(cwd)
        outputs = Counter(item.get("output") for item in database)
        with tempfile.TemporaryDirectory(prefix="generation-", dir=cache) as temporary:
            generation = Path(temporary)
            for item in database:
                report["counts"]["candidates"] += 1
                try:
                    if Path(item["directory"]).resolve() != cwd or outputs[item["output"]] != 1:
                        raise Miss("ambiguous compilation database output")
                    command = item["command"]
                    argv = split_command(command)
                    parsed = action(argv, cwd, donor)
                    compiler = relative_path(argv[0], src / "out/Chromix", src)
                    with compiler.open("rb") as stream:
                        if stream.read(4) != b"\x7fELF":
                            raise Miss("compiler is not a Linux executable")
                    output = parsed["output"]
                    if output != item["output"] or parsed["source"] != item["file"]:
                        raise Miss("compilation database disagrees with command")
                    if output not in logs or output not in dependencies:
                        raise Miss("missing Ninja object record")
                    mtime, command_hash, log_version = logs[output]
                    # v7 uses rapidhash, not the v5/v6 MurmurHash64A below.
                    if log_version not in (5, 6):
                        raise Miss(f"unsupported Ninja command hash for object reuse: v{log_version}")
                    dep_mtime, names = dependencies[output]
                    obj = relative_path(output, cwd, cwd, output=True)
                    if murmur_hash64a(command.encode()) != command_hash:
                        raise Miss("stale Ninja command hash")
                    freshness = object_times(obj.stat().st_mtime_ns, dep_mtime, mtime,
                                             log_version, allow_truncated_mtimes)
                    if not names:
                        raise Miss("empty Ninja dependencies")
                    names = sorted({dependency_name(name, cwd, donor) for name in names})
                    if dependency_name(parsed["source"], cwd, donor) not in names:
                        raise Miss("source absent from Ninja dependencies")
                    for relative in parsed["trees"]:
                        if relative not in trees:
                            trees[relative] = paired_tree(src, donor, relative)
                    for relative in [CLANG.as_posix()] + parsed["trees"]:
                        if relative not in tree_mtimes:
                            tree_mtimes[relative] = max(entry.get("stamp", [0, 0])[1]
                                                       for entry in trees[relative]["donor"].values())
                        if not input_is_fresh(tree_mtimes[relative], freshness):
                            raise Miss("stale donor toolchain/sysroot")
                    inputs = [input_record(name, cwd, donor, freshness)
                              for name in sorted(set(names + parsed["files"]))]
                    record = {"argv": argv, "output": output, **freshness,
                              "dependencies": names, "inputs": inputs,
                              "trees": parsed["trees"], "object_sha256": digest(obj)}
                    name = key(output)
                    shutil.copyfile(obj, generation / f"{name}.o")
                    if (digest(generation / f"{name}.o") != record["object_sha256"]
                            or obj.stat().st_mtime_ns != record["object_mtime"]):
                        raise Miss("donor object changed during preparation")
                    write_json(generation / f"{name}.json", record)
                    report["counts"]["prepared"] += 1
                    report["counts"]["bytes_staged"] += obj.stat().st_size
                except (Miss, OSError, ValueError, KeyError, TypeError) as error:
                    report["counts"]["rejected"] += 1
                    reasons[str(error)] += 1
            if not report["counts"]["prepared"]:
                raise Miss("no eligible upstream objects")
            write_json(generation / "trees.json", trees)
            destination = cache / (generation.name + "-ready")
            generation.rename(destination)
        location = {"path": str(donor), "relative": False}
        if donor.is_relative_to(work):
            location = {"path": donor.relative_to(work).as_posix(), "relative": True}
        write_json(cache / "manifest.json", {"schema": SCHEMA, "status": "ready",
                   "generation": destination.name, "donor": location, "arch": arch})
        report["status"] = "ready"
    except (Miss, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        report["reasons"].append(str(error))
    report["rejections"] = dict(reasons)
    if reasons:
        report["reasons"].extend(f"{count} rejected: {reason}" for reason, count in reasons.most_common(20))
    return report


def depfile_names(path: Path, cwd: Path, root: Path) -> set[str]:
    text = path.read_text(encoding="utf-8").replace("\\\n", " ")
    # Eligible Chromium paths need no Make escaping, so reject unfamiliar syntax.
    if text.count(":") != 1 or re.search(r"[\\#$]", text):
        raise Miss("unsupported preprocessor depfile")
    target, names = text.split(":")
    if target.strip() != "__upstream_object__":
        raise Miss("unexpected preprocessor dependency target")
    return {dependency_name(name, cwd, root) for name in names.split()}


def verify_inputs(record: dict, donor: Path, src: Path) -> None:
    for entry in record["inputs"]:
        name = entry["path"]
        old = donor / "out/Default" / dependency_name(name, donor / "out/Default", donor)
        new = src / "out/Chromix" / dependency_name(name, src / "out/Chromix", src)
        if not input_is_fresh(old.stat().st_mtime_ns, record) or digest(old) != entry["sha256"]:
            raise Miss("stale donor input")
        if digest(new) != entry["sha256"]:
            raise Miss("canonical source/header/config changed")
        # Pragmas can suppress -Werror=date-time. Inline assembler can read files
        # that are not represented in Clang's dependency list.
        if re.search(rb"\b(?:__DATE__|__TIME__|__TIMESTAMP__)\b|\.incbin\b|\.include\b|#\s*embed\b",
                     old.read_bytes()):
            raise Miss("time-dependent or hidden input")


def preprocess(argv: list[str], cwd: Path, root: Path, compiler: Path,
               output: Path, depfile: Path) -> set[str]:
    command, i = [], 0
    while i < len(argv):
        arg = argv[i]
        if arg in ("-o", "-MF"):
            i += 2
            continue
        if arg not in ("-c", "-MMD", "-MD"):
            command.append(arg)
        i += 1
    guard = output.parent / "deterministic.h"
    if not guard.exists():
        guard.write_text('#pragma clang diagnostic push\n'
                         '#pragma clang diagnostic ignored "-Wbuiltin-macro-redefined"\n'
                         '#undef __DATE__\n#undef __TIME__\n#undef __TIMESTAMP__\n'
                         '#pragma clang diagnostic pop\n'
                         '#pragma GCC poison __DATE__ __TIME__ __TIMESTAMP__\n', encoding="utf-8")
    command[1:1] = ["-include", str(guard)]
    command += ["-E", "-MD", "-MT", "__upstream_object__", "-MF", str(depfile),
                "-o", str(output), "-Werror=date-time", "-fno-crash-diagnostics"]
    # argv[0] retains the donor-relative resource lookup. The executable is never
    # taken from the donor; it is the content-verified canonical Clang binary.
    with tempfile.TemporaryFile() as errors:
        result = subprocess.run(command, executable=str(compiler), cwd=cwd,
                                stdout=subprocess.DEVNULL, stderr=errors,
                                timeout=PREPROCESS_TIMEOUT)
    if result.returncode:
        raise Miss("preprocessor failed")
    text = depfile.read_text(encoding="utf-8")
    # The identical private poison header affects neither dependency closure.
    text = text.replace(str(guard), "")
    depfile.write_text(text, encoding="utf-8")
    return depfile_names(depfile, cwd, root)


def try_hit(argv: list[str], cwd: Path, src: Path, cache: Path) -> int:
    if not sys.platform.startswith("linux"):
        raise Miss("unsupported platform; Linux only")
    if any(name in os.environ for name in ENVIRONMENT_INPUTS):
        raise Miss("compiler-affecting environment")
    parsed = action(argv, cwd, src)
    manifest = read_json(cache / "manifest.json")
    if manifest.get("schema") != SCHEMA or manifest.get("status") != "ready":
        raise Miss("cache not prepared")
    generation = relative_path(manifest["generation"], cache, cache, output=True)
    record = read_json(generation / f"{key(parsed['output'])}.json")
    if argv != record["argv"] or parsed["output"] != record["output"]:
        raise Miss("canonical argv differs from donor")
    location = manifest["donor"]
    donor = ((cache.parent / location["path"]) if location["relative"]
             else Path(location["path"])).resolve()
    trees = read_json(generation / "trees.json")
    for relative in [CLANG.as_posix()] + record["trees"]:
        verify_tree(src / relative, trees[relative]["canonical"])
        verify_tree(donor / relative, trees[relative]["donor"], freshness=record)
    verify_inputs(record, donor, src)
    compiler = relative_path(argv[0], cwd, src).resolve(strict=True)
    payload = generation / f"{key(parsed['output'])}.o"
    if digest(payload) != record["object_sha256"]:
        raise Miss("cached object integrity mismatch")
    with tempfile.TemporaryDirectory(prefix="preprocess-", dir=cache) as temporary:
        temporary = Path(temporary)
        canonical_deps = preprocess(argv, cwd, src, compiler, temporary / "canonical.i", temporary / "canonical.d")
        donor_deps = preprocess(argv, donor / "out/Default", donor, compiler,
                                temporary / "donor.i", temporary / "donor.d")
        if digest(temporary / "canonical.i") != digest(temporary / "donor.i"):
            raise Miss("preprocessor bytes mismatch")
        recorded = set(record["dependencies"])
        if not recorded <= donor_deps or canonical_deps != donor_deps:
            raise Miss("preprocessor dependency set mismatch")
        # MMD omits system headers: accept extras only in fully matched packaged
        # sysroots/LLVM trees, and still require their donor timestamps to be old.
        for name in donor_deps - recorded:
            old = relative_path(name, donor / "out/Default", donor)
            if not any(old.is_relative_to(donor / relative)
                       for relative in [CLANG.as_posix()] + record["trees"]):
                raise Miss("unrecorded donor dependency")
            if not input_is_fresh(old.stat().st_mtime_ns, record):
                raise Miss("stale unrecorded donor dependency")
        if re.search(rb"\b(?:__DATE__|__TIME__|__TIMESTAMP__)\b|\.incbin\b|\.include\b",
                     (temporary / "donor.i").read_bytes()):
            raise Miss("hidden preprocessed input")
        verify_inputs(record, donor, src)
        output = relative_path(parsed["output"], cwd, cwd, output=True)
        depfile = relative_path(parsed["depfile"], cwd, cwd, output=True)
        output.parent.mkdir(parents=True, exist_ok=True)
        depfile.parent.mkdir(parents=True, exist_ok=True)
        dependency_text = (temporary / "canonical.d").read_text(encoding="utf-8")
        dependency_text = dependency_text.replace("__upstream_object__:", parsed["output"] + ":", 1)
        depfile.write_text(dependency_text, encoding="utf-8")
        fd, name = tempfile.mkstemp(prefix=".upstream-", dir=output.parent)
        os.close(fd)
        try:
            shutil.copyfile(payload, name)
            os.replace(name, output)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return output.stat().st_size


def compile_command(argv: list[str]) -> int:
    """Attempt one object, otherwise invoke the unmodified canonical command."""
    cwd = Path.cwd().resolve()
    src = cwd.parent.parent
    cache = src.parent / CACHE
    output = None
    if "-o" in argv and argv.index("-o") + 1 < len(argv):
        output = argv[argv.index("-o") + 1]
    receipt = {"status": "miss", "output": output, "bytes": 0}
    try:
        if cwd.name != "Chromix" or cwd.parent.name != "out" or src.name != "src":
            raise Miss("not WORK/src/out/Chromix")
        receipt["bytes"] = try_hit(argv, cwd, src, cache)
        receipt["status"] = "hit"
        receipt["reason"] = "verified upstream object"
        result = 0
    except (Miss, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        receipt["reason"] = str(error)
        try:
            result = subprocess.call(argv)
        except OSError as compiler_error:
            print(f"upstream object cache: {compiler_error}", file=sys.stderr)
            result = 127
    receipt["returncode"] = result
    if cwd == src / "out/Chromix" and src.name == "src":
        try:
            write_json(cache / "receipts" / f"{key(output or shlex.join(argv))}.json", receipt)
        except OSError:
            pass
    return result


def main() -> int:
    if len(sys.argv) < 4 or sys.argv[1:3] != ["compile", "--"]:
        print("usage: upstream_object_cache.py compile -- COMPILER [ARGS...]", file=sys.stderr)
        return 2
    result = compile_command(sys.argv[3:])
    if result < 0:
        import signal
        signal.signal(-result, signal.SIG_DFL)
        os.kill(os.getpid(), -result)
    return result


if __name__ == "__main__":
    sys.exit(main())
