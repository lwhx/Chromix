#!/usr/bin/env python3
"""Generate .github/workflows/build-posix-github.yml deterministically.

The generator exists because posix-2..posix-8 must stay byte-identical apart
from stage numbers; editing eight job blocks by hand drifts. Tests in
tools/tests/test_cross_platform_build.py assert the generated file matches a
fresh run of this script.
"""
from pathlib import Path

OUT = Path(".github/workflows/build-posix-github.yml")
STAGES = 8

HEADER = """# GitHub-hosted POSIX (Linux x64/arm64, macOS x64/arm64) build, modeled on
# ungoogled-chromium-portablelinux's prep/build_part_01..10 chain and
# ungoogled-chromium-macos' retrieve-resources/build_job_01..20 chain: a full
# Chromium build exceeds the 6h single-job limit, so posix-1..posix-8 run
# under a self-imposed ~5h budget via `timeout -k`, then snapshot the work
# tree with tar|zstd (mtimes preserved for ninja) uploaded as artifacts; the
# next stage restores and resumes ninja incrementally until it returns 0.
name: build-posix-github

on:
  workflow_call:
    inputs:
      platform:
        required: true
        type: string
      arch:
        required: true
        type: string
      runner:
        required: true
        type: string
      artifact:
        required: true
        type: string
      max-stages:
        required: false
        type: number
        default: 8
      use_upstream_cache:
        required: false
        type: boolean
        default: true
    secrets:
      UPSTREAM_ACTIONS_TOKEN:
        required: false
  workflow_dispatch:

permissions:
  contents: read
  actions: read

concurrency:
  group: build-posix-${{ inputs.platform }}-${{ inputs.arch }}-${{ github.ref }}
  cancel-in-progress: false

env:
  DEPOT_TOOLS_METRICS: '0'
  DEPOT_TOOLS_COLLECT_METRICS: '0'
  CHROMIUM_VERSION: '152.0.7977.82'

jobs:
"""

LINUX_CLEAN = """      - name: Free Linux disk space
        if: runner.os == 'Linux'
        run: |
          sudo rm -rf /usr/local/lib/android /usr/local/.ghcup /usr/lib/jvm \\
            /usr/local/share/boost /usr/share/swift \\
            /usr/lib/dotnet /usr/lib/google-cloud-sdk
          sudo docker system prune -af || true
          df -h

      # Upstream portablelinux pins a Debian Docker image whose packages match
      # Chromium's install-build-deps and whose Go is new enough for Dawn's
      # go.mod toolchain line (go 1.25.0). The preinstalled /usr/local/go is
      # replaced by the pinned version below; arm64 runners download
      # linux-arm64 and x64 runners download linux-amd64, matching upstream's
      # host-architecture Go selection.
      - name: Install Linux build dependencies
        if: runner.os == 'Linux'
        run: |
          set -euo pipefail
          sudo apt-get update
          sudo apt-get install -y \\
            bison clang clang-format cmake curl flex g++ git gperf \\
            libasound2-dev libatk1.0-dev libcups2-dev libdrm-dev libegl1-mesa-dev \\
            libevent-dev libflac-dev libgbm-dev libglib2.0-dev libgtk-3-dev \\
            libjpeg-dev libnss3-dev libopus-dev libpam0g-dev libpci-dev \\
            libpipewire-0.3-dev libpulse-dev libspeechd-dev libudev-dev \\
            libva-dev libvpx-dev libwebp-dev libx11-xcb-dev libxcb-dri3-dev \\
            libxshmfence-dev libxslt1-dev libxss-dev libxtst-dev mesa-common-dev \\
            ninja-build pkg-config python3-jinja2 python3-pyparsing \\
            python3-setuptools python3-six rsync uuid-dev xz-utils yasm zip unzip \\
            zstd patch file
          GO_VERSION="$(curl -fsSL "https://go.dev/VERSION?m=text" | head -n1)"
          case "$GO_VERSION" in go1.*) ;; *) echo "unexpected Go version: $GO_VERSION" >&2; exit 1;; esac
          case "${GOARCH:-$(uname -m)}" in
            x86_64|amd64|x64) GO_ARCHIVE_TAIL="amd64" ;;
            aarch64|arm64) GO_ARCHIVE_TAIL="arm64" ;;
            *) echo "unsupported Go host architecture" >&2; exit 1 ;;
          esac
          curl -fsSL "https://go.dev/dl/${GO_VERSION}.linux-${GO_ARCHIVE_TAIL}.tar.gz" -o /tmp/go.tgz
          sudo rm -rf /usr/local/go /opt/hostedtoolcache/go*
          sudo tar -C /usr/local -xzf /tmp/go.tgz
          # arm64 runner images ship no Go on PATH, and a plain `go` here would
          # still miss after the rm above; publish the bin dir to later steps
          # through GITHUB_PATH and verify via the absolute path.
          echo "/usr/local/go/bin" >> "$GITHUB_PATH"
          /usr/local/go/bin/go version
"""

