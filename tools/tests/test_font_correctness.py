import re
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


REPO = Path(__file__).resolve().parents[2]
TEXT_METRICS = REPO / "patches" / "0033-third_party-blink-renderer-core-html-canvas-text_metrics-cc.patch"
FONT_CACHE = REPO / "patches" / "0047-third_party-blink-renderer-platform-fonts-font_cache-cc.patch"
PACKAGE_WIN = REPO / "build" / "windows" / "package-win.ps1"
PACKAGE_LINUX = REPO / "build" / "linux" / "package-linux.sh"
FONTS = REPO / "assets" / "fonts"
PY_FONTS = REPO / "sdk" / "python" / "chromix" / "_fonts.py"
PY_API = REPO / "sdk" / "python" / "chromix" / "api.py"
NODE_FONTS = REPO / "sdk" / "node" / "_fonts.js"
NODE_INDEX = REPO / "sdk" / "node" / "index.js"
NODE_PACKAGE = REPO / "sdk" / "node" / "package.json"


class FontCorrectnessRegressionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.metrics = TEXT_METRICS.read_text(encoding="utf-8")
        cls.fonts = FONT_CACHE.read_text(encoding="utf-8")
        cls.package = PACKAGE_WIN.read_text(encoding="utf-8")
        cls.linux_package = PACKAGE_LINUX.read_text(encoding="utf-8")
        cls.py_fonts = PY_FONTS.read_text(encoding="utf-8")
        cls.py_api = PY_API.read_text(encoding="utf-8")
        cls.node_fonts = NODE_FONTS.read_text(encoding="utf-8")
        cls.node_index = NODE_INDEX.read_text(encoding="utf-8")
        cls.node_package = NODE_PACKAGE.read_text(encoding="utf-8")

    def test_allowlist_preserves_multiword_family_names(self):
        self.assertIn("base::SplitString", self.fonts)
        self.assertIn("base::TRIM_WHITESPACE", self.fonts)
        self.assertIn("base::EqualsCaseInsensitiveASCII", self.fonts)
        self.assertNotIn("ph_c == ' '", self.fonts)
        self.assertNotIn("ph_norm", self.fonts)

    def test_filter_covers_family_and_local_lookup(self):
        # Family lookup AND src:local() unique-name probing are both filtered;
        # only downloaded faces and the last-resort fallback stay native.
        self.assertIn("CreationType() == kCreateFontByFamily", self.fonts)
        self.assertIn("AlternateFontName::kLastResort", self.fonts)
        self.assertIn("UxrFontFamilyAllowed(creation_params.Family())", self.fonts)
        self.assertIn("src:local()", self.fonts)
        self.assertNotIn("AlternateFontName::kLocalUniqueFace", self.fonts)
        self.assertNotIn("UxrFontHidden(family)", self.fonts)

    def test_linux_windows_persona_keeps_real_families_visible(self):
        self.assertIn("kBundledWindowsFamilies", self.fonts)
        for family in ("Arial Narrow", "MS Gothic", "Segoe UI Light", "Wingdings 3", "ＭＳ ゴシック"):
            self.assertIn(f'"{family}"', self.fonts)

    def test_generics_keep_native_resolution(self):
        for family in ("serif", "sans-serif", "monospace", "system-ui", "emoji"):
            self.assertIn(f'"{family}"', self.fonts)

    def test_generics_resolve_to_persona_families_under_windows_persona(self):
        # UxrSystemFontSubstitute maps CSS generics to Chrome's Windows
        # defaults so a Linux host can't leak through native generic
        # resolution (system-ui -> Cantarell would expose the host OS).
        self.assertIn("UxrSystemFontSubstitute", self.fonts)
        for generic, target in (("system-ui", "Segoe UI"),
                                ("sans-serif", "Arial"),
                                ("serif", "Times New Roman"),
                                ("monospace", "Consolas"),
                                ("emoji", "Segoe UI Emoji"),
                                ("cursive", "Comic Sans MS"),
                                ("fantasy", "Impact"),
                                ("math", "Cambria Math"),
                                ("ui-monospace", "Consolas")):
            self.assertIn(f'{{"{generic}", "{target}"}}', self.fonts)
        # substitution runs before the whitelist check via a recursive
        # family lookup, and stays inactive for explicit custom whitelists
        self.assertIn("FontFaceCreationParams(AtomicString(ux_sub))", self.fonts)
        self.assertIn('Get("uxr-font-whitelist").empty()', self.fonts)

    def test_text_metrics_are_not_independently_jittered(self):
        self.assertNotIn("UxrJitterMetric", self.metrics)
        self.assertNotIn("uxr-canvas-seed", self.metrics)
        self.assertNotIn("base/uxr_config.h", self.metrics)
        self.assertIn("actual shaped and rendered font", self.metrics)

    def test_windows_font_bundle_has_provenance_and_expected_formats(self):
        self.assertTrue((FONTS / "NOTICE").is_file())
        self.assertTrue((FONTS / "SOURCE.md").is_file())
        self.assertTrue((FONTS / "fonts.conf.template").is_file())
        font_files = [p for p in FONTS.iterdir() if p.is_file()]
        self.assertGreaterEqual(len(font_files), 150)
        self.assertTrue(any(p.suffix.lower() == ".ttc" for p in font_files))
        self.assertTrue(any(p.suffix.lower() == ".fon" for p in font_files))
        for family in ("Arial", "Calibri", "Cambria", "Consolas", "SegoeUI", "Tahoma", "TimesNewRoman", "Verdana"):
            self.assertTrue(any(FONTS.glob(f"{family}-*.ttf")), family)
        self.assertNotIn("FORTRESS-LICENSE", " ".join(p.name for p in font_files))
        self.assertNotIn("ATTRIBUTION.md", " ".join(p.name for p in font_files))

    def test_linux_package_bundles_supported_font_formats_and_launcher(self):
        for text in ("fonts.conf.template", "FONTCONFIG_FILE", "NOTICE", "SOURCE.md", "*.ttc"):
            self.assertIn(text, self.linux_package)
        self.assertIn("-iname '*.ttf'", self.linux_package)
        self.assertIn("-iname '*.ttc'", self.linux_package)
        self.assertNotIn("FORTRESS-LICENSE", self.linux_package)
        self.assertIn("exec \"$HERE/chrome\"", self.linux_package)

    def test_windows_package_does_not_install_or_register_clone_fonts(self):
        for forbidden in ("AddFontResource", "Fonts\\", "fonts.conf", "FONTCONFIG_FILE"):
            self.assertNotIn(forbidden, self.package)

    def test_sdk_font_wiring_is_linux_only_and_caller_env_wins(self):
        for source in (self.py_fonts, self.node_fonts):
            self.assertIn('sys.platform != "linux"' if source == self.py_fonts else 'process.platform !== "linux"', source)
            self.assertIn("FONTCONFIG_FILE", source)
            self.assertIn("user_env" if source == self.py_fonts else "userEnv", source)
        self.assertIn('"_fonts.js"', self.node_package)

    def test_sdk_font_dir_generates_whitelist_from_name_tables(self):
        # Both SDKs parse OpenType name tables (IDs 1+16) and TTC members to
        # build --uxr-font-whitelist from a user-provided font directory.
        self.assertIn("font_families_in_dir", self.py_fonts)
        self.assertIn("font_dir_whitelist_arg", self.py_fonts)
        self.assertIn('b"ttcf"', self.py_fonts)
        self.assertIn("utf-16-be", self.py_fonts)
        self.assertIn("--uxr-font-whitelist=", self.py_fonts)
        self.assertIn("fontFamiliesInDir", self.node_fonts)
        self.assertIn("fontDirWhitelistArg", self.node_fonts)
        self.assertIn('"ttcf"', self.node_fonts)
        self.assertIn("--uxr-font-whitelist=", self.node_fonts)

    def test_launch_paths_wire_fonts_dir(self):
        # fonts_dir must reach both the CLI args (_prepare/buildLaunchOptions)
        # and the env wiring (apply_font_env/fontLaunchEnv).
        self.assertIn("fonts_dir=fonts_dir", self.py_api)
        self.assertIn("font_dir_whitelist_arg", self.py_api)
        self.assertIn("options.fontsDir", self.node_index)
        self.assertIn("fontDirWhitelistArg", self.node_index)

    def test_font_dir_parser_matches_bundled_windows_families(self):
        # Functional: the parser must reproduce patch 0047's static family
        # list from assets/fonts exactly (validates TTC/subfamily handling).
        import importlib.util
        spec = importlib.util.spec_from_file_location("chromix_fonts", PY_FONTS)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        families = set(mod.font_families_in_dir(FONTS))
        self.assertGreaterEqual(len(families), 50)
        for family in ("Arial", "Arial Narrow", "Calibri", "Cambria Math",
                       "Consolas", "MS Gothic", "MS PGothic", "Segoe UI",
                       "Segoe UI Light", "Tahoma", "Times New Roman",
                       "Verdana", "Wingdings 3", "ＭＳ ゴシック"):
            self.assertIn(family, families)


