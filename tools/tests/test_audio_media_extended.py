"""Media recording/callback contracts on pinned Chromium 152 excerpts.

Only temporary copies and standalone C++ interface stubs are executed. Optional
CHROMIX_AUDIO_MEDIA_EXTENDED_BASELINE_ROOT checks full local pre-Chromix files;
no browser, encoder, Mojo service or audio DSP graph is run or downloaded.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import subprocess

import pytest

import test_fingerprint_codecs as codecs
import test_fingerprint_media_audio as audio

ROOT = Path(__file__).resolve().parents[2]
RECORDER = "third_party/blink/renderer/modules/mediarecorder/media_recorder.cc"
RECORDER_SHA256 = "8226f857e25b9b850dce336b259f7160b4046c25116527daef3bc090fcf75074"

# Independent pre-Chromix 152.0.7977.82 excerpts, including complete methods.
RECORDER_SECTIONS = [(261, r'''void MediaRecorder::start(ExceptionState& exception_state) {
  start(std::numeric_limits<int>::max() /* timeSlice */, exception_state);
}

void MediaRecorder::start(int time_slice, ExceptionState& exception_state) {
  if (!GetExecutionContext() || GetExecutionContext()->IsContextDestroyed()) {
    exception_state.ThrowDOMException(DOMExceptionCode::kNotSupportedError,
                                      "Execution context is detached.");
    return;
  }
  if (state_ != State::kInactive) {
    exception_state.ThrowDOMException(DOMExceptionCode::kInvalidStateError,
                                      StrCat({"The MediaRecorder's state is '",
                                              state().AsStringView(), "'."}));
    return;
  }

  if (stream_->getTracks().size() == 0) {
    exception_state.ThrowDOMException(DOMExceptionCode::kNotSupportedError,
                                      "The MediaRecorder cannot start because"
                                      "there are no audio or video tracks "
                                      "available.");
    return;
  }

  state_ = State::kRecording;

  if (stream_->getAudioTracks().size() == 0) {
    audio_bits_per_second_ = 0;
    if (overall_bits_per_second_.has_value()) {
      video_bits_per_second_ = ClampVideoBitRate(
          GetExecutionContext(), overall_bits_per_second_.value());
    }
  }

  if (stream_->getVideoTracks().size() == 0) {
    video_bits_per_second_ = 0;
    if (overall_bits_per_second_.has_value()) {
      audio_bits_per_second_ = ClampAudioBitRate(
          GetExecutionContext(), overall_bits_per_second_.value());
    }
  }

  const ContentType content_type(mime_type_);
  if (!recorder_handler_->Start(time_slice, content_type.GetType(),
                                audio_bits_per_second_,
                                video_bits_per_second_)) {
    exception_state.ThrowDOMException(
        DOMExceptionCode::kNotSupportedError,
        "There was an error starting the MediaRecorder.");
  }
}
'''), (387, r'''  WriteData(/*data=*/{}, /*last_in_slice=*/true, /*error_event=*/nullptr);
}

bool MediaRecorder::isTypeSupported(ExecutionContext* context,
                                    const String& type) {
  MediaRecorderHandler* handler = MakeGarbageCollected<MediaRecorderHandler>(
      context->GetTaskRunner(TaskType::kInternalMediaRealTime),
      KeyFrameRequestProcessor::Configuration());
  if (!handler)
    return false;

  // If true is returned from this method, it only indicates that the
  // MediaRecorder implementation is capable of recording Blob objects for the
  // specified MIME type. Recording may still fail if sufficient resources are
  // not available to support the concrete media encoding.
  // https://w3c.github.io/mediacapture-record/#dom-mediarecorder-istypesupported
  ContentType content_type(type);
  bool result = handler->CanSupportMimeType(
      content_type.GetType(), content_type.Parameter("codecs"),
      MediaRecorderHandler::CanSupportMimeTypeCaller::kIsTypeSupported);

  return result;
}
''')]


def recorder_source():
    lines = []
    for first, text in RECORDER_SECTIONS:
        lines.extend("// unrelated Chromium source\n" for _ in range(first - 1 - len(lines)))
        lines.extend(text.splitlines(keepends=True))
    lines.append("// trailing Chromium source\n")
    return "".join(lines)


def roundtrip_recorder(directory, original):
    target = directory / RECORDER
    target.parent.mkdir(parents=True)
    target.write_text(original)
    audio.apply_patch(directory, "0040")
    patched = target.read_text()
    audio.apply_patch(directory, "0040", reverse=True)
    assert target.read_text() == original
    return patched


@pytest.fixture(scope="module")
def sources(tmp_path_factory):
    recorder = roundtrip_recorder(tmp_path_factory.mktemp("recorder-source"), recorder_source())
    capabilities = codecs.apply_and_reverse(
        tmp_path_factory.mktemp("media-capabilities-source"), codecs.source_fixture().encode())
    return recorder, capabilities


def test_optional_local_provenance_and_roundtrip(tmp_path, sources):
    baseline = os.environ.get("CHROMIX_AUDIO_MEDIA_EXTENDED_BASELINE_ROOT")
    if not baseline:
        pytest.skip("set CHROMIX_AUDIO_MEDIA_EXTENDED_BASELINE_ROOT to local pre-Chromix 152 source")
    for target, sections, checksum in (
        (RECORDER, RECORDER_SECTIONS, RECORDER_SHA256),
        (str(codecs.TARGET), codecs.SOURCE_SECTIONS, codecs.BASELINE_SHA256),
    ):
        path = Path(baseline) / target
        original, mtime = path.read_bytes(), path.stat().st_mtime_ns
        assert hashlib.sha256(original).hexdigest() == checksum
        lines = original.decode().splitlines(keepends=True)
        for first, text in sections:
            assert "".join(lines[first - 1:first - 1 + len(text.splitlines())]) == text
        if target == RECORDER:
            patched = roundtrip_recorder(tmp_path / "recorder", original.decode())
            for name in ("start(int", "isTypeSupported("):
                assert recorder_method(patched, name) == recorder_method(sources[0], name)
        else:
            patched = codecs.apply_and_reverse(tmp_path / "capabilities", original)
            for name in codecs.CALLBACKS:
                assert codecs.method(patched, name) == codecs.method(sources[1], name)
        assert path.read_bytes() == original and path.stat().st_mtime_ns == mtime


def recorder_method(source, name):
    start = source.index("void MediaRecorder::" + name) if name.startswith("start") else source.index("bool MediaRecorder::" + name)
    return source[start:source.index("\n}\n", start) + 3]


def test_recording_queries_cannot_bypass_native_parser_or_encoder(sources):
    method = recorder_method(sources[0], "isTypeSupported(")
    without_comments = lambda text: re.sub(r"^\s*//[^\n]*\n", "", text, flags=re.M)
    assert without_comments(method) == without_comments(recorder_method(recorder_source(), "isTypeSupported("))
    assert "uxr-codec-matrix" not in sources[0]
    assert "CanSupportMimeTypeCaller::kIsTypeSupported" in method


@pytest.mark.parametrize("case", ["mime", "record-start", "record-guards", "record-bitrates",
                                  "mixed-recording", "webrtc-lifetime", "webrtc-results", "eme-fallback"])
def test_executable_media_contract(runtime_binary, case):
    result = subprocess.run([str(runtime_binary), case], text=True, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture(scope="module")
def runtime_binary(tmp_path_factory, sources):
    if codecs.CXX is None:
        pytest.skip("a local C++20 compiler is required")
    recorder, capabilities = sources
    record_tail = capabilities[capabilities.index("  DCHECK_EQ(config->type(), V8MediaEncodingType::Enum::kRecord);"):]
    record_tail = record_tail[:record_tail.index("\n}\n") + 3]
    source = CPP_SUPPORT + "\n" + codecs.helpers(capabilities)
    callback_start = capabilities.index("void OnMediaCapabilitiesEncodingInfo(")
    source += "\n" + capabilities[callback_start:capabilities.index("\n}\n", callback_start) + 3]
    source += "\n" + recorder_method(recorder, "start(int")
    source += "\n" + recorder_method(recorder, "isTypeSupported(")
    for name in ("GetPerfInfo", "OnWebrtcSupportInfo", "OnWebrtcPerfHistoryInfo"):
        source += "\n" + codecs.method(capabilities, name)
    source += "\nint RecordInfo(const MediaEncodingConfiguration* config, Resolver* resolver) {\n  const int promise = 42;\n" + record_tail
    source += "\n" + CPP_TESTS
    directory = tmp_path_factory.mktemp("audio-media-extended-runtime")
    path = directory / "media.cc"
    path.write_text(source)
    binary = directory / "media"
    result = subprocess.run([codecs.CXX, "-std=c++20", "-O0", "-Wall", "-Wextra", "-Werror",
                             str(path), "-o", str(binary)], text=True, capture_output=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return binary


CPP_SUPPORT = r'''
#include <algorithm>
#include <cassert>
#include <functional>
#include <limits>
#include <map>
#include <memory>
#include <optional>
#include <string>
#include <utility>
#include <vector>
#define DCHECK(x) assert(x)
#define DCHECK_EQ(x, y) assert((x) == (y))
#define UMA_HISTOGRAM_TIMES(name, value) ((void)(value))
#define FROM_HERE 0
struct Log { template<class T> Log& operator<<(const T&) { return *this; } };
#define DVLOG(level) Log()
using String = std::string;
namespace base {
struct TimeDelta {};
struct TimeTicks { static TimeTicks Now() { return {}; } };
TimeDelta operator-(TimeTicks, TimeTicks) { return {}; }
struct UxrConfig {
  std::map<std::string, std::string> values;
  static UxrConfig& GetInstance() { static UxrConfig result; return result; }
  bool Has(const char* key) const { return values.contains(key); }
  std::string Get(const char* key) const { auto it = values.find(key); return it == values.end() ? "" : it->second; }
};
enum { KEEP_WHITESPACE, SPLIT_WANT_NONEMPTY };
std::vector<std::string> SplitString(const std::string& input, const char*, int, int) {
  std::vector<std::string> result;
  for (size_t first = 0; first < input.size();) {
    size_t last = input.find(',', first);
    if (last == input.npos) last = input.size();
    if (first != last) result.push_back(input.substr(first, last - first));
    first = last + 1;
  }
  return result;
}
}
namespace media {
enum class VideoCodec { kUnknown, kH264, kVP8, kVP9, kAV1, kHEVC };
enum VideoCodecProfile { UNKNOWN, VP9PROFILE };
struct VideoColorSpace {};
VideoCodec VideoCodecProfileToVideoCodec(VideoCodecProfile value) { return value == VP9PROFILE ? VideoCodec::kVP9 : VideoCodec::kUnknown; }
}
namespace gfx { struct Size { Size(int, int) {} }; }
namespace media::mojom::blink {
struct PredictionFeatures {
  static auto New(VideoCodecProfile, gfx::Size, double, String, bool) { return std::make_unique<PredictionFeatures>(); }
};
using PredictionFeaturesPtr = std::unique_ptr<PredictionFeatures>;
struct WebrtcPredictionFeatures {
  VideoCodecProfile profile = VP9PROFILE;
  int video_pixels = 16;
  bool is_decode_stats = false, hardware_accelerated = false;
};
using WebrtcPredictionFeaturesPtr = std::unique_ptr<WebrtcPredictionFeatures>;
}
template<class T> T* WrapPersistent(T* value) { return value; }
template<class F, class... Args> auto BindOnce(F fn, Args&&... args) {
  return std::bind_front(fn, std::forward<Args>(args)...);
}
namespace blink { using ::BindOnce; }
struct TaskRunner {
  int posts = 0;
  template<class F> void PostTask(int, F&& task) { ++posts; std::move(task)(); }
};
enum class TaskType { kInternalMediaRealTime };
struct ExecutionContext {
  bool destroyed = false;
  TaskRunner runner;
  bool IsContextDestroyed() const { return destroyed; }
  TaskRunner* GetTaskRunner(TaskType) { assert(!destroyed); return &runner; }
};
struct MediaKeySystemAccess {
  String GetInternalKeySystem() const { return "real-key-system"; }
  bool UseHardwareSecureCodecs() const { return true; }
};
struct MediaCapabilitiesInfo {
  bool supported_ = false, smooth_ = false, power_ = false;
  static MediaCapabilitiesInfo* Create() { return new MediaCapabilitiesInfo; }
  void setSupported(bool v) { supported_ = v; }
  void setSmooth(bool v) { smooth_ = v; }
  void setPowerEfficient(bool v) { power_ = v; }
  bool supported() const { return supported_; }
  bool smooth() const { return smooth_; }
  bool powerEfficient() const { return power_; }
  virtual ~MediaCapabilitiesInfo() = default;
};
struct MediaCapabilitiesDecodingInfo : MediaCapabilitiesInfo {
  MediaKeySystemAccess* access = nullptr;
  static MediaCapabilitiesDecodingInfo* Create() { return new MediaCapabilitiesDecodingInfo; }
  void setKeySystemAccess(MediaKeySystemAccess* value) { access = value; }
};
MediaCapabilitiesDecodingInfo* CreateDecodingInfoWith(bool v) {
  auto* info = MediaCapabilitiesDecodingInfo::Create();
  info->setSupported(v); info->setSmooth(v); info->setPowerEfficient(v); return info;
}
MediaCapabilitiesInfo* CreateEncodingInfoWith(bool v) { return CreateDecodingInfoWith(v); }
struct Resolver {
  ExecutionContext* context;
  int resolves = 0;
  std::unique_ptr<MediaCapabilitiesInfo> result = nullptr;
  ExecutionContext* GetExecutionContext() { return context; }
  template<class T> Resolver* DowncastTo() { return this; }
  void Resolve(MediaCapabilitiesInfo* info) { assert(context && !context->destroyed); result.reset(info); ++resolves; }
};
template<class T> using ScriptPromiseResolver = Resolver;
struct VideoConfiguration { int width() const { return 16; } int height() const { return 16; } double framerate() const { return 30; } };
struct MediaDecodingConfiguration {
  bool video_present = true;
  VideoConfiguration video_;
  bool hasVideo() const { return video_present; }
  const VideoConfiguration* video() const { return &video_; }
};
struct WebTrackConfiguration { String mime_type, codec; };
struct WebMediaConfiguration { std::optional<WebTrackConfiguration> audio_configuration, video_configuration; };
namespace V8MediaEncodingType { enum class Enum { kRecord }; }
struct MediaEncodingConfiguration {
  WebMediaConfiguration value;
  V8MediaEncodingType::Enum type() const { return V8MediaEncodingType::Enum::kRecord; }
};
WebMediaConfiguration ToWebMediaConfiguration(const MediaEncodingConfiguration* config) { return config->value; }
struct RuntimeEnabledFeatures { static bool MediaCapabilitiesEncodingInfoEnabled() { return true; } };
struct KeyFrameRequestProcessor { struct Configuration {}; };
struct ContentType {
  String value;
  explicit ContentType(String input) : value(std::move(input)) {}
  String GetType() const { return value.substr(0, value.find(';')); }
  String Parameter(const char*) const { size_t at = value.find("codecs="); return at == value.npos ? "" : value.substr(at + 7); }
};
struct WebMediaCapabilitiesInfo { bool supported = false, smooth = false, power_efficient = false; };
struct MediaRecorderHandler {
  enum class CanSupportMimeTypeCaller { kIsTypeSupported, kEncodingInfo };
  bool support = false, start_result = false, dirty = false, observing = false;
  WebMediaCapabilitiesInfo encoding_result;
  WebMediaConfiguration encoded_configuration;
  int queries = 0, starts = 0, stops = 0, encodes = 0, timeslice = 0;
  uint32_t audio_rate = 0, video_rate = 0;
  String queried_type, queried_codec;
  CanSupportMimeTypeCaller caller = CanSupportMimeTypeCaller::kIsTypeSupported;
  bool CanSupportMimeType(const String& type, const String& codec, CanSupportMimeTypeCaller who) {
    ++queries; queried_type = type; queried_codec = codec; caller = who; return support;
  }
  bool Start(int time, const String&, uint32_t audio, uint32_t video) {
    assert(!dirty && !observing); ++starts; dirty = observing = true;
    timeslice = time; audio_rate = audio; video_rate = video; return start_result;
  }
  void Stop() { ++stops; dirty = observing = false; timeslice = 0; }
  void EncodingInfo(const WebMediaConfiguration& configuration,
                    std::function<void(std::unique_ptr<WebMediaCapabilitiesInfo>)> callback) {
    ++encodes; encoded_configuration = configuration;
    std::move(callback)(std::make_unique<WebMediaCapabilitiesInfo>(encoding_result));
  }
};
MediaRecorderHandler handler;
bool handler_available = true;
template<class T, class... Args> T* MakeGarbageCollected(Args&&... args) {
  if constexpr (std::is_same_v<T, MediaRecorderHandler>) return handler_available ? &handler : nullptr;
  else return new T(std::forward<Args>(args)...);
}
enum class DOMExceptionCode { kNotSupportedError, kInvalidStateError };
struct ExceptionState {
  std::optional<DOMExceptionCode> error;
  void ThrowDOMException(DOMExceptionCode code, const String&) { error = code; }
};
String StrCat(std::initializer_list<String> parts) { String result; for (const auto& item : parts) result += item; return result; }
uint32_t ClampAudioBitRate(ExecutionContext*, uint32_t value) { return std::clamp(value, 5000u, 510000u); }
uint32_t ClampVideoBitRate(ExecutionContext*, uint32_t value) { return std::max(value, 75000u); }
struct Stream {
  std::vector<int> audio{1}, video;
  std::vector<int> getTracks() const { auto result = audio; result.insert(result.end(), video.begin(), video.end()); return result; }
  const auto& getAudioTracks() const { return audio; }
  const auto& getVideoTracks() const { return video; }
};
struct StateString { String AsStringView() const { return "recording"; } };
struct MediaRecorder {
  enum class State { kInactive, kRecording, kPaused };
  State state_ = State::kInactive;
  ExecutionContext* context;
  Stream* stream_;
  MediaRecorderHandler* recorder_handler_ = &handler;
  String mime_type_ = "audio/webm;codecs=opus";
  uint32_t audio_bits_per_second_ = 128000, video_bits_per_second_ = 2500000;
  std::optional<uint32_t> overall_bits_per_second_ = std::nullopt;
  ExecutionContext* GetExecutionContext() { return context; }
  StateString state() const { return {}; }
  void start(int, ExceptionState&);
  static bool isTypeSupported(ExecutionContext*, const String&);
};
template<class T> struct CallbackMap : std::map<int, std::unique_ptr<T>> {
  bool Contains(int id) const { return this->contains(id); }
  T* at(int id) { return std::map<int, std::unique_ptr<T>>::at(id).get(); }
  void insert(int id, T* value) { this->emplace(id, value); }
};
bool UseGpuFactoriesForPowerEfficient(ExecutionContext*, MediaKeySystemAccess*) { return false; }
bool WebrtcDecodeForceSmoothIfPowerEfficient() { return false; }
bool WebrtcEncodeForceSmoothIfPowerEfficient() { return false; }
struct History {
  int calls = 0;
  template<class... Args> void GetPerfInfo(Args&&...) { ++calls; }
};
struct MediaCapabilities {
  enum class OperationType { kEncoding, kDecoding };
  struct PendingCallbackState {
    Resolver* resolver;
    MediaKeySystemAccess* key_system_access;
    base::TimeTicks request_time;
    media::VideoCodec video_codec = media::VideoCodec::kUnknown;
    std::optional<bool> is_supported, is_gpu_factories_supported;
    PendingCallbackState(Resolver* r, MediaKeySystemAccess* access, base::TimeTicks time)
        : resolver(r), key_system_access(access), request_time(time) {}
  };
  CallbackMap<PendingCallbackState> pending_cb_map_;
  History history;
  History* decode_history_service_ = &history;
  History* webrtc_history_service_ = &history;
  bool history_available = true;
  int ensure_calls = 0;
  bool EnsurePerfHistoryService(ExecutionContext* context) { assert(context && !context->destroyed); ++ensure_calls; return history_available; }
  bool EnsureWebrtcPerfHistoryService(ExecutionContext* context) { return EnsurePerfHistoryService(context); }
  int CreateCallbackId() { return 17; }
  void GetGpuFactoriesSupport(int, media::VideoCodec, media::VideoCodecProfile, media::VideoColorSpace, const MediaDecodingConfiguration*) {}
  void OnPerfHistoryInfo(int, bool, bool) {}
  void GetPerfInfo(media::VideoCodec, media::VideoCodecProfile, media::VideoColorSpace,
                   const MediaDecodingConfiguration*, const base::TimeTicks&, Resolver*, MediaKeySystemAccess*);
  void OnWebrtcSupportInfo(int, media::mojom::blink::WebrtcPredictionFeaturesPtr,
                           float, OperationType, bool, bool);
  void OnWebrtcPerfHistoryInfo(int, OperationType, bool);
};
'''

CPP_TESTS = r'''
int main(int argc, char** argv) {
  assert(argc == 2);
  const String test = argv[1];
  ExecutionContext context;
  auto& config = base::UxrConfig::GetInstance().values;
  if (test == "mime") {
    const std::vector<String> types = {"", "video/unknown", "audio/webm;codecs=opus",
        "audio/webm;codecs=opus-invalid", "video/webm;codecs=vp8,unknown",
        "video/webm;codecs=vp8", "video/mp4;codecs=avc1.640028",
        "audio/mp4;codecs=mp4a.40.2", "video/webm;codecs=vp9garbage",
        "VIDEO/WEBM;CODECS=VP8", "video/webm;codecs=\"vp8,opus\";foo=bar"};
    for (const String& matrix : {String(), String("desktop"), String("supported")}) {
      config["uxr-codec-matrix"] = matrix;
      for (const auto& type : types) {
        for (bool native : {false, true}) {
          handler = {}; handler.support = native;
          assert(MediaRecorder::isTypeSupported(&context, type) == native);
          assert(handler.queries == 1 && handler.starts == 0);
          assert(handler.queried_type == ContentType(type).GetType());
          assert(handler.queried_codec == ContentType(type).Parameter("codecs"));
          assert(handler.caller == MediaRecorderHandler::CanSupportMimeTypeCaller::kIsTypeSupported);
        }
      }
    }
    handler_available = false;
    assert(!MediaRecorder::isTypeSupported(&context, "audio/webm"));
  } else if (test == "record-start") {
    Stream stream;
    MediaRecorder recorder{.context = &context, .stream_ = &stream};
    for (int attempt = 0; attempt < 2; ++attempt) {
      ExceptionState error;
      recorder.start(100, error);
      assert(error.error == DOMExceptionCode::kNotSupportedError);
      assert(recorder.state_ == MediaRecorder::State::kInactive);
      assert(handler.stops == attempt + 1 && !handler.dirty && !handler.observing);
    }
    handler.start_result = true;
    ExceptionState error;
    recorder.start(100, error);
    assert(!error.error && recorder.state_ == MediaRecorder::State::kRecording);
    assert(handler.starts == 3 && handler.stops == 2 && handler.dirty && handler.observing);
  } else if (test == "record-guards") {
    for (int condition = 0; condition < 4; ++condition) {
      handler = {}; context.destroyed = condition == 1;
      Stream stream;
      if (condition == 3) stream.audio.clear();
      MediaRecorder recorder{.context = condition == 0 ? nullptr : &context, .stream_ = &stream};
      if (condition == 2) recorder.state_ = MediaRecorder::State::kPaused;
      ExceptionState error;
      recorder.start(100, error);
      assert(error.error == (condition == 2 ? DOMExceptionCode::kInvalidStateError : DOMExceptionCode::kNotSupportedError));
      assert(!handler.starts && !handler.stops);
    }
  } else if (test == "record-bitrates") {
    for (int tracks = 1; tracks < 4; ++tracks) {
      handler = {}; handler.start_result = true;
      Stream stream; stream.audio.resize(tracks & 1); stream.video.resize((tracks >> 1) & 1);
      MediaRecorder recorder{.context = &context, .stream_ = &stream};
      recorder.overall_bits_per_second_ = 1000000;
      ExceptionState error;
      recorder.start(0, error);
      assert(!error.error && handler.timeslice == 0 && !handler.stops);
      assert(handler.audio_rate == (tracks == 1 ? 510000u : tracks == 2 ? 0u : 128000u));
      assert(handler.video_rate == (tracks == 1 ? 0u : tracks == 2 ? 1000000u : 2500000u));
    }
  } else if (test == "mixed-recording") {
    for (int tracks = 1; tracks < 4; ++tracks) {
      for (int native = 0; native < 8; ++native) {
        for (bool supported : {false, true}) {
          handler = {}; handler.support = supported;
          handler.encoding_result = {bool(native & 1), bool(native & 2), bool(native & 4)};
          MediaEncodingConfiguration encoding;
          if (tracks & 1) encoding.value.audio_configuration = WebTrackConfiguration{"audio/webm", "unknown"};
          if (tracks & 2) encoding.value.video_configuration = WebTrackConfiguration{"video/webm", "vp8"};
          Resolver resolver{.context = &context};
          const int previous_posts = context.runner.posts;
          assert(RecordInfo(&encoding, &resolver) == 42);
          const bool rejected = tracks == 3 && !supported;
          assert(resolver.resolves == 1 && resolver.result->supported() == (!rejected && bool(native & 1)));
          assert(resolver.result->smooth() == (!rejected && bool(native & 2)));
          assert(resolver.result->powerEfficient() == (!rejected && bool(native & 4)));
          assert(handler.queries == (tracks == 3 ? 1 : 0));
          assert(handler.encodes == (rejected ? 0 : 1));
          assert(context.runner.posts - previous_posts == (rejected ? 0 : 1));
          if (tracks == 3) {
            assert(handler.queried_type == "audio/webm" && handler.queried_codec == "unknown");
            assert(handler.caller == MediaRecorderHandler::CanSupportMimeTypeCaller::kEncodingInfo);
          }
          if (!rejected) {
            assert(handler.encoded_configuration.audio_configuration.has_value() == bool(tracks & 1));
            assert(handler.encoded_configuration.video_configuration.has_value() == bool(tracks & 2));
          }
        }
      }
    }
    handler_available = false;
    MediaEncodingConfiguration encoding;
    Resolver resolver{.context = &context};
    assert(RecordInfo(&encoding, &resolver) == 42 && resolver.resolves == 1);
    assert(!resolver.result->supported() && !resolver.result->smooth() && !resolver.result->powerEfficient());
    context.destroyed = true;
    Resolver detached{.context = &context};
    OnMediaCapabilitiesEncodingInfo(&detached, std::make_unique<WebMediaCapabilitiesInfo>());
    assert(!detached.resolves);
  } else if (test == "webrtc-lifetime") {
    for (bool missing : {false, true}) {
      for (bool supported : {false, true}) {
        for (bool audio_only : {false, true}) {
          MediaCapabilities caps;
          context.destroyed = true;
          Resolver resolver{.context = missing ? nullptr : &context};
          caps.pending_cb_map_.insert(1, new MediaCapabilities::PendingCallbackState(&resolver, nullptr, {}));
          auto features = std::make_unique<media::mojom::blink::WebrtcPredictionFeatures>();
          features->video_pixels = audio_only ? 0 : 16;
          caps.OnWebrtcSupportInfo(1, std::move(features), 30, MediaCapabilities::OperationType::kEncoding, supported, true);
          assert(caps.pending_cb_map_.empty() && !resolver.resolves && !caps.ensure_calls && !caps.history.calls);
        }
      }
    }
  } else if (test == "webrtc-results") {
    for (auto operation : {MediaCapabilities::OperationType::kEncoding, MediaCapabilities::OperationType::kDecoding}) {
      for (bool audio_only : {false, true}) {
        for (bool supported : {false, true}) {
          for (bool allowed : {false, true}) {
            MediaCapabilities caps;
            Resolver resolver{.context = &context};
            config["uxr-codec-vp9"] = allowed ? "supported,smooth,power-efficient" : "";
            caps.pending_cb_map_.insert(1, new MediaCapabilities::PendingCallbackState(&resolver, nullptr, {}));
            auto features = std::make_unique<media::mojom::blink::WebrtcPredictionFeatures>();
            features->video_pixels = audio_only ? 0 : 16;
            features->profile = audio_only ? media::UNKNOWN : media::VP9PROFILE;
            caps.OnWebrtcSupportInfo(1, std::move(features), 30, operation, supported, supported);
            if (supported && !audio_only) {
              assert(!resolver.resolves && caps.history.calls == 1 && caps.pending_cb_map_.Contains(1));
              caps.OnWebrtcPerfHistoryInfo(1, operation, false);
            }
            assert(resolver.resolves == 1 && caps.pending_cb_map_.empty());
            const bool expected = supported && (audio_only || allowed);
            assert(resolver.result->supported() == expected && resolver.result->powerEfficient() == expected);
            assert(resolver.result->smooth() == (expected && audio_only));
          }
        }
      }
    }
  } else if (test == "eme-fallback") {
    for (bool video : {false, true}) {
      for (bool encrypted : {false, true}) {
        for (bool allowed : {false, true}) {
          MediaCapabilities caps; caps.history_available = false;
          MediaKeySystemAccess access;
          MediaDecodingConfiguration decoding; decoding.video_present = video;
          Resolver resolver{.context = &context};
          config["uxr-codec-vp9"] = allowed ? "supported,smooth,power-efficient" : "";
          caps.GetPerfInfo(media::VideoCodec::kVP9, media::VP9PROFILE, {}, &decoding, {}, &resolver, encrypted ? &access : nullptr);
          assert(resolver.resolves == 1 && caps.pending_cb_map_.empty() && !caps.history.calls);
          assert(resolver.result->supported() == (!video || allowed));
          auto* info = dynamic_cast<MediaCapabilitiesDecodingInfo*>(resolver.result.get());
          assert(info && info->access == (encrypted ? &access : nullptr));
        }
      }
    }
  } else { assert(false); }
}
'''
