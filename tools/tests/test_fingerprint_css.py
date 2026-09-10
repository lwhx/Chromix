"""CSS pointer/hover merge regressions against Chromium 152 enum contracts."""
from pathlib import Path
import re
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[2]
PATCH = ROOT / "patches/0052-third_party-blink-renderer-core-css-media_values-cc.patch"


def test_hunks_follow_source_order():
    starts = [int(value) for value in re.findall(r"^@@ -(\d+)", PATCH.read_text(), re.M)]
    assert starts == sorted(starts)


def test_pointer_hover_enum_and_native_fallback_contract(tmp_path):
    compiler = shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler unavailable")
    additions = "\n".join(line[1:] for line in PATCH.read_text().splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
    contracts = [
        ("pointer", "persona primary pointer.", "PointerType", "GetPrimaryPointerType"),
        ("pointers", "keep any-pointer coherent", "int", "GetAvailablePointerTypes"),
        ("hover", "persona primary hover", "HoverType", "GetPrimaryHoverType"),
        ("hovers", "keep any-hover coherent", "int", "GetAvailableHoverTypes"),
    ]
    source = SUPPORT
    for name, marker, result, native in contracts:
        start = additions.rfind("  {", 0, additions.index(marker))
        end = additions.index("\n  }", start) + len("\n  }")
        source += f"\n{result} {name}() {{\n" + additions[start:end] + f"\nreturn {native}();\n}}\n"
    source += TESTS
    path = tmp_path / "css-contract.cc"
    path.write_text(source)
    binary = tmp_path / "css-contract"
    subprocess.run([compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror",
                    str(path), "-o", str(binary)], check=True, capture_output=True, text=True)
    subprocess.run([str(binary)], check=True, capture_output=True, text=True)


SUPPORT = r'''
#include <algorithm>
#include <cassert>
#include <cctype>
#include <map>
#include <string>
// Values and names from Chromium 152 web_preferences.mojom.
namespace mojom::blink {
enum class PointerType { kPointerNone = 1, kPointerCoarseType = 2, kPointerFineType = 4 };
enum class HoverType { kHoverNone = 1, kHoverHoverType = 2 };
}
using mojom::blink::PointerType;
using mojom::blink::HoverType;
namespace base {
struct UxrConfig {
  std::map<std::string, std::string> values;
  static UxrConfig& GetInstance() { static UxrConfig value; return value; }
  std::string Get(const std::string& key) const {
    const auto it = values.find(key);
    return it == values.end() ? "" : it->second;
  }
};
bool EqualsCaseInsensitiveASCII(const std::string& a, const std::string& b) {
  return a.size() == b.size() && std::equal(a.begin(), a.end(), b.begin(),
    [](unsigned char x, unsigned char y) { return std::tolower(x) == std::tolower(y); });
}
}
PointerType GetPrimaryPointerType() { return PointerType::kPointerCoarseType; }
int GetAvailablePointerTypes() { return 6; }
HoverType GetPrimaryHoverType() { return HoverType::kHoverNone; }
int GetAvailableHoverTypes() { return 3; }
'''

TESTS = r'''
int main() {
  auto& values = base::UxrConfig::GetInstance().values;
  for (const std::string platform : {"", "linux", "MacIntel", "unknown"}) {
    values = {{"uxr-platform", platform}};
    assert(pointer() == PointerType::kPointerCoarseType && pointers() == 6);
    assert(hover() == HoverType::kHoverNone && hovers() == 3);
  }
  for (const std::string platform : {"windows", "Win32", "WINDOWS"}) {
    values = {{"uxr-platform", platform}};
    assert(pointer() == PointerType::kPointerFineType && pointers() == 4);
    assert(hover() == HoverType::kHoverHoverType && hovers() == 2);
    values["uxr-pointer"] = "coarse";
    values["uxr-hover"] = "none";
    assert(pointer() == PointerType::kPointerCoarseType && pointers() == 2);
    assert(hover() == HoverType::kHoverNone && hovers() == 1);
  }
  for (const std::string platform : {"windows", "linux"}) {
    values = {{"uxr-platform", platform}, {"uxr-pointer", "none"}, {"uxr-hover", "hover"}};
    assert(pointer() == PointerType::kPointerNone && pointers() == 1);
    assert(hover() == HoverType::kHoverHoverType && hovers() == 2);
    values["uxr-pointer"] = "fine";
    assert(pointer() == PointerType::kPointerFineType && pointers() == 4);
  }
}
'''