# Chromium 152.0.7977.82 context, independent of the patch under test.
FONT_CACHE_SECTIONS = (
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
)

# Only the config, strings and native backend are stubbed. The complete patched
# lookup and its helpers are compiled verbatim, not a model of their behavior.
FONT_RUNTIME_STUBS = r'''
#include <cassert>
#include <iostream>
#include <string>
#include <vector>

std::string AsciiLower(std::string text) {
  for (char& c : text) {
    if (c >= 'A' && c <= 'Z')
      c += 'a' - 'A';
  }
  return text;
}
namespace base {
struct UxrConfig {
  std::string persona;
  std::string whitelist;
  static UxrConfig& GetInstance() {
    static UxrConfig instance;
    return instance;
  }
  std::string Get(const char* key) const {
    if (std::string(key) == "uxr-synthetic-device-tests")
      return persona == "native-test" ? "false" : "true";
    if (std::string(key) == "uxr-platform")
      return persona;
    assert(std::string(key) == "uxr-font-whitelist");
    return whitelist;
  }
};
bool EqualsCaseInsensitiveASCII(const std::string& a, const std::string& b) {
  return AsciiLower(a) == AsciiLower(b);
}
enum WhitespaceHandling { TRIM_WHITESPACE };
enum SplitResult { SPLIT_WANT_NONEMPTY };
std::vector<std::string> SplitString(const std::string& input,
                                   const std::string& delimiters,
                                   WhitespaceHandling, SplitResult) {
  std::vector<std::string> result;
  size_t start = 0;
  while (start <= input.size()) {
    const size_t end = input.find_first_of(delimiters, start);
    std::string entry = input.substr(start, end - start);
    const size_t first = entry.find_first_not_of(" \t\r\n\f\v");
    if (first != std::string::npos) {
      const size_t last = entry.find_last_not_of(" \t\r\n\f\v");
      result.push_back(entry.substr(first, last - first + 1));
    }
    if (end == std::string::npos)
      break;
    start = end + 1;
  }
  return result;
}
}
struct String {
  std::string value;
  String ToAsciiLower() const { return {AsciiLower(value)}; }
  std::string Utf8() const { return value; }
};
struct AtomicString {
  String value;
  explicit AtomicString(const char* text) : value{text} {}
  const String& GetString() const { return value; }
  bool empty() const { return value.value.empty(); }
  bool operator==(const AtomicString& other) const {
    return value.value == other.value.value;
  }
};
enum FontFaceCreationType {
  kCreateFontByFamily,
  kCreateFontByFciIdAndTtcIndex
};
enum class AlternateFontName {
  kAllowAlternate, kNoAlternate, kLocalUniqueFace, kLastResort
};
struct FontFaceCreationParams {
  AtomicString family;
  FontFaceCreationType type = kCreateFontByFamily;
  explicit FontFaceCreationParams(AtomicString name) : family(name) {}
  FontFaceCreationType CreationType() const { return type; }
  const AtomicString& Family() const {
    assert(type == kCreateFontByFamily);
    return family;
  }
};
struct FontDescription { int marker = 42; };
struct FontPlatformData { std::string family; };
struct FontCache;
struct NativeBackend {
  std::string missing;
  bool denied = false;
  FontPlatformData result;
  std::vector<std::string> attempts;
  const FontPlatformData* GetOrCreateFontPlatformData(
      FontCache*, const FontDescription& description,
      const FontFaceCreationParams& params, AlternateFontName mode) {
    const std::string name = params.type == kCreateFontByFamily
                                 ? params.Family().GetString().Utf8()
                                 : "<file>";
    attempts.push_back(name + "|" + std::to_string(static_cast<int>(mode)) +
                       "|" + std::to_string(description.marker));
    if (denied || name == missing)
      return nullptr;
    result.family = name;
    return &result;
  }
};
namespace font_family_names {
const AtomicString kSystemUi("system-ui");
}
struct FontCache {
  NativeBackend font_platform_data_cache_;
  const FontPlatformData* GetFontPlatformData(
      const FontDescription&, const FontFaceCreationParams&, AlternateFontName);
  const FontPlatformData* SystemFontPlatformData(const FontDescription& desc) {
    return GetFontPlatformData(desc, FontFaceCreationParams(AtomicString("Host GUI")),
                               AlternateFontName::kNoAlternate);
  }
};
#define TRACE_EVENT0(category, name) ((void)0)
#define BUILDFLAG(flag) (flag)
'''