MAC_STEPS = """      - name: Select compatible Xcode
        if: runner.os == 'macOS'
        run: bash build/macos/select-xcode.sh

      - name: Inspect macOS toolchain
        if: runner.os == 'macOS'
        run: |
          {
            sw_vers
            xcode-select -p
            xcodebuild -version
            xcrun --sdk macosx --show-sdk-version
            xcrun --sdk macosx --show-sdk-path
          } 2>&1 | tee "${RUNNER_TEMP}/chromix-logs/xcode.log"

      - name: Disable Spotlight indexing
        if: runner.os == 'macOS'
        run: sudo mdutil -a -i off

      # Upstream macOS CI uses Homebrew for ninja/Go only; clang comes from
      # Chromium's own downloaded LLVM resources per target architecture.
      - name: Install macOS build tools
        if: runner.os == 'macOS'
        run: brew install ninja go coreutils gpatch zstd
"""

NODE_PY = """      - name: Set up Node.js
        uses: actions/setup-node@v4
        with:
          node-version: '24'

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.13'
"""

CACHE_RESTORE = """      # The pinned download cache is deliberately outside the tree snapshot;
      # re-warm it first so a resumed preparation does not redownload archives.
      - name: Restore pinned source downloads
        uses: actions/cache@v4
        with:
          path: ${{ runner.temp }}/chromix-build/download_cache
          key: ${{ runner.os }}-${{ inputs.platform }}-${{ inputs.arch }}-downloads-v2-${{ env.CHROMIUM_VERSION }}-${{ hashFiles('build/ungoogled-revisions.psd1', 'build/prepare-ungoogled.sh') }}
          restore-keys: |
            ${{ runner.os }}-${{ inputs.platform }}-${{ inputs.arch }}-downloads-v2-
"""


def run_step(stage: int) -> str:
    # GHA expressions use ${{ ... }}, so f-strings cannot carry them (their
    # braces would collapse to single braces). Use token replacement instead.
    # For stage 1 no restore argument exists; the continuation chain must stay
    # valid shell, so the token sits inline rather than as a standalone line.
    if stage > 1:
        restore_args = (
            '--from-snapshot "${RUNNER_TEMP}/chromix-restore" '
            "\\\n            "
        )
    else:
        restore_args = ""
    return (
        """      - name: Run stage __STAGE__
        id: stage
        env:
          CHROMIX_USE_UPSTREAM_CACHE: ${{ inputs.use_upstream_cache && '1' || '0' }}
          GH_TOKEN: ${{ secrets.UPSTREAM_ACTIONS_TOKEN || github.token }}
        run: |
          set -euo pipefail
          DEADLINE_EPOCH=$(( $(date +%s) + 300 * 60 ))
          build/posix/ci-stage.sh \\
            --platform '${{ inputs.platform }}' --arch '${{ inputs.arch }}' \\
            --workdir "${RUNNER_TEMP}/chromix-build" \\
            --stage-index __STAGE__ --max-stages '${{ inputs['max-stages'] }}' __RESTORE_ARGS__--deadline-epoch "$DEADLINE_EPOCH" \\
            2>&1 | tee "${RUNNER_TEMP}/chromix-logs/stage-__STAGE__.log"
"""
    ).replace("__STAGE__", str(stage)).replace("__RESTORE_ARGS__", restore_args)

DOWNLOAD_STEP = """      - name: Download tree from previous stage
        if: always()
        uses: actions/download-artifact@v4
        with:
          pattern: ${{ inputs.artifact }}-tree-s%(prev)d-attempt-*-part*
          merge-multiple: true
          path: ${{ runner.temp }}/chromix-restore
"""

SNAPSHOT_ENSURE = """      - name: Verify handoff snapshot
        if: success() && steps.stage.outputs.upload_snapshot == 'true'
        run: |
          SNAP="${RUNNER_TEMP}/chromix-build/.snapshot-stage-%(stage)d"
          test -d "$SNAP/p1"
          find "$SNAP" -type f -name 'tree.tar.zst.*' -print -quit | grep -q .
"""

