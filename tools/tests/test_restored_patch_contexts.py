"""Strict patch-context regressions for Chromium 152 plus pinned ungoogled core.

The small source fixtures are independent of patch bodies and build caches.
Optional full-series smoke: set CHROMIX_RESTORED_SMOKE_ROOT (containing
macos/upstream and windows/upstream), CHROMIX_RESTORED_SMOKE_CORE and
CHROMIX_RESTORED_SMOKE_WINDOWS to pre-domain-substitution input trees.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import apply_restored_patches as arp  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
PATCHES = {
    "0018": "0018-third_party-blink-renderer-core-frame-screen-cc.patch",
    "0031": "0031-third_party-blink-renderer-platform-graphics-image_data_buffer-cc.patch",
    "0033": "0033-third_party-blink-renderer-core-html-canvas-text_metrics-cc.patch",
    "0047": "0047-third_party-blink-renderer-platform-fonts-font_cache-cc.patch",
}
# Original functional additions, excluding empty lines only; indentation is hashed.
ADDITION_HASHES = {
    "0018": "938125a87df94a17835e9efe3c344f813688ed5f6cdaf6657845a8953c105e1c",
    "0031": "26dbbcb68c4f1aea42809f74bd0da8e95549aead76d9c549d7f9a7365756ee61",
    "0033": "61b5b3ae456468cdf8ee6a901a1014d77dfef2880c9d64588bdd1f0a59980fce",
    "0047": "4692e6aa285951ec94060c8dccbd6945ab9479f84a5121976afe2d90a42df204",
}

# Line numbers and snippets come from pinned pre-Chromix sources, not the diffs.
SOURCE_SECTIONS = {
    "0018": [
        (29, '''#include "third_party/blink/renderer/core/frame/screen.h"

#include "base/numerics/safe_conversions.h"
#include "services/network/public/mojom/permissions_policy/permissions_policy_feature.mojom-blink.h"
#include "third_party/blink/renderer/core/dom/document.h"
#include "third_party/blink/renderer/core/event_target_names.h"
#include "third_party/blink/renderer/core/frame/local_dom_window.h"
'''),
        (41, '''
namespace blink {

Screen::Screen(LocalDOMWindow* window, int64_t display_id)
    : ExecutionContextClient(window), display_id_(display_id) {}

'''),
        (90, '''int Screen::height() const {
  if (!DomWindow())
    return 0;
  return GetRect(/*available=*/false).height();
}

int Screen::width() const {
  if (!DomWindow())
    return 0;
  return GetRect(/*available=*/false).width();
}

'''),
        (109, '''  if (!DomWindow()) {
    return unknown_color_depth;
  }
  return GetScreenInfo().depth == 0
             ? unknown_color_depth
             : base::saturated_cast<unsigned>(GetScreenInfo().depth);
'''),
        (133, '''int Screen::availHeight() const {
  if (!DomWindow())
    return 0;
  return GetRect(/*available=*/true).height();
}

int Screen::availWidth() const {
  if (!DomWindow())
    return 0;
  return GetRect(/*available=*/true).width();
}

'''),
    ],
    "0031": [
        (33, '''#include "third_party/blink/renderer/platform/graphics/image_data_buffer.h"

#include "base/compiler_specific.h"
#include "base/memory/ptr_util.h"
#include "third_party/blink/renderer/platform/image-encoders/image_encoder_utils.h"
#include "third_party/blink/renderer/platform/runtime_enabled_features.h"
#include "third_party/blink/renderer/platform/wtf/text/base64.h"
'''),
        (44, '''
namespace blink {

ImageDataBuffer::ImageDataBuffer(scoped_refptr<StaticBitmapImage> image) {
  if (!image)
    return;
'''),
        (90, '''      return;
    }
    MSAN_CHECK_MEM_IS_INITIALIZED(pixmap_.addr(), pixmap_.computeByteSize());
    retained_image_ = SkImages::RasterFromData(info, std::move(data), rowBytes);
  } else {
    retained_image_ = paint_image.GetSwSkImage();
'''),
    ],
    "0033": [
        (92, '''  Update(font, direction, baseline, align, text, text_painter);
}

void TextMetrics::Shuffle(const double factor) {
  // x-direction
  width_ *= factor;
  actual_bounding_box_left_ *= factor;
  actual_bounding_box_right_ *= factor;

  // y-direction
  font_bounding_box_ascent_ *= factor;
  font_bounding_box_descent_ *= factor;
  actual_bounding_box_ascent_ *= factor;
  actual_bounding_box_descent_ *= factor;
  em_height_ascent_ *= factor;
  em_height_descent_ *= factor;
  baselines_->setAlphabetic(baselines_->alphabetic() * factor);
  baselines_->setHanging(baselines_->hanging() * factor);
  baselines_->setIdeographic(baselines_->ideographic() * factor);
}

void TextMetrics::Update(const Font* font,
                         const TextDirection& direction,
                         const V8CanvasTextBaseline::Enum baseline,
                         const V8CanvasTextAlign::Enum align,
'''),
    ],
    "0047": [
        (44, '''#include "base/timer/elapsed_timer.h"
#include "base/trace_event/process_memory_dump.h"
#include "base/trace_event/trace_event.h"
#include "build/build_config.h"
#include "skia/ext/font_utils.h"
#include "third_party/blink/public/common/features.h"
'''),
        (79, '''
namespace blink {

const char kColorEmojiLocale[] = "und-Zsye";
const char kMonoEmojiLocale[] = "und-Zsym";

'''),
        (155, '''const FontPlatformData* FontCache::GetFontPlatformData(
    const FontDescription& font_description,
    const FontFaceCreationParams& creation_params,
    AlternateFontName alternate_font_name) {
  TRACE_EVENT0("fonts", "FontCache::GetFontPlatformData");

#if !BUILDFLAG(IS_MAC)
  if (creation_params.CreationType() == kCreateFontByFamily &&
      creation_params.Family() == font_family_names::kSystemUi) {
    return SystemFontPlatformData(font_description);
  }
#endif

  return font_platform_data_cache_.GetOrCreateFontPlatformData(
      this, font_description, creation_params, alternate_font_name);
}
'''),
    ],
}


def source_fixture(number):
    lines = []
    for first, text in SOURCE_SECTIONS[number]:
        assert len(lines) < first
        while len(lines) < first - 1:
            lines.append(f"// unrelated source line {len(lines) + 1}\n")
        lines.extend(text.splitlines(keepends=True))
    lines.extend(["// hunk is not at EOF\n", "// trailing source stays intact\n"])
    return "".join(lines).encode("utf-8")


def target_path(data):
    return re.search(rb"^\+\+\+ b/(.*)$", data, re.M)[1].decode()


def apply_patch(src, patch_file, *, reverse=False, dry_run=False):
    if PATCH_BIN is None:
        pytest.skip("GNU patch is required")
    command = [PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--forward", "--binary",
               "--get=0", "--no-backup-if-mismatch", "--reject-file=-", "--input", str(patch_file)]
    if reverse:
        command.append("--reverse")
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(command, cwd=src, env=dict(os.environ, LC_ALL="C", PATCH_GET="0"),
                          stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.STDOUT, check=False)


@pytest.mark.parametrize("number", PATCHES)
def test_functional_additions_are_byte_identical(number):
    data = (REPO / "patches" / PATCHES[number]).read_bytes()
    additions = b"".join(line[1:] for line in data.splitlines(keepends=True)
                         if line.startswith(b"+") and not line.startswith(b"+++") and line[1:].strip())
    assert hashlib.sha256(additions).hexdigest() == ADDITION_HASHES[number]
    assert not any(line.startswith(b"-") and not line.startswith(b"---") for line in data.splitlines())
    # Balanced context avoids GNU patch's asymmetric-hunk EOF restriction.
    for body in re.split(rb"^@@[^\n]*\n", data, flags=re.M)[1:]:
        lines = body.splitlines(keepends=True)
        changed = [i for i, line in enumerate(lines) if line.startswith(b"+")]
        assert changed[0] == 3
        assert len(lines) - changed[-1] - 1 == 3


@pytest.mark.parametrize("number", PATCHES)
def test_actual_patch_applies_without_fuzz_and_reverses_exactly(tmp_path, number):
    patch_file = REPO / "patches" / PATCHES[number]
    data = patch_file.read_bytes()
    path = tmp_path / target_path(data)
    path.parent.mkdir(parents=True)
    original = source_fixture(number)
    path.write_bytes(original)
    before_patch = patch_file.stat().st_mtime_ns
    result = apply_patch(tmp_path, patch_file)
    assert result.returncode == 0, result.stdout.decode()
    assert b"fuzz" not in result.stdout
    effective = path.read_bytes()
    assert effective != original
    assert effective.endswith(b"// hunk is not at EOF\n// trailing source stays intact\n")
    if number == "0033":
        comment = b"// Text metrics remain derived from the actual shaped and rendered font.\n"
        assert effective.replace(comment, b"", 1) == original
        assert comment + b"void TextMetrics::Update" in effective
    elif number == "0047":
        assert b"alternate_font_name != AlternateFontName::kLastResort" in effective
        assert b"!UxrFontFamilyAllowed(creation_params.Family())" in effective
        assert b"return font_platform_data_cache_.GetOrCreateFontPlatformData(" in effective
    result = apply_patch(tmp_path, patch_file, reverse=True)
    assert result.returncode == 0, result.stdout.decode()
    assert path.read_bytes() == original
    assert patch_file.read_bytes() == data
    assert patch_file.stat().st_mtime_ns == before_patch


@pytest.mark.parametrize(("number", "required_context"), [
    ("0018", b'#include "third_party/blink/renderer/core/dom/document.h"'),
    ("0031", b'#include "third_party/blink/renderer/platform/runtime_enabled_features.h"'),
    ("0033", b"  baselines_->setIdeographic(baselines_->ideographic() * factor);"),
    ("0047", b'  TRACE_EVENT0("fonts", "FontCache::GetFontPlatformData");'),
])
def test_wrong_context_is_not_silently_accepted(tmp_path, number, required_context):
    patch_file = REPO / "patches" / PATCHES[number]
    path = tmp_path / target_path(patch_file.read_bytes())
    path.parent.mkdir(parents=True)
    source = source_fixture(number)
    assert source.count(required_context) == 1
    broken = source.replace(required_context, b"// incompatible upstream context", 1)
    path.write_bytes(broken)
    result = apply_patch(tmp_path, patch_file, dry_run=True)
    assert result.returncode != 0
    assert b"FAILED" in result.stdout
    assert path.read_bytes() == broken


@pytest.mark.parametrize("platform", ["macos", "windows"])
def test_small_domain_restored_series(tmp_path, platform):
    if PATCH_BIN is None:
        pytest.skip("GNU patch is required")
    repo, src, core, tooling = (tmp_path / name for name in ("repo", "src", "core", "tooling"))
    for root in (repo, src, core, tooling):
        root.mkdir()
    (repo / "patches").mkdir()
    paths = []
    for number, name in PATCHES.items():
        data = (REPO / "patches" / name).read_bytes()
        (repo / "patches" / name).write_bytes(data)
        target = target_path(data)
        paths.append(target)
        path = src / target
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(source_fixture(number) + b"// blocked.test\n")
    (repo / "patches/series").write_text("".join(f"patches/{name}\n" for name in PATCHES.values()))
    (core / "domain_regex.list").write_bytes(rb"example\.com#blocked.test" + b"\n")
    for root in (core, tooling):
        (root / "domain_substitution.list").write_text("\n".join(paths) + "\n")
    result = arp.run_apply(src, repo, core, tooling, platform, PATCH_BIN)
    assert result["status"] == "applied"
    assert result["patch_count"] == 4
    assert result["changed_files"] == sorted(paths)
    assert all((src / target).read_bytes().endswith(b"// blocked.test\n") for target in paths)
    assert arp.run_apply(src, repo, core, tooling, platform, PATCH_BIN)["status"] == "skipped"
    assert arp.run_apply(src, repo, core, tooling, platform, PATCH_BIN, check=True)["status"] == "checked"


@pytest.mark.parametrize("platform", ["linux", "macos", "windows"])
def test_full_series_on_supplied_sparse_upstream(tmp_path, platform):
    names = ("CHROMIX_RESTORED_SMOKE_ROOT", "CHROMIX_RESTORED_SMOKE_CORE", "CHROMIX_RESTORED_SMOKE_WINDOWS")
    if not all(os.environ.get(name) for name in names):
        pytest.skip("optional pinned sparse upstream inputs are not supplied")
    if PATCH_BIN is None:
        pytest.skip("GNU patch is required")
    root, core, windows = (Path(os.environ[name]).resolve() for name in names)
    baseline = root / platform / "upstream"
    tooling = windows if platform == "windows" else core
    series = [line.split("#", 1)[0].strip() for line in (REPO / "patches/series").read_text().splitlines()]
    series = [name for name in series if name]
    assert len(series) == 110
    targets = set()
    patch_stats = {}
    for name in series:
        patch_file = REPO / name
        data = patch_file.read_bytes()
        patch_stats[name] = (data, patch_file.stat().st_mtime_ns)
        _, entries = arp.transform_patch(data, set(), [])
        targets.update(entry[0] for entry in entries)
    payload_root = REPO / arp.LITE
    payloads = {p.relative_to(payload_root).as_posix(): p.read_bytes()
                for p in payload_root.rglob("*") if p.is_file()}
    targets.update(payloads)
    normal, restored = tmp_path / "normal", tmp_path / "restored"
    normal.mkdir()
    restored.mkdir()
    baseline_stats = {}
    for name in sorted(targets):
        path = baseline / name
        assert not path.is_symlink()
        baseline_stats[name] = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
        if path.exists():
            for dest in (normal, restored):
                target = dest / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(baseline_stats[name][0])
    for name, data in payloads.items():
        for dest in (normal, restored):
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
    for name in series:
        result = apply_patch(normal, REPO / name)
        assert result.returncode == 0, (name, result.stdout.decode())
        assert b"fuzz" not in result.stdout
    rules = arp._rules((core / "domain_regex.list").read_bytes())
    listed = set(((windows if platform == "windows" else core) / "domain_substitution.list").read_text().splitlines())
    for name in targets & listed:
        path = restored / name
        if path.exists():
            text, encoding = arp._decode(path.read_bytes())
            path.write_bytes(arp._substitute(text, rules).encode(encoding))
    result = arp.run_apply(restored, REPO, core, tooling, platform, PATCH_BIN)
    assert result["status"] == "applied"
    assert result["patch_count"] == 110
    assert arp.run_apply(restored, REPO, core, tooling, platform, PATCH_BIN)["status"] == "skipped"
    assert arp.run_apply(restored, REPO, core, tooling, platform, PATCH_BIN, check=True)["status"] == "checked"
    for name in targets:
        data = (normal / name).read_bytes()
        if name in listed:
            text, encoding = arp._decode(data)
            data = arp._substitute(text, rules).encode(encoding)
        assert (restored / name).read_bytes() == data, name
    for name, state in baseline_stats.items():
        path = baseline / name
        actual = (path.read_bytes(), path.stat().st_mtime_ns) if path.exists() else None
        assert actual == state
    for name, state in patch_stats.items():
        path = REPO / name
        assert (path.read_bytes(), path.stat().st_mtime_ns) == state
