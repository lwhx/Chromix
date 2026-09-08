#!/usr/bin/env python3
"""Reclaim only disposable Xcode resources on GitHub-hosted macOS runners."""
import fnmatch
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


APPLICATIONS = Path("/Applications")
SIMULATOR_RUNTIMES = Path("/Library/Developer/CoreSimulator/Profiles/Runtimes")
# Best-effort cleanup target; actual archive/chunk admission belongs to the fetcher.
CLEANUP_TARGET_BYTES = 120 * 1024**3
MOBILE_PLATFORMS = {
    "iPhoneOS": "iPhoneOS",
    "iPhoneSimulator": "iPhoneSimulator",
    "AppleTVOS": "AppleTVOS",
    "AppleTVSimulator": "AppleTVSimulator",
    "WatchOS": "WatchOS",
    "WatchSimulator": "WatchSimulator",
    "XROS": "XROS",
    "XRSimulator": "XRSimulator",
}


class CleanupError(RuntimeError):
    pass


def log(message):
    print("macOS disk cleanup: " + message, flush=True)


def overlaps(left, right):
    return left == right or left in right.parents or right in left.parents


def path_chain(path):
    """Return lookup carriers and the endpoint, with bounded symlink expansion."""
    path = Path(path)
    if not path.is_absolute():
        path = Path.cwd() / path
    paths = [path]
    pending = list(path.parts[1:])
    cursor = Path(path.anchor)
    links = 0
    while pending:
        name = pending.pop(0)
        if name == "..":
            paths.append(cursor)
            cursor = cursor.parent
            continue
        candidate = cursor / name
        if candidate.is_symlink():
            links += 1
            if links > 40:
                raise CleanupError(f"protected path has a symlink cycle or too many links: {path}")
            paths.append(candidate)
            target = Path(os.readlink(candidate))
            if target.is_absolute():
                cursor = Path(target.anchor)
                pending = list(target.parts[1:]) + pending
            else:
                pending = list(target.parts) + pending
        else:
            cursor = candidate
    paths.append(cursor)
    return paths


def plain_directory(path):
    # Reject aliases at every level; rm must see the same confined path we checked.
    return (path.is_absolute() and path.is_dir()
            and not any(part.is_symlink() for part in (path, *path.parents)))


def candidates(root, pattern, kind):
    if not root.exists() and not root.is_symlink():
        return
    if not plain_directory(root):
        log(f"refused unsafe allowlist directory: {root}")
        return
    for path in sorted(root.iterdir()):
        if fnmatch.fnmatchcase(path.name, pattern):
            yield path, root, pattern, kind


def safe_target(path, root, pattern, kind, selected_paths, protected):
    if (path.parent != root or not fnmatch.fnmatchcase(path.name, pattern)
            or not plain_directory(path) or path.resolve(strict=True) != path
            or path.is_mount()):
        log(f"refused unsafe target: {path}")
        return False
    if kind == "xcode":
        if any(overlaps(path, selected) for selected in selected_paths):
            log(f"keep selected Xcode: {path}")
            return False
        if not (path / "Contents/Developer/Platforms/MacOSX.platform").is_dir():
            log(f"refused non-Xcode bundle: {path}")
            return False
    if any(overlaps(path, keep) for keep in protected):
        log(f"refused protected target: {path}")
        return False
    return True


def remove_target(path):
    # BSD rm -x does not cross filesystems; no trailing slash or symlink traversal.
    subprocess.run(["sudo", "-n", "/bin/rm", "-rf", "-x", "--", str(path)], check=True)
    if path.exists() or path.is_symlink():
        raise CleanupError(f"removal incomplete: {path}")


def record_space(label, work_volume):
    usage = shutil.disk_usage(work_volume)
    log(f"{label}: volume={work_volume}; free_bytes={usage.free}; "
        f"free_GiB={usage.free / 1024**3:.2f}; cleanup_target_GiB={CLEANUP_TARGET_BYTES / 1024**3:.0f}")
    subprocess.run(["df", "-h", str(work_volume)], check=True)
    return usage.free


