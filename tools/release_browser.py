#!/usr/bin/env python3
"""Publish verified inner browser ZIPs from successful, same-repository CI runs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

WORKFLOWS = {
    "build-linux-x64": ("chromix-linux-x64",),
    "build-linux-arm64": ("chromix-linux-arm64",),
    "build-macos-x64": ("chromix-mac-x64",),
    "build-macos-arm64": ("chromix-mac-arm64",),
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


def matches_run(run: dict, repo: str, head_sha: str) -> bool:
    return (isinstance(head_sha, str)
            and run.get("head_sha") == head_sha
            and re.fullmatch(r"[0-9a-f]{40}", head_sha) is not None
            and run.get("head_branch") == "main"
            and (run.get("repository") or {}).get("full_name") == repo
            and (run.get("head_repository") or {}).get("full_name") == repo
            and run.get("event") in ("push", "workflow_dispatch")
            and run.get("name") in WORKFLOWS
            and run.get("path") == f".github/workflows/{run.get('name')}.yml")


def run_identity(run: dict) -> tuple[int, int]:
    return int(run["id"]), int(run.get("run_attempt", 1))


def successful_runs(repo: str, head_sha: str) -> dict[str, dict]:
    pages = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/actions/runs?head_sha={head_sha}&per_page=100"))
    latest = {}
    for page in pages:
        for run in page.get("workflow_runs", []):
            if not matches_run(run, repo, head_sha):
                continue
            name = run["name"]
            # Rank runs and rerun attempts before considering their conclusions.
            identity = run_identity(run)
            previous = latest.get(name)
            if (previous is None or identity > run_identity(previous)
                    or (identity == run_identity(previous)
                        and (run.get("status") != "completed" or run.get("conclusion") != "success"))):
                latest[name] = run
    return {name: run for name, run in latest.items()
            if run.get("status") == "completed" and run.get("conclusion") == "success"}


def validate_run(run: dict, repo: str, head_sha: str | None = None) -> tuple[str, ...]:
    if (not matches_run(run, repo, head_sha if head_sha is not None else run.get("head_sha", ""))
            or run.get("status") != "completed" or run.get("conclusion") != "success"):
        raise ValueError("Only successful, same-repository main browser builds at the expected SHA may be released")
    return WORKFLOWS[run["name"]]


def validate_runs(runs: dict[str, dict], repo: str) -> str:
    if set(runs) != set(WORKFLOWS):
        raise ValueError("All five platform workflows are required")
    head_sha = runs[next(iter(WORKFLOWS))]["head_sha"]
    for name, run in runs.items():
        validate_run(run, repo, head_sha)
        if run["name"] != name:
            raise ValueError("Workflow run does not match its platform")
    return head_sha


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
        else:
            raise ValueError(f"Missing checksum: {asset}")
        validate_bundle(files[asset])
        result[asset] = files[asset]
    return result


def validate_release_revision(repo: str, tag: str, head_sha: str, release: dict | None) -> None:
    refs = api(repo, f"git/matching-refs/tags/{tag}")
    ref = next((ref for ref in refs if ref["ref"] == f"refs/tags/{tag}"), None)
    if ref:
        obj = ref["object"]
        seen = set()
        while obj["type"] == "tag":
            if obj["sha"] in seen or len(seen) >= 10:
                raise ValueError("Invalid annotated release tag chain")
            seen.add(obj["sha"])
            obj = api(repo, f"git/tags/{obj['sha']}")["object"]
        if obj["type"] != "commit" or obj["sha"] != head_sha:
            raise ValueError(f"Release tag {tag} does not point to the built commit")
    elif release and (not release.get("draft") or release.get("target_commitish") != head_sha):
        raise ValueError("Existing release has no verifiable tag or draft commit")
    if release:
        target = release.get("target_commitish", "")
        if re.fullmatch(r"[0-9a-fA-F]{40}", target) and target.lower() != head_sha:
            raise ValueError("Existing release targets a different commit")
        commits = re.findall(r"Source commit:\s*`?([0-9a-fA-F]{40})", release.get("body") or "")
        if any(commit.lower() != head_sha for commit in commits):
            raise ValueError("Existing release provenance references a different commit")


def publish(repo: str, runs: dict[str, dict], tag: str, bundles: dict[str, Path], root: Path) -> None:
    head_sha = validate_runs(runs, repo)
    if set(bundles) != ASSETS:
        raise ValueError("All five verified browser bundles are required before publishing")
    releases = json.loads(gh("api", "--paginate", "--slurp", f"repos/{repo}/releases?per_page=100"))
    release = next((r for page in releases for r in page if r["tag_name"] == tag), None)
    validate_release_revision(repo, tag, head_sha, release)
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
    # Recheck after all downloads, including existing release assets, before any write to GitHub.
    current = successful_runs(repo, head_sha)
    if (set(current) != set(WORKFLOWS)
            or any(run_identity(current[name]) != run_identity(runs[name]) for name in WORKFLOWS)):
        print(f"Pending release for {head_sha}: platform runs changed during artifact verification")
        return
    manifest = root / "SHA256SUMS"
    manifest.write_text("".join(f"{hashes[name]}  {name}\n" for name in sorted(hashes)), encoding="ascii")
    notes = root / "notes.md"
    body = (release.get("body") or "") if release else (
        f"Chromix {tag[1:]} browser bundles. All five platforms are verified at one source commit. "
        "Browser assets are ZIP archives; verify downloads against SHA256SUMS.\n\n"
        "macOS bundles are not Developer ID signed or notarized. Gatekeeper may block them."
    )
    for name in WORKFLOWS:
        run = runs[name]
        provenance = (f"\n\nVerified build: {run['html_url']}\n"
                      f"Workflow: {name} (attempt {run.get('run_attempt', 1)})\n"
                      f"Source commit: `{head_sha}`\n"
                      f"Assets: {', '.join(asset + '.zip' for asset in WORKFLOWS[name])}.\n")
        if provenance not in body:
            body += provenance
    notes.write_text(body, encoding="utf-8")
    if not release:
        gh("release", "create", tag, "--repo", repo, "--target", head_sha,
           "--title", f"Chromix {tag[1:]}", "--draft", "--notes-file", str(notes))
    for name, path in sorted(bundles.items()):
        if name not in existing:
            gh("release", "upload", tag, str(path), "--repo", repo)
    gh("release", "upload", tag, str(manifest), "--repo", repo, "--clobber")
    gh("release", "edit", tag, "--repo", repo, "--draft=false", "--notes-file", str(notes))
    print(f"Published {tag}: {', '.join(sorted(bundles))}")


def ready_runs(repo: str, event_run: dict) -> dict[str, dict]:
    validate_run(event_run, repo)
    head_sha = event_run["head_sha"]
    run = api(repo, f"actions/runs/{int(event_run['id'])}")
    if (not matches_run(run, repo, head_sha) or run["name"] != event_run["name"]
            or int(run["id"]) != int(event_run["id"])):
        raise ValueError("Triggering workflow run identity changed")
    if run.get("status") != "completed" or run.get("conclusion") != "success":
        print(f"Pending release for {head_sha}: triggering run is no longer successful")
        return {}
    runs = successful_runs(repo, head_sha)
    if set(runs) != set(WORKFLOWS):
        print(f"Pending release for {head_sha}: waiting for {', '.join(sorted(set(WORKFLOWS) - set(runs)))}")
        return {}
    validate_runs(runs, repo)
    return runs


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check-ready", action="store_true",
                        help="Write readiness to GITHUB_OUTPUT without downloading or publishing artifacts")
    args = parser.parse_args(argv)
    repo = os.environ["GITHUB_REPOSITORY"]
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    runs = ready_runs(repo, event["workflow_run"])
    if args.check_ready:
        ready = "true" if runs else "false"
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            output.write(f"ready={ready}\n")
        print(f"Release readiness: {ready}")
        return
    if not runs:
        return
    head_sha = event["workflow_run"]["head_sha"]
    version = gh("api", f"repos/{repo}/contents/CHROMIUM_VERSION?ref={head_sha}",
                 "-H", "Accept: application/vnd.github.raw+json")
    if not re.fullmatch(r"\d+\.\d+\.\d+\.\d+", version):
        raise ValueError("Invalid Chromium version in the built commit")
    with tempfile.TemporaryDirectory(prefix="chromix-release-") as directory:
        root = Path(directory)
        bundles = {}
        for name in WORKFLOWS:
            bundles.update(collect(repo, runs[name], root))
        publish(repo, runs, "v" + version, bundles, root)


if __name__ == "__main__":
    main()
