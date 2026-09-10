"""Codec regressions against independent Chromium 152.0.7977.82 source excerpts.

The actual 0060 patch is applied before its helpers are compiled, unchanged,
with config/string/binding stubs. Callback wiring is checked statically, not
executed through Blink/Mojo. CHROMIX_CODECS_BASELINE_ROOT optionally verifies
provenance and strict application against a local full pre-Chromix source.
No downloads, browser builds, or writes to the supplied baseline are performed.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/0060-third_party-blink-renderer-modules-media_capabilities-media_capabilities-cc.patch"
TARGET = Path("third_party/blink/renderer/modules/media_capabilities/media_capabilities.cc")
PATCH_BIN = shutil.which("gpatch") or shutil.which("patch")
CXX = shutil.which("clang++") or shutil.which("g++")
BASELINE_SHA256 = "f71c600e526d2788dc085e3fac36352059984c85d91276b7e92c3ccd52d57056"
FAMILIES = ("h264", "vp8", "vp9", "av1", "hevc")
TOKENS = ("supported", "smooth", "power-efficient")
ALL_CAPABILITIES = ",".join(TOKENS)


def source_fixture():
    lines = []
    for first, text in SOURCE_SECTIONS:
        assert len(lines) < first
        lines.extend("// unrelated Chromium source\n" for _ in range(first - 1 - len(lines)))
        lines.extend(text.splitlines(keepends=True))
    lines.append("// trailing Chromium source\n")
    return "".join(lines)


def apply_patch(directory, *, reverse=False, dry_run=False):
    if PATCH_BIN is None:
        pytest.skip("GNU patch is required")
    command = [PATCH_BIN, "-p1", "--fuzz=0", "--batch", "--binary", "--get=0",
               "--no-backup-if-mismatch", "--reject-file=-", "--input", str(PATCH),
               "--reverse" if reverse else "--forward"]
    if dry_run:
        command.append("--dry-run")
    return subprocess.run(command, cwd=directory, text=True, capture_output=True,
                          stdin=subprocess.DEVNULL, timeout=15,
                          env={**os.environ, "LC_ALL": "C", "PATCH_GET": "0"})


def assert_strict(result):
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    assert not re.search(r"offset|fuzz|FAILED|Reversed|Skipping", output, re.I), output


def apply_and_reverse(directory, original):
    target = directory / TARGET
    target.parent.mkdir(parents=True)
    target.write_bytes(original)
    assert_strict(apply_patch(directory, dry_run=True))
    assert target.read_bytes() == original
    assert_strict(apply_patch(directory))
    patched = target.read_bytes()
    assert patched != original
    assert_strict(apply_patch(directory, reverse=True, dry_run=True))
    assert target.read_bytes() == patched
    assert_strict(apply_patch(directory, reverse=True))
    assert target.read_bytes() == original
    return patched.decode()


@pytest.fixture(scope="module")
def patched_source(tmp_path_factory):
    directory = tmp_path_factory.mktemp("codec-source")
    return apply_and_reverse(directory, source_fixture().encode())


def helpers(source):
    start = source.index("struct UxrCodecCapabilities {")
    end = source.index("// static\nbool UseGpuFactoriesForPowerEfficient(", start)
    return source[start:end]


def method(source, name):
    start = source.index(f"void MediaCapabilities::{name}(")
    return source[start:source.index("\n}\n", start) + 3]


def test_patch_applies_without_fuzz_or_offset_and_reverses(patched_source):
    assert "GetUxrCodecCapabilities(" in helpers(patched_source)
    assert "ApplyUxrCodecCapabilities(" in helpers(patched_source)
    assert "UxrCodecCapabilities" not in source_fixture()


@pytest.mark.parametrize("context", [
    '#include "base/numerics/safe_conversions.h"',
    '      kWebrtcEncodeSmoothIfPowerEfficientDefault);',
    '  if (!EnsurePerfHistoryService(execution_context)) {',
    '          resolver, access, request_time));',
    '  info->setSmooth(*pending_cb->db_is_smooth);',
    '    bool is_power_efficient) {',
    '    info->setPowerEfficient(is_power_efficient);',
    '  info->setSmooth(is_smooth);',
])
def test_incompatible_patch_context_is_rejected(tmp_path, context):
    original = source_fixture()
    context = f"\n{context}\n"
    assert original.count(context) == 1
    original = original.replace(context, "\n// incompatible Chromium context\n")
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True)
    target.write_text(original)
    result = apply_patch(tmp_path, dry_run=True)
    assert result.returncode != 0
    assert "FAILED" in result.stdout + result.stderr
    assert target.read_text() == original


def test_strict_guard_rejects_offset_application(tmp_path):
    target = tmp_path / TARGET
    target.parent.mkdir(parents=True)
    original = "// unexpected source shift\n" + source_fixture()
    target.write_text(original)
    result = apply_patch(tmp_path, dry_run=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "offset" in result.stdout + result.stderr
    with pytest.raises(AssertionError):
        assert_strict(result)
    assert target.read_text() == original


def test_optional_local_baseline_provenance_and_round_trip(tmp_path, patched_source):
    baseline = os.environ.get("CHROMIX_CODECS_BASELINE_ROOT")
    if not baseline:
        pytest.skip("set CHROMIX_CODECS_BASELINE_ROOT to a local pre-Chromix Chromium 152 tree")
    path = Path(baseline) / TARGET
    original = path.read_bytes()
    before = path.stat()
    assert hashlib.sha256(original).hexdigest() == BASELINE_SHA256
    lines = original.decode().splitlines(keepends=True)
    for first, text in SOURCE_SECTIONS:
        assert "".join(lines[first - 1:first - 1 + len(text.splitlines())]) == text
    patched = apply_and_reverse(tmp_path, original)
    assert helpers(patched) == helpers(patched_source)
    for name in CALLBACKS:
        assert method(patched, name) == method(patched_source, name)
    assert path.read_bytes() == original
    assert path.stat().st_mtime_ns == before.st_mtime_ns


CALLBACKS = ("GetPerfInfo", "ResolveCallbackIfReady", "OnPerfHistoryInfo",
             "OnGpuFactoriesSupport", "OnWebrtcSupportInfo", "OnWebrtcPerfHistoryInfo")
CALL = "ApplyUxrCodecCapabilities(pending_cb->video_codec, info);"


@pytest.mark.parametrize("name", CALLBACKS)
def test_all_callback_routes_preserve_native_control_flow(patched_source, name):
    actual = method(patched_source, name)
    original = method(source_fixture(), name)
    if name in ("OnPerfHistoryInfo", "OnGpuFactoriesSupport"):
        assert "ResolveCallbackIfReady(callback_id);" in actual
        assert actual == original
        return
    call = "ApplyUxrCodecCapabilities(video_codec, info);" if name == "GetPerfInfo" else CALL
    assert actual.count(call) == 1
    if name == "GetPerfInfo":
        assert actual.index("if (!EnsurePerfHistoryService(") < actual.index(call)
        assert actual.index(call) < actual.index("resolver->Resolve(WrapPersistent(info));")
        assignment = "  pending_cb_map_.at(callback_id)->video_codec = video_codec;\n"
        assert actual.count(assignment) == 1
        assert actual.index(assignment) < actual.index("decode_history_service_->GetPerfInfo(")
        actual = actual.replace(assignment, "")
    else:
        for setter in re.finditer(r"info->set(?:Supported|Smooth|PowerEfficient)\(", actual):
            assert setter.start() < actual.index(call)
        for resolve in re.finditer(r"->Resolve\(", actual):
            assert actual.index(call) < resolve.start()
        if name == "OnWebrtcSupportInfo":
            assignment = ("  pending_cb->video_codec =\n"
                          "      media::VideoCodecProfileToVideoCodec(features->profile);\n")
            assert actual.count(assignment) == 1
            assert actual.index(assignment) < actual.index("if (!is_supported")
            assert "BindOnce(&MediaCapabilities::OnWebrtcPerfHistoryInfo," in actual
            actual = actual.replace(assignment, "")
        if name.startswith("OnWebrtc"):
            assert actual.index(call) < actual.index("if (type == OperationType::kEncoding)")
            assert "DowncastTo<MediaCapabilitiesInfo>()" in actual
            assert "DowncastTo<MediaCapabilitiesDecodingInfo>()" in actual
    actual = re.sub(r"^ +" + re.escape(call) + r"\n", "", actual, flags=re.M)
    assert actual == original


def test_helper_has_exactly_four_callback_call_sites(patched_source):
    assert patched_source.count("ApplyUxrCodecCapabilities(") == 5


@pytest.fixture(scope="module")
def runtime_binary(tmp_path_factory, patched_source):
    if CXX is None:
        pytest.skip("a local C++20 compiler is required for the standalone codec harness")
    directory = tmp_path_factory.mktemp("codec-runtime")
    source = directory / "codecs.cc"
    source.write_text(CPP_SUPPORT + "\n" + helpers(patched_source) + "\n" + CPP_MAIN)
    binary = directory / "codecs"
    result = subprocess.run([CXX, "-std=c++20", "-O0", "-Wall", "-Wextra", "-Werror",
                             str(source), "-o", str(binary)], text=True,
                            capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


def assert_codec_matrix(binary, family, config, capabilities):
    result = subprocess.run([str(binary), family,
                             *(f"{key}={value}" for key, value in config.items())],
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
    rows = [tuple(map(int, line.split())) for line in result.stdout.splitlines()]
    assert len(rows) == 8
    for native, row in enumerate(rows):
        active = capabilities is not None
        parsed = capabilities if active else native
        expected = native
        if active:
            expected = native & capabilities if native & capabilities & 1 else 0
        assert row == (native, int(active), parsed, expected, expected,
                       3 if active else 0, 3 if active else 0), (family, config, row)
        assert not (row[3] & ~native), (family, config, row)
        assert not (row[4] & ~native), (family, config, row)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("configured", range(8))
def test_complete_native_and_config_boolean_matrix(runtime_binary, family, configured):
    raw = ",".join(token for bit, token in enumerate(TOKENS) if configured & (1 << bit))
    parsed = configured if configured & 1 else 0
    assert_codec_matrix(runtime_binary, family, {f"uxr-codec-{family}": raw}, parsed)


@pytest.mark.parametrize("family", FAMILIES)
def test_missing_configuration_is_a_write_free_native_passthrough(runtime_binary, family):
    assert_codec_matrix(runtime_binary, family, {}, None)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("raw,parsed", [
    ("", 0), (",,,", 0), (" \t\r\n", 0), ("invalid", 0), ("true", 0),
    ("false", 0), ("0", 0), ("1", 0), ("Supported,Smooth,Power-Efficient", 0),
    ("unsupported,not-smooth,powerEfficient", 0),
    (" supported,smooth,power-efficient", 0),
    ("supported, smooth,power-efficient", 5),
    ("supported,smooth ,power-efficient ", 1),
    ("supported,\tsmooth,\npower-efficient", 1),
    ("supported;smooth;power-efficient", 0),
    ("smooth,power-efficient", 0),
    ("supported,invalid,smooth,power-efficient", 7),
    ("supported,supported,,smooth,,power-efficient,", 7),
    ("power-efficient,smooth,supported", 7),
])
def test_exact_token_empty_and_invalid_semantics(runtime_binary, family, raw, parsed):
    assert_codec_matrix(runtime_binary, family, {f"uxr-codec-{family}": raw}, parsed)


@pytest.mark.parametrize("family", FAMILIES)
@pytest.mark.parametrize("configured_family", FAMILIES)
def test_codec_family_keys_are_isolated(runtime_binary, family, configured_family):
    for raw, parsed in (("", 0), (ALL_CAPABILITIES, 7)):
        expected = parsed if family == configured_family else None
        assert_codec_matrix(runtime_binary, family,
                            {f"uxr-codec-{configured_family}": raw}, expected)


@pytest.mark.parametrize("family", FAMILIES)
def test_simultaneous_family_settings_do_not_leak(runtime_binary, family):
    masks = dict(zip(FAMILIES, (0, 1, 3, 5, 7)))
    config = {f"uxr-codec-{name}": ",".join(
        token for bit, token in enumerate(TOKENS) if mask & (1 << bit))
        for name, mask in masks.items()}
    assert_codec_matrix(runtime_binary, family, config, masks[family])


@pytest.mark.parametrize("family", FAMILIES)
def test_unrecognized_keys_cannot_enable_codec_configuration(runtime_binary, family):
    config = {key: ALL_CAPABILITIES for key in (
        "uxr-codec", "uxr-codec-h265", "uxr-codec-H264", "uxr-codec-VP9",
        "uxr-codec-avc1", "uxr-codec-av01", "uxr-codec-vp09", "uxr-codec-hev1",
        "uxr-codec-hvc1", "uxr-codec-h264-profile", "uxr-fingerprint-seed")}
    assert_codec_matrix(runtime_binary, family, config, None)


@pytest.mark.parametrize("family", ("unknown", "theora"))
@pytest.mark.parametrize("raw", ("", "invalid", ALL_CAPABILITIES))
def test_unmapped_codec_and_audio_sentinel_remain_native(runtime_binary, family, raw):
    config = {f"uxr-codec-{name}": raw for name in (*FAMILIES, "unknown", "theora")}
    assert_codec_matrix(runtime_binary, family, config, None)


CPP_SUPPORT = r'''
#include <cassert>
#include <iostream>
#include <map>
#include <string>
#include <vector>

namespace media {
enum class VideoCodec { kUnknown, kH264, kVP8, kVP9, kAV1, kHEVC, kTheora };
}
namespace base {
struct UxrConfig {
  std::map<std::string, std::string> values;
  static UxrConfig& GetInstance() { static UxrConfig config; return config; }
  bool Has(const std::string& key) const { return values.contains(key); }
  std::string Get(const std::string& key) const {
    auto it = values.find(key);
    return it == values.end() ? std::string() : it->second;
  }
};
enum WhitespaceHandling { KEEP_WHITESPACE, TRIM_WHITESPACE };
enum SplitResult { SPLIT_WANT_ALL, SPLIT_WANT_NONEMPTY };
std::vector<std::string> SplitString(const std::string& input,
                                     const std::string& separators,
                                     WhitespaceHandling whitespace,
                                     SplitResult result) {
  assert(separators == "," && whitespace == KEEP_WHITESPACE);
  assert(result == SPLIT_WANT_NONEMPTY);
  std::vector<std::string> tokens;
  size_t start = 0;
  while (start < input.size()) {
    size_t end = input.find_first_of(separators, start);
    if (end == std::string::npos) end = input.size();
    if (end != start) tokens.push_back(input.substr(start, end - start));
    start = end + 1;
  }
  return tokens;
}
}
class MediaCapabilitiesInfo {
 public:
  explicit MediaCapabilitiesInfo(int mask)
      : supported_(mask & 1), smooth_(mask & 2), power_efficient_(mask & 4) {}
  bool supported() const { return supported_; }
  bool smooth() const { return smooth_; }
  bool powerEfficient() const { return power_efficient_; }
  void setSupported(bool value) { supported_ = value; ++writes; }
  void setSmooth(bool value) { smooth_ = value; ++writes; }
  void setPowerEfficient(bool value) { power_efficient_ = value; ++writes; }
  int mask() const { return supported_ | (smooth_ << 1) | (power_efficient_ << 2); }
  int writes = 0;
 private:
  bool supported_, smooth_, power_efficient_;
};
class MediaCapabilitiesDecodingInfo : public MediaCapabilitiesInfo {
 public:
  using MediaCapabilitiesInfo::MediaCapabilitiesInfo;
  const void* key_system_access = nullptr;
};
'''


CPP_MAIN = r'''
int main(int argc, char** argv) {
  assert(argc >= 2);
  const std::map<std::string, media::VideoCodec> codecs = {
      {"h264", media::VideoCodec::kH264}, {"vp8", media::VideoCodec::kVP8},
      {"vp9", media::VideoCodec::kVP9}, {"av1", media::VideoCodec::kAV1},
      {"hevc", media::VideoCodec::kHEVC}, {"unknown", media::VideoCodec::kUnknown},
      {"theora", media::VideoCodec::kTheora}};
  const auto codec = codecs.at(argv[1]);
  auto& config = base::UxrConfig::GetInstance();
  for (int i = 2; i < argc; ++i) {
    const std::string arg = argv[i];
    const size_t equals = arg.find('=');
    assert(equals != std::string::npos);
    config.values.emplace(arg.substr(0, equals), arg.substr(equals + 1));
  }
  const auto original_config = config.values;
  for (int native = 0; native < 8; ++native) {
    UxrCodecCapabilities parsed;
    parsed.supported = native & 1;
    parsed.smooth = native & 2;
    parsed.power_efficient = native & 4;
    const bool active = GetUxrCodecCapabilities(codec, &parsed);
    const int parsed_mask = parsed.supported | (parsed.smooth << 1) |
                            (parsed.power_efficient << 2);
    MediaCapabilitiesInfo encoding(native);
    MediaCapabilitiesDecodingInfo decoding(native);
    decoding.key_system_access = &original_config;
    ApplyUxrCodecCapabilities(codec, &encoding);
    ApplyUxrCodecCapabilities(codec, &decoding);
    std::cout << native << ' ' << active << ' ' << parsed_mask << ' '
              << encoding.mask() << ' ' << decoding.mask() << ' '
              << encoding.writes << ' ' << decoding.writes << '\n';
    const int encoding_mask = encoding.mask(), decoding_mask = decoding.mask();
    ApplyUxrCodecCapabilities(codec, &encoding);
    ApplyUxrCodecCapabilities(codec, &decoding);
    assert(encoding.mask() == encoding_mask && decoding.mask() == decoding_mask);
    assert(decoding.key_system_access == &original_config);
    assert(config.values == original_config);
  }
}
'''


# Frozen pre-Chromix excerpts include complete callbacks, independent of patch bodies.
SOURCE_SECTIONS = [
    (13, r'''#include "base/metrics/field_trial_params.h"
#include "base/metrics/histogram_macros.h"
#include "base/numerics/safe_conversions.h"
#include "base/task/single_thread_task_runner.h"
#include "media/base/media_switches.h"
#include "media/base/media_util.h"
#include "media/base/mime_util.h"
'''),
    (103, r'''// static
bool WebrtcEncodeForceSmoothIfPowerEfficient() {
  return base::GetFieldTrialParamByFeatureAsBool(
      media::kWebrtcMediaCapabilitiesParameters,
      MediaCapabilities::kWebrtcEncodeSmoothIfPowerEfficientParamName,
      kWebrtcEncodeSmoothIfPowerEfficientDefault);
}

// static
bool UseGpuFactoriesForPowerEfficient(
    ExecutionContext* execution_context,
    const MediaKeySystemAccess* key_system_access) {
'''),
    (1335, r'''void MediaCapabilities::GetPerfInfo(
    media::VideoCodec video_codec,
    media::VideoCodecProfile video_profile,
    media::VideoColorSpace video_color_space,
    const MediaDecodingConfiguration* decoding_config,
    const base::TimeTicks& request_time,
    ScriptPromiseResolver<MediaCapabilitiesDecodingInfo>* resolver,
    MediaKeySystemAccess* access) {
  ExecutionContext* execution_context = resolver->GetExecutionContext();
  if (!execution_context || execution_context->IsContextDestroyed())
    return;

  if (!decoding_config->hasVideo()) {
    // Audio-only is always smooth and power efficient.
    MediaCapabilitiesDecodingInfo* info = CreateDecodingInfoWith(true);
    info->setKeySystemAccess(access);
    resolver->Resolve(info);
    return;
  }

  const VideoConfiguration* video_config = decoding_config->video();
  String key_system = "";
  bool use_hw_secure_codecs = false;

  if (access) {
    // Use the internal/base key system to keep the perf database simple, e.g.
    // different key system names may share the same internal/base key system
    // and the same CDM implementation, and hence the same performance.
    key_system = access->GetInternalKeySystem();
    use_hw_secure_codecs = access->UseHardwareSecureCodecs();
  }

  if (!EnsurePerfHistoryService(execution_context)) {
    MediaCapabilitiesDecodingInfo* info = CreateDecodingInfoWith(true);
    resolver->Resolve(WrapPersistent(info));
    return;
  }

  const int callback_id = CreateCallbackId();
  pending_cb_map_.insert(
      callback_id,
      MakeGarbageCollected<MediaCapabilities::PendingCallbackState>(
          resolver, access, request_time));

  media::mojom::blink::PredictionFeaturesPtr features =
      media::mojom::blink::PredictionFeatures::New(
          video_profile,
          gfx::Size(video_config->width(), video_config->height()),
          video_config->framerate(), key_system, use_hw_secure_codecs);

  decode_history_service_->GetPerfInfo(
      std::move(features), BindOnce(&MediaCapabilities::OnPerfHistoryInfo,
                                    WrapPersistent(this), callback_id));

  if (UseGpuFactoriesForPowerEfficient(execution_context, access)) {
    GetGpuFactoriesSupport(callback_id, video_codec, video_profile,
                           video_color_space, decoding_config);
  }
}
'''),
    (1466, r'''void MediaCapabilities::ResolveCallbackIfReady(int callback_id) {
  DCHECK(pending_cb_map_.Contains(callback_id));
  PendingCallbackState* pending_cb = pending_cb_map_.at(callback_id);
  ExecutionContext* execution_context =
      pending_cb_map_.at(callback_id)->resolver->GetExecutionContext();

  if (!pending_cb->db_is_power_efficient.has_value())
    return;

  // Both db_* fields should be set simultaneously by the DB callback.
  DCHECK(pending_cb->db_is_smooth.has_value());

  if (UseGpuFactoriesForPowerEfficient(execution_context,
                                       pending_cb->key_system_access) &&
      !pending_cb->is_gpu_factories_supported.has_value()) {
    return;
  }

  if (!pending_cb->resolver->GetExecutionContext() ||
      pending_cb->resolver->GetExecutionContext()->IsContextDestroyed()) {
    // We're too late! Now that all the callbacks have provided state, its safe
    // to erase the entry in the map.
    pending_cb_map_.erase(callback_id);
    return;
  }

  auto* info = MediaCapabilitiesDecodingInfo::Create();
  info->setSupported(true);
  info->setKeySystemAccess(pending_cb->key_system_access);

  if (UseGpuFactoriesForPowerEfficient(execution_context,
                                       pending_cb->key_system_access)) {
    info->setPowerEfficient(*pending_cb->is_gpu_factories_supported);
    // Builtin video codec guarantee a certain codec can be decoded under any
    // circumstances, and if the result is not powerEfficient and the video
    // codec is not builtin, that means the video will failed to play at the
    // given video config, so change the supported value to false here.
    if (!info->powerEfficient() &&
        !pending_cb->is_builtin_video_codec.value_or(true)) {
      info->setSupported(false);
    }
  } else {
    info->setPowerEfficient(*pending_cb->db_is_power_efficient);
  }

  info->setSmooth(*pending_cb->db_is_smooth);

  const base::TimeDelta process_time =
      base::TimeTicks::Now() - pending_cb->request_time;
  UMA_HISTOGRAM_TIMES("Media.Capabilities.DecodingInfo.Time.Video",
                      process_time);

  // Record another time in the appropriate subset, either clear or encrypted
  // content.
  if (pending_cb->key_system_access) {
    UMA_HISTOGRAM_TIMES("Media.Capabilities.DecodingInfo.Time.Video.Encrypted",
                        process_time);
  } else {
    UMA_HISTOGRAM_TIMES("Media.Capabilities.DecodingInfo.Time.Video.Clear",
                        process_time);
  }

  pending_cb->resolver->DowncastTo<MediaCapabilitiesDecodingInfo>()->Resolve(
      std::move(info));
  pending_cb_map_.erase(callback_id);
}

void MediaCapabilities::OnPerfHistoryInfo(int callback_id,
                                          bool is_smooth,
                                          bool is_power_efficient) {
  DCHECK(pending_cb_map_.Contains(callback_id));
  PendingCallbackState* pending_cb = pending_cb_map_.at(callback_id);

  pending_cb->db_is_smooth = is_smooth;
  pending_cb->db_is_power_efficient = is_power_efficient;

  ResolveCallbackIfReady(callback_id);
}

void MediaCapabilities::OnGpuFactoriesSupport(int callback_id,
                                              bool is_supported,
                                              media::VideoCodec video_codec) {
  DVLOG(2) << __func__ << " video_codec:" << video_codec
           << ", is_supported:" << is_supported;
  DCHECK(pending_cb_map_.Contains(callback_id));
  PendingCallbackState* pending_cb = pending_cb_map_.at(callback_id);

  pending_cb->is_gpu_factories_supported = is_supported;
  pending_cb->is_builtin_video_codec =
      media::IsDecoderBuiltInVideoCodec(video_codec);

  ResolveCallbackIfReady(callback_id);
}

void MediaCapabilities::OnWebrtcSupportInfo(
    int callback_id,
    media::mojom::blink::WebrtcPredictionFeaturesPtr features,
    float frames_per_second,
    OperationType type,
    bool is_supported,
    bool is_power_efficient) {
  DCHECK(pending_cb_map_.Contains(callback_id));
  PendingCallbackState* pending_cb = pending_cb_map_.at(callback_id);

  // Special treatment if the config is not supported, or if only audio was
  // specified which is indicated by the fact that `video_pixels` equals 0,
  // or if we fail to access the WebrtcPerfHistoryService.
  // If enabled through default setting or field trial, we also set
  // smooth=true if the configuration is power efficient.
  if (!is_supported || features->video_pixels == 0 ||
      !EnsureWebrtcPerfHistoryService(
          pending_cb->resolver->GetExecutionContext()) ||
      (is_power_efficient && features->is_decode_stats &&
       WebrtcDecodeForceSmoothIfPowerEfficient()) ||
      (is_power_efficient && !features->is_decode_stats &&
       WebrtcEncodeForceSmoothIfPowerEfficient())) {
    MediaCapabilitiesDecodingInfo* info =
        MediaCapabilitiesDecodingInfo::Create();
    info->setSupported(is_supported);
    info->setSmooth(is_supported);
    info->setPowerEfficient(is_power_efficient);
    if (type == OperationType::kEncoding) {
      pending_cb->resolver->DowncastTo<MediaCapabilitiesInfo>()->Resolve(info);
    } else {
      pending_cb->resolver->DowncastTo<MediaCapabilitiesDecodingInfo>()
          ->Resolve(info);
    }
    pending_cb_map_.erase(callback_id);
    return;
  }

  pending_cb->is_supported = is_supported;
  pending_cb->is_gpu_factories_supported = is_power_efficient;

  features->hardware_accelerated = is_power_efficient;

  webrtc_history_service_->GetPerfInfo(
      std::move(features), frames_per_second,
      BindOnce(&MediaCapabilities::OnWebrtcPerfHistoryInfo,
               WrapPersistent(this), callback_id, type));
}

void MediaCapabilities::OnWebrtcPerfHistoryInfo(int callback_id,
                                                OperationType type,
                                                bool is_smooth) {
  DCHECK(pending_cb_map_.Contains(callback_id));
  PendingCallbackState* pending_cb = pending_cb_map_.at(callback_id);

  // supported and gpu factories supported are set simultaneously.
  DCHECK(pending_cb->is_supported.has_value());
  DCHECK(pending_cb->is_gpu_factories_supported.has_value());

  if (!pending_cb->resolver->GetExecutionContext() ||
      pending_cb->resolver->GetExecutionContext()->IsContextDestroyed()) {
    // We're too late! Now that all the callbacks have provided state, its safe
    // to erase the entry in the map.
    pending_cb_map_.erase(callback_id);
    return;
  }

  auto* info = MediaCapabilitiesDecodingInfo::Create();
  info->setSupported(*pending_cb->is_supported);
  info->setPowerEfficient(*pending_cb->is_gpu_factories_supported);
  info->setSmooth(is_smooth);

  const base::TimeDelta process_time =
      base::TimeTicks::Now() - pending_cb->request_time;
  UMA_HISTOGRAM_TIMES("Media.Capabilities.DecodingInfo.Time.Webrtc",
                      process_time);

  if (type == OperationType::kEncoding) {
    pending_cb->resolver->DowncastTo<MediaCapabilitiesInfo>()->Resolve(info);
  } else {
    pending_cb->resolver->DowncastTo<MediaCapabilitiesDecodingInfo>()->Resolve(
        info);
  }
  pending_cb_map_.erase(callback_id);
}
'''),
]