def cleanup():
    if not (platform.system() == "Darwin" and os.environ.get("CI") == "true"
            and os.environ.get("GITHUB_ACTIONS") == "true"
            and os.environ.get("RUNNER_OS") == "macOS"
            and os.environ.get("RUNNER_ENVIRONMENT") == "github-hosted"):
        log("skipped: requires CI on a GitHub-hosted macOS runner")
        return

    environment_paths = {}
    for name in ("DEVELOPER_DIR", "RUNNER_TEMP", "GITHUB_WORKSPACE", "HOME"):
        value = os.environ.get(name, "")
        if not value or not Path(value).is_absolute():
            raise CleanupError(f"requires an existing absolute {name}")
        environment_paths[name] = path_chain(value)
        if not environment_paths[name][-1].is_dir():
            raise CleanupError(f"requires an existing absolute {name}")
    developer = environment_paths["DEVELOPER_DIR"][-1]
    selected_app = developer.parent.parent
    if (developer.name != "Developer" or developer.parent.name != "Contents"
            or selected_app.suffix != ".app"):
        raise CleanupError("DEVELOPER_DIR must select a full Xcode Contents/Developer directory")
    selected_paths = [selected_app, *environment_paths["DEVELOPER_DIR"]]
    sdk_output = subprocess.check_output(
        ["/usr/bin/xcrun", "--sdk", "macosx", "--show-sdk-path"], text=True).strip()
    if not sdk_output or not Path(sdk_output).is_absolute():
        raise CleanupError("selected macOS SDK is not an existing absolute directory")
    sdk_paths = path_chain(sdk_output)
    sdk = sdk_paths[-1]
    if not sdk.is_dir():
        raise CleanupError("selected macOS SDK is not an existing absolute directory")
    mac_paths = path_chain(developer / "Platforms/MacOSX.platform")
    if not mac_paths[-1].is_dir():
        raise CleanupError("selected Xcode is missing MacOSX.platform")
    protected = [*sdk_paths, *mac_paths]
    # Selected developer ancestors are protected separately to allow mobile SDK cleanup.
    protected.extend(path for path in selected_paths if path not in (selected_app, developer))
    for path in (__file__, sys.executable, Path.cwd()):
        protected.extend(path_chain(path))
    for name in ("HOME", "RUNNER_TEMP", "GITHUB_WORKSPACE", "RUNNER_WORKSPACE", "RUNNER_TOOL_CACHE"):
        if os.environ.get(name):
            value = Path(os.environ[name])
            if not value.is_absolute():
                raise CleanupError(f"requires an absolute {name}")
            protected.extend(environment_paths[name] if name in environment_paths else path_chain(value))
    for value in os.environ.get("PATH", "").split(os.pathsep):
        protected.extend(path_chain(value or Path.cwd()))
    toolchain_paths = path_chain(developer / "Toolchains")
    toolchains = toolchain_paths[-1]
    if not toolchains.is_dir():
        raise CleanupError("selected Xcode is missing Toolchains")
    protected.extend(toolchain_paths)
    default_paths = path_chain(toolchains / "XcodeDefault.xctoolchain")
    if not default_paths[-1].is_dir():
        raise CleanupError("selected Xcode is missing XcodeDefault.xctoolchain")
    protected.extend(default_paths)
    # Inspect direct toolchain entries only, not their potentially large contents.
    for entry in toolchains.iterdir():
        protected.extend(path_chain(entry))
    for tool in ("clang", "ld"):
        output = subprocess.check_output(
            ["/usr/bin/xcrun", "--sdk", "macosx", "--find", tool], text=True).strip()
        if not output or not Path(output).is_absolute():
            raise CleanupError(f"xcrun {tool} is not an existing absolute executable")
        paths = path_chain(output)
        if not paths[-1].is_file() or not os.access(paths[-1], os.X_OK):
            raise CleanupError(f"xcrun {tool} is not an existing absolute executable")
        protected.extend(paths)
        log(f"preserve {tool}={output}; canonical={paths[-1]}")
    work_volume = environment_paths["RUNNER_TEMP"][-1]
    log(f"preserve DEVELOPER_DIR={os.environ['DEVELOPER_DIR']}; canonical={developer}; macOS SDK={sdk}")
    before = record_space("before", work_volume)
    try:
        groups = [(APPLICATIONS, "Xcode*.app", "xcode"),
                  (APPLICATIONS / "Xcodes", "Xcode*.app", "xcode")]
        for prefix in ("iOS", "tvOS", "watchOS", "visionOS"):
            groups.append((SIMULATOR_RUNTIMES, prefix + "*.simruntime", "runtime"))
        # Only mobile SDKs/runtimes inside a real, allowlisted selected bundle.
        if (selected_app.parent in (APPLICATIONS, APPLICATIONS / "Xcodes")
                and plain_directory(selected_app)):
            for platform_name, sdk_name in MOBILE_PLATFORMS.items():
                mobile = developer / "Platforms" / (platform_name + ".platform")
                groups.append((mobile / "Library/Developer/CoreSimulator/Profiles/Runtimes",
                               "*.simruntime", "runtime"))
                groups.append((mobile / "Developer/SDKs", sdk_name + "*.sdk", "sdk"))
        for root, pattern, kind in groups:
            if shutil.disk_usage(work_volume).free >= CLEANUP_TARGET_BYTES:
                break
            for path, allowed_root, allowed_pattern, allowed_kind in candidates(root, pattern, kind):
                if shutil.disk_usage(work_volume).free >= CLEANUP_TARGET_BYTES:
                    break
                if not safe_target(path, allowed_root, allowed_pattern, allowed_kind, selected_paths, protected):
                    continue
                free = shutil.disk_usage(work_volume).free
                log(f"remove {kind}: {path}")
                remove_target(path)
                after = shutil.disk_usage(work_volume).free
                log(f"removed: {path}; reclaimed_bytes={after - free}; free_bytes={after}")
    finally:
        after = record_space("after", work_volume)
        log(f"net_reclaimed_bytes={after - before}")
    if after < CLEANUP_TARGET_BYTES:
        log("cleanup target not reached after confined cleanup; "
            f"free_bytes={after}; cleanup_target_bytes={CLEANUP_TARGET_BYTES}; "
            "deferring capacity admission to actual archive/chunk space checks")
    log("confined cleanup complete; restore capacity is not guaranteed; "
        "required restoration still fails on insufficient space")


def main():
    try:
        cleanup()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"::error::macOS disk cleanup failed: {error}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
