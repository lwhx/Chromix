"""Standalone regressions for the merged renderer configuration templates."""
from pathlib import Path
import shutil
import subprocess

import pytest

REPO = Path(__file__).resolve().parents[2]


def added_source(name):
    return "\n".join(
        line[1:] for line in (REPO / "patches" / name).read_text().splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ) + "\n"


@pytest.fixture(scope="module")
def config_binary(tmp_path_factory):
    compiler = shutil.which("c++") or shutil.which("g++")
    if not compiler:
        pytest.skip("C++ compiler unavailable")
    root = tmp_path_factory.mktemp("merged-config")
    sources = {
        "base/uxr_config.cc": added_source("0002-base-uxr_config-cc.patch"),
        "base/uxr_config.h": added_source("0003-base-uxr_config-h.patch"),
        "base/base_export.h": "#pragma once\n#define BASE_EXPORT\n",
        "base/containers/flat_map.h": """#pragma once
#include <map>
namespace base {
template<class K, class V> using flat_map = std::map<K, V>;
}
""",
        "base/synchronization/lock.h": """#pragma once
#include <mutex>
namespace base {
using Lock = std::mutex;
using AutoLock = std::lock_guard<Lock>;
}
""",
        "base/no_destructor.h": """#pragma once
#include <new>
namespace base {
template<class T> class NoDestructor {
 public:
  NoDestructor() { new (storage_) T; }
  T& operator*() { return *reinterpret_cast<T*>(storage_); }
 private:
  alignas(T) unsigned char storage_[sizeof(T)];
};
}
""",
        "base/strings/string_number_conversions.h": """#pragma once
#include <charconv>
#include <cstdint>
#include <string>
namespace base {
template<class T> bool ParseNumber(const std::string& text, T* out) {
  auto parsed = std::from_chars(text.data(), text.data() + text.size(), *out);
  return parsed.ec == std::errc() && parsed.ptr == text.data() + text.size();
}
inline bool StringToInt(const std::string& text, int* out) {
  return ParseNumber(text, out);
}
inline bool StringToUint64(const std::string& text, uint64_t* out) {
  return ParseNumber(text, out);
}
inline bool StringToDouble(const std::string& text, double* out) {
  return ParseNumber(text, out);
}
}
""",
        "main.cc": """#include "base/uxr_config.h"
#include <iostream>
int main(int argc, char** argv) {
  auto& config = base::UxrConfig::GetInstance();
  if (argc > 2) {
    config.SetAll({{"typed", argv[2]}});
    if (std::string(argv[1]) == "uint64") {
      uint64_t value = 0;
      const bool valid = config.GetUint64("typed", &value);
      std::cout << valid << ' ' << value << '\\n';
    } else {
      double value = 0;
      const bool valid = config.GetDouble("typed", &value);
      std::cout << valid << ' ' << value << '\\n';
    }
    return 0;
  }
  if (argc > 1) config.SetAll({{"uxr-canvas-seed", argv[1]}});
  int width = 0, height = 0;
  float dpr = 0;
  const bool screen = config.GetSeededScreen(&width, &height, &dpr);
  std::cout << config.GetSeededHwConcurrency() << ' '
            << config.GetSeededDeviceMemory() << ' ' << screen << ' '
            << width << ' ' << height << ' ' << dpr << ' '
            << config.GetSeededTaskbarHeight() << '\\n';
}
""",
    }
    for name, text in sources.items():
        target = root / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
    binary = root / "config-test"
    subprocess.run(
        [compiler, "-std=c++20", "-Wall", "-Wextra", "-Werror", "-I", str(root),
         str(root / "base/uxr_config.cc"), str(root / "main.cc"), "-o", str(binary)],
        check=True, capture_output=True, text=True,
    )
    return binary


def read_config(binary, seed=None):
    args = [str(binary)] + ([] if seed is None else [seed])
    return subprocess.check_output(args, text=True).strip().split()


@pytest.mark.parametrize("seed", [None, "", "0", "-1", "abc", "12x", "18446744073709551616"])
def test_invalid_or_missing_seed_keeps_native_fallback(config_binary, seed):
    assert read_config(config_binary, seed) == ["0"] * 7


@pytest.mark.parametrize("seed", ["1", "4294967295", "4294967296", "1099511627776", "18446744073709551615"])
def test_full_64bit_canvas_seed_selects_stable_templates(config_binary, seed):
    result = read_config(config_binary, seed)
    assert result == read_config(config_binary, seed)
    cores, memory, screen, width, height, dpr, taskbar = result
    assert int(cores) in {2, 4, 6, 8, 12, 14, 16, 20, 24}
    assert float(memory) in {4.0, 8.0}
    assert screen == "1"
    assert (int(width), int(height), float(dpr)) in {
        (1920, 1080, 1.0), (1366, 768, 1.0), (2560, 1440, 1.0),
        (1536, 864, 1.25), (1440, 900, 1.0), (1680, 1050, 1.0),
        (1280, 720, 1.0), (1600, 900, 1.0), (1920, 1200, 1.0),
        (2560, 1440, 1.5), (1280, 800, 1.0),
    }
    assert int(taskbar) in {40, 48}


@pytest.mark.parametrize("kind, raw, valid, expected", [
    ("uint64", "18446744073709551615", True, "18446744073709551615"),
    ("uint64", "18446744073709551616", False, None),
    ("uint64", "-1", False, None),
    ("uint64", "42x", False, None),
    ("double", "1.25", True, "1.25"),
    ("double", "invalid", False, None),
    ("double", "1e999", False, None),
])
def test_typed_config_parsers(config_binary, kind, raw, valid, expected):
    result = subprocess.check_output([str(config_binary), kind, raw], text=True).split()
    assert result[0] == str(int(valid))
    if valid:
        assert result[1] == expected


def test_template_selection_uses_portable_integer_weights():
    source = added_source("0002-base-uxr_config-cc.patch")
    assert "StringToUint64" in source
    assert "std::discrete_distribution" not in source
    assert "rng() % total_weight" in source
