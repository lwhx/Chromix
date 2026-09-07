#!/usr/bin/env python3
"""Publish verified inner browser ZIPs from successful, same-repository CI runs."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

WORKFLOWS = {
    "build-cross-platform": (
        "chromix-linux-x64", "chromix-linux-arm64", "chromix-mac-x64", "chromix-mac-arm64",
    ),
    "build-win-x64-github": ("chromix-win-x64",),
}
ASSETS = {name + ".zip" for names in WORKFLOWS.values() for name in names}


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True).strip()


def api(repo: str, path: str):
    return json.loads(gh("api", f"repos/{repo}/{path}"))


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def parse_manifest(text: str) -> dict[str, str]:
    result = {}
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.fullmatch(r"([a-fA-F0-9]{64})\s+\*?(chromix-[\w-]+\.zip)", line.strip())
        if not match or match[2] not in ASSETS or match[2] in result:
            raise ValueError("Invalid or duplicate SHA256SUMS entry")
        result[match[2]] = match[1].lower()
    return result


def successful_runs(repo: str, head_sha: str) -> dict[str, dict]:
    runs = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100"))
    result = {}
    for page in runs:
        for run in page.get("workflow_runs", []):
            if (run.get("status") == "completed" and run.get("conclusion") == "success"
                    and run.get("head_branch") == "main"
                    and run.get("head_repository", {}).get("full_name") == repo
                    and run.get("event") in ("push", "workflow_dispatch")
                    and run.get("name") in WORKFLOWS
                    and run.get("path") == f".github/workflows/{run.get('name')}.yml"):
                result[run["name"]] = run
    return result


def validate_run(run: dict, repo: str) -> tuple[str, ...]:
    if (run.get("status") != "completed" or run.get("conclusion") != "success"
            or run.get("head_branch") != "main"
            or run.get("head_repository", {}).get("full_name") != repo
            or run.get("event") not in ("push", "workflow_dispatch")
            or run.get("name") not in WORKFLOWS
            or run.get("path") != f".github/workflows/{run.get('name')}.yml"
            or not re.fullmatch(r"[0-9a-f]{40}", run.get("head_sha", ""))):
        raise ValueError("Only successful, same-repository main browser builds may be released")
    return WORKFLOWS[run["name"]]


def validate_bundle(path: Path) -> None:
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(set(names)) != len(names):
            raise ValueError(f"Duplicate ZIP members in {path.name}")
        for name in names:
            parts = PurePosixPath(name).parts
            if (not parts or parts[0] != "chromix" or ".." in parts
                    or "\\" in name or ":" in name):
                raise ValueError(f"Unsafe browser ZIP member: {name}")
        if path.name == "chromix-win-x64.zip":
            required = {"chromix/chromix.cmd", "chromix/chrome.exe"}
        elif path.name.startswith("chromix-mac-"):
            required = {"chromix/chromix", "chromix/Chromium.app/Contents/MacOS/Chromium"}
        else:
            required = {"chromix/chromix", "chromix/chrome"}
        required |= {"chromix/LICENSE.chromix", "chromix/LICENSE.chromium"}
        if not required.issubset(names) or any(archive.getinfo(n).file_size == 0 for n in required):
            raise ValueError(f"Incomplete browser ZIP: {path.name}")
        if archive.testzip() is not None:
            raise ValueError(f"Corrupt browser ZIP: {path.name}")


def collect(repo: str, run: dict, root: Path) -> dict[str, Path]:
    result = {}
    for name in validate_run(run, repo):
        dest = root / name
        gh("run", "download", str(run["id"]), "--repo", repo, "--name", name, "--dir", str(dest))
        files = {p.relative_to(dest).as_posix(): p for p in dest.rglob("*") if p.is_file()}
        asset = name + ".zip"
        if asset not in files or set(files) - {asset, "SHA256SUMS"}:
            raise ValueError(f"Unexpected artifact contents: {name}")
        checksum = digest(files[asset])
        if "SHA256SUMS" in files:
            sums = parse_manifest(files["SHA256SUMS"].read_text(encoding="ascii"))
            if sums.get(asset) != checksum:
                raise ValueError(f"Checksum mismatch: {asset}")
        elif name != "chromix-win-x64":
            raise ValueError(f"Missing checksum: {asset}")
        # Older successful Windows jobs uploaded only the inner ZIP.
        validate_bundle(files[asset])
        result[asset] = files[asset]
    return result


def publish(repo: str, run: dict, tag: str, bundles: dict[str, Path], root: Path) -> None:
    releases = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"))
    release = next((r for page in releases for r in page if r["tag_name"] == tag), None)
    existing = {a["name"]: a for a in release["assets"]} if release else {}
    hashes = {}
    old_dir = root / "existing"
    old_dir.mkdir()
    if "SHA256SUMS" in existing:
        gh("release", "download", tag, "--repo", repo, "--pattern", "SHA256SUMS", "--dir", str(old_dir))
        hashes = parse_manifest((old_dir / "SHA256SUMS").read_text(encoding="ascii"))
        if set(hashes) - set(existing):
            raise ValueError("Existing manifest references missing release assets")
    for name in sorted(set(existing) & ASSETS):
        if name not in hashes or name in bundles:
            gh("release", "download", tag, "--repo", repo, "--pattern", name, "--dir", str(old_dir))
            actual = digest(old_dir / name)
            if name in hashes and hashes[name] != actual:
                raise ValueError(f"Existing release checksum mismatch: {name}")
            hashes[name] = actual
    for name, path in bundles.items():
        checksum = digest(path)
        if name in existing and hashes[name] != checksum:
            raise ValueError(f"Refusing to replace a different published browser: {name}")
        hashes[name] = checksum
    manifest = root / "SHA256SUMS"
    manifest.write_text("".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes)), encoding="ascii")
    provenance = (f"\n\nVerified build: {run['html_url']}\n"
                  f"Source commit: `{run['head_sha']}`\n"
                  f"Assets: {', '.join(sorted(bundles))}.\n")
    notes = root / "notes.md"
    body = (release.get("body") or "") if release else (
        f"Chromix {tag[1:]} browser bundles. Browser assets are ZIP archives; "
        "verify downloads against SHA256SUMS. Only successfully built platforms are attached.\n\n"
        "macOS bundles are not Developer ID signed or notarized. Gatekeeper may block them."
    )
    if run["html_url"] not in body:
        body += provenance
    notes.write_text(body, encoding="utf-8")
    if not release:
        gh("release", "create", tag, "--repo", repo, "--target", run["head_sha"],
           "--title", f"Chromix {tag[1:]}", "--draft", "--notes-file", str(notes))
    for name, path in bundles.items():
        if name not in existing:
            gh("release", "upload", tag, str(path), "--repo", repo)
    gh("release", "upload", tag, str(manifest), "--repo", repo, "--clobber")
    gh("release", "edit", tag, "--repo", repo, "--draft=false", "--notes-file", str(notes))
    print(f"Published {tag}: {', '.join(sorted(bundles))}")


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    event_run = event["workflow_run"]
    run = api(repo, f"actions/runs/{int(event_run['id'])}")
    validate_run(run, repo)
    version = gh("api", f"repos/{repo}/contents/CHROMIUM_VERSION?ref={run['head_sha']}",
                 "-H", "Accept: application/vnd.github.raw+json")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        raise ValueError("Invalid Chromium version in the built commit")
    runs = successful_runs(repo, run["head_sha"])
    if set(runs) != set(WORKFLOWS):
        raise ValueError("Both successful browser workflows are required for the same commit")
    # Use the triggering workflow only for provenance; collect both workflow artifacts.
    with tempfile.TemporaryDirectory(prefix="chromix-release-") as directory:
        root = Path(directory)
        bundles = {}
        for successful in runs.values():
            bundles.update(collect(repo, successful, root))
        publish(repo, runs["build-cross-platform"], "v" + version, bundles, root)


if __name__ == "__main__":
    main()