UPLOAD_PARTS = """      - name: Upload tree part 1
        if: success() && steps.stage.outputs.upload_snapshot == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-tree-s%(stage)d-attempt-${{ github.run_attempt }}-part1
          path: ${{ runner.temp }}/chromix-build/.snapshot-stage-%(stage)d/p1/
          if-no-files-found: warn
          retention-days: 3
          compression-level: 0
      - name: Upload tree part 2
        if: success() && steps.stage.outputs.upload_snapshot == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-tree-s%(stage)d-attempt-${{ github.run_attempt }}-part2
          path: ${{ runner.temp }}/chromix-build/.snapshot-stage-%(stage)d/p2/
          if-no-files-found: warn
          retention-days: 3
          compression-level: 0
      - name: Upload tree part 3
        if: success() && steps.stage.outputs.upload_snapshot == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-tree-s%(stage)d-attempt-${{ github.run_attempt }}-part3
          path: ${{ runner.temp }}/chromix-build/.snapshot-stage-%(stage)d/p3/
          if-no-files-found: warn
          retention-days: 3
          compression-level: 0
      - name: Upload tree part 4
        if: success() && steps.stage.outputs.upload_snapshot == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-tree-s%(stage)d-attempt-${{ github.run_attempt }}-part4
          path: ${{ runner.temp }}/chromix-build/.snapshot-stage-%(stage)d/p4/
          if-no-files-found: warn
          retention-days: 3
          compression-level: 0
"""

FINAL_UPLOADS = """      - name: Upload final bundle
        if: steps.stage.outputs.finished == 'true'
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}
          path: |
            ${{ runner.temp }}/chromix-build/dist/${{ inputs.artifact }}.zip
            ${{ runner.temp }}/chromix-build/dist/SHA256SUMS
          if-no-files-found: error
          retention-days: 14
          compression-level: 0

      - name: Upload build diagnostics
        if: always()
        uses: actions/upload-artifact@v4
        with:
          name: ${{ inputs.artifact }}-logs-s%(stage)d-attempt-${{ github.run_attempt }}
          path: |
            ${{ runner.temp }}/chromix-logs/
            ${{ runner.temp }}/chromix-build/src/out/Chromix/args.gn
            ${{ runner.temp }}/chromix-build/upstream-cache-import.json
            ${{ runner.temp }}/chromix-build/upstream-cache-plan.log
            ${{ runner.temp }}/chromix-build/upstream-object-cache.json
            ${{ runner.temp }}/chromix-upstream/result.json
          if-no-files-found: warn
          retention-days: 14
"""


def job(stage: int) -> str:
    needs = f"posix-{stage - 1}" if stage > 1 else None
    # Job titles also carry GHA expressions; build them without f-strings.
    if stage == 1:
        title = "${{ inputs.platform }}-${{ inputs.arch }} stage 1 (prepare + first compile)"
    else:
        title = (
            "${{ inputs.platform }}-${{ inputs.arch }} stage "
            + str(stage)
            + " (resume compile)"
        )
    parts = [f"  posix-{stage}:\n", f"    name: {title}\n"]
    if needs:
        parts.append(
            "    needs: %s\n    if: >-\n"
            "      always() &&\n"
            "      needs.%s.result == 'success' &&\n"
            "      needs.%s.outputs.finished != 'true'\n" % (needs, needs, needs)
        )
    parts.append("    runs-on: ${{ inputs.runner }}\n")
    parts.append("    timeout-minutes: 355\n")
    parts.append("    outputs:\n")
    parts.append("      finished: ${{ steps.stage.outputs.finished }}\n")
    parts.append("    steps:\n")
    parts.append("      - uses: actions/checkout@v4\n\n")
    parts.append("      - name: Record runner resources\n")
    parts.append("""        run: |
          mkdir -p "${RUNNER_TEMP}/chromix-logs"
          { uname -a; df -h; } 2>&1 | tee "${RUNNER_TEMP}/chromix-logs/runner.log"

""")
    if stage > 1:
        parts.append(DOWNLOAD_STEP % {"prev": stage - 1})
        parts.append("\n")
    parts.append(LINUX_CLEAN)
    parts.append(MAC_STEPS)
    parts.append(NODE_PY)
    parts.append(CACHE_RESTORE)
    parts.append(run_step(stage))
    parts.append(SNAPSHOT_ENSURE % {"stage": stage})
    parts.append("\n")
    parts.append(UPLOAD_PARTS % {"stage": stage})
    parts.append(FINAL_UPLOADS % {"stage": stage})
    return "".join(parts)


body = HEADER + "\n".join(job(s) for s in range(1, STAGES + 1))
OUT.write_text(body)
print(f"wrote {OUT} ({len(body)} bytes, {STAGES} stages)")