FONT_RUNTIME_MAIN = r'''
int main(int argc, char** argv) {
  assert(argc == 8);
  auto& config = base::UxrConfig::GetInstance();
  config.persona = argv[2];
  config.whitelist = argv[3];
  AtomicString family(argv[4]);
  const std::string action = argv[1];
  if (action == "allowed") {
    std::cout << UxrFontFamilyAllowed(family) << '\n';
  } else if (action == "substitute") {
    const char* substitute = UxrSystemFontSubstitute(family);
    std::cout << (substitute ? substitute : "<null>") << '\n';
  } else {
    assert(action == "lookup");
    FontCache cache;
    cache.font_platform_data_cache_.missing = argv[6];
    cache.font_platform_data_cache_.denied = std::string(argv[7]) == "denied";
    FontFaceCreationParams params(family);
    const int mode = std::stoi(argv[5]);
    if (mode == 4)
      params.type = kCreateFontByFciIdAndTtcIndex;
    const auto* result = cache.GetFontPlatformData(
        FontDescription(), params,
        mode == 4 ? AlternateFontName::kAllowAlternate
                  : static_cast<AlternateFontName>(mode));
    std::cout << (result ? result->family : "<null>") << '\n';
    for (const auto& attempt : cache.font_platform_data_cache_.attempts)
      std::cout << attempt << '\n';
  }
  return 0;
}
'''


class FontCacheFunctionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        patch_bin = shutil.which("gpatch") or shutil.which("patch")
        compiler = os.environ.get("CXX") or shutil.which("clang++") or shutil.which("g++")
        if not patch_bin or not compiler:
            raise unittest.SkipTest("GNU patch and a local C++ compiler are required")
        temporary = tempfile.TemporaryDirectory(prefix=".font-correctness-", dir=REPO)
        cls.addClassCleanup(temporary.cleanup)
        directory = Path(temporary.name)
        lines = []
        for first, section in FONT_CACHE_SECTIONS:
            while len(lines) < first - 1:
                lines.append(f"// unrelated source line {len(lines) + 1}\n")
            lines.extend(section.splitlines(keepends=True))
        lines.append("// trailing source stays intact\n")
        original = "".join(lines)
        target = directory / "third_party/blink/renderer/platform/fonts/font_cache.cc"
        target.parent.mkdir(parents=True)
        target.write_bytes(original.encode("utf-8"))
        command = [patch_bin, "-p1", "--fuzz=0", "--batch", "--forward",
                   "--binary", "--get=0", "--no-backup-if-mismatch", "--reject-file=-",
                   "--input", str(FONT_CACHE)]
        result = subprocess.run(command, cwd=directory, capture_output=True,
                                text=True, timeout=15)
        if result.returncode or re.search(r"fuzz|offset|FAILED", result.stdout + result.stderr):
            raise AssertionError(result.stdout + result.stderr)
        patched = target.read_text(encoding="utf-8")
        result = subprocess.run(command + ["--reverse"], cwd=directory,
                                capture_output=True, text=True, timeout=15)
        if result.returncode or target.read_text(encoding="utf-8") != original:
            raise AssertionError("Patch round-trip failed: " + result.stdout + result.stderr)
        helpers = patched.split("namespace blink {\n", 1)[1].split(
            "const char kColorEmojiLocale[]", 1)[0]
        lookup = patched.split("const FontPlatformData* FontCache::GetFontPlatformData(", 1)[1]
        source = FONT_RUNTIME_STUBS + helpers
        source += "const FontPlatformData* FontCache::GetFontPlatformData(" + lookup
        source += FONT_RUNTIME_MAIN
        if os.name == 'nt':
            source = '#define NOMINMAX\n#include <windows.h>\n' + source.replace('int main(int argc, char** argv)', 'int test_main(int argc, char** argv)')
            source += r'''
int wmain(int argc, wchar_t** wide) {
  std::vector<std::string> strings;
  for (int i = 0; i < argc; ++i) {
    int size = WideCharToMultiByte(CP_UTF8, 0, wide[i], -1, nullptr, 0, nullptr, nullptr);
    strings.emplace_back(size, '\0');
    WideCharToMultiByte(CP_UTF8, 0, wide[i], -1, strings.back().data(), size, nullptr, nullptr);
  }
  std::vector<char*> args;
  for (auto& s : strings) args.push_back(s.data());
  return test_main(argc, args.data());
}
'''
        cpp = directory / "font_functions.cc"
        cpp.write_text(source, encoding="utf-8")
        cls.binaries = []
        for is_mac in (0, 1):
            binary = directory / f"font-functions-{is_mac}"
            result = subprocess.run(
                [compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                 f"-DIS_MAC={is_mac}", str(cpp), "-o", str(binary)],
                capture_output=True, text=True, timeout=30)
            if result.returncode:
                raise AssertionError(result.stdout + result.stderr)
            cls.binaries.append(binary)

    def check_runtime(self, expected, action, family, *, persona="windows",
                      whitelist="", mode=0, missing="", denied=False):
        for binary in self.binaries:
            with self.subTest(platform=binary.name, action=action, family=family,
                              persona=persona, whitelist=whitelist, mode=mode):
                result = subprocess.run(
                    [str(binary), action, persona, whitelist, family, str(mode),
                     missing, "denied" if denied else "allowed"],
                    capture_output=True, text=True, encoding='utf-8', timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.splitlines(), expected)

    def test_allowlist_exact_names_case_and_persona(self):
        for family in ("Arial", "aRiAl NaRrOw", "Segoe UI Light", "ＭＳ ゴシック"):
            self.check_runtime(["1"], "allowed", family)
        for family in ("Cantarell", "ArialNarrow", "Arial Other", "Arial "):
            self.check_runtime(["0"], "allowed", family)
        for persona in ("", "linux", "macos"):
            self.check_runtime(["1"], "allowed", "Host Font", persona=persona)
        self.check_runtime(["0"], "allowed", "Host Font", persona="WiN32")
        for generic in ("", "SERIF", "sans-serif", "system-ui", "emoji", "fangsong"):
            self.check_runtime(["1"], "allowed", generic)

    def test_native_mode_ignores_synthetic_whitelist_and_mapping(self):
        self.check_runtime(["1"], "allowed", "Host Font", persona="native-test", whitelist="Other")
        self.check_runtime(["<null>"], "substitute", "system-ui", persona="native-test")

    def test_custom_whitelist_overrides_default_without_splitting_spaces(self):
        for persona in ("windows", "linux"):
            for family in ("Custom Family", "Arial Narrow", "ＭＳ ゴシック"):
                self.check_runtime(["1"], "allowed", family, persona=persona,
                                   whitelist="  custom family, ,Arial Narrow, ＭＳ ゴシック ")
            for family in ("Custom", "Family", "Arial", "ArialNarrow", "Segoe UI"):
                self.check_runtime(["0"], "allowed", family, persona=persona,
                                   whitelist="Custom Family, Arial Narrow")
        self.check_runtime(["0"], "allowed", "Arial", whitelist=" , ")
        self.check_runtime(["1"], "allowed", "serif", whitelist="Custom Family")

    def test_generic_substitutions_are_allowed_and_non_recursive(self):
        expected = {
            "system-ui": "Segoe UI", "sans-serif": "Arial", "serif": "Times New Roman",
            "monospace": "Consolas", "emoji": "Segoe UI Emoji", "cursive": "Comic Sans MS",
            "fantasy": "Impact", "math": "Cambria Math", "ui-monospace": "Consolas",
            "ui-serif": "Times New Roman", "ui-sans-serif": "Segoe UI", "ui-rounded": "Segoe UI",
            "-webkit-body": "Times New Roman", "-webkit-pictograph": "Segoe UI Emoji",
            "-webkit-system-font": "Segoe UI", "-webkit-control": "Segoe UI",
        }
        for generic, family in expected.items():
            self.check_runtime([family], "substitute", generic.upper(), persona="WiN32")
            self.check_runtime(["1"], "allowed", family)
            self.check_runtime(["<null>"], "substitute", family)
            for mode in (0, 1):
                self.check_runtime([family, f"{family}|{mode}|42"], "lookup", generic, mode=mode)

    def test_substitution_stays_inactive_with_custom_whitelist_or_other_persona(self):
        for persona in ("", "linux", "macos"):
            self.check_runtime(["<null>"], "substitute", "sans-serif", persona=persona)
            self.check_runtime(["sans-serif", "sans-serif|0|42"], "lookup", "sans-serif",
                               persona=persona)
        for whitelist in ("Custom Family", "Arial", " , "):
            self.check_runtime(["<null>"], "substitute", "sans-serif", whitelist=whitelist)
            self.check_runtime(["sans-serif", "sans-serif|0|42"], "lookup", "sans-serif",
                               whitelist=whitelist)
        for family in ("fangsong", "ui-fangsong", "Cantarell", "Arial"):
            self.check_runtime(["<null>"], "substitute", family)

    def test_local_lookup_is_filtered_without_generic_substitution(self):
        self.check_runtime(["<null>"], "lookup", "Host Font", mode=2)
        self.check_runtime(["Arial", "Arial|2|42"], "lookup", "Arial", mode=2)
        self.check_runtime(["sans-serif", "sans-serif|2|42"], "lookup", "sans-serif", mode=2)
        self.check_runtime(["Custom Family", "Custom Family|2|42"], "lookup", "Custom Family",
                           mode=2, whitelist="Custom Family")
        self.check_runtime(["<null>"], "lookup", "Arial", mode=2, whitelist="Custom Family")
        self.check_runtime(["Host Font", "Host Font|2|42"], "lookup", "Host Font",
                           mode=2, persona="linux")

    def test_file_and_last_resort_lookups_retain_native_backend(self):
        for family in ("Host Font", "sans-serif"):
            self.check_runtime([family, f"{family}|3|42"], "lookup", family, mode=3)
            self.check_runtime(["<file>", "<file>|0|42"], "lookup", family, mode=4)
            self.check_runtime([family, f"{family}|3|42"], "lookup", family,
                               mode=3, whitelist="Custom Family")

    def test_missing_substitute_and_backend_denial_are_not_synthesized(self):
        self.check_runtime(["sans-serif", "Arial|0|42", "sans-serif|0|42"],
                           "lookup", "sans-serif", missing="Arial")
        self.check_runtime(["<null>", "Arial|0|42", "sans-serif|0|42"],
                           "lookup", "sans-serif", denied=True)
        self.check_runtime(["<null>", "Arial|2|42"], "lookup", "Arial", mode=2, denied=True)
        self.check_runtime(["<null>", "Host Font|3|42"], "lookup", "Host Font", mode=3, denied=True)
        self.check_runtime(["<null>", "<file>|0|42"], "lookup", "Host Font", mode=4, denied=True)


if __name__ == "__main__":
    unittest.main()
