"""Compare tool-build inputs against pinned forward domain transformations."""
from pathlib import Path
import re

# Exact endpoint restoration in portablelinux scripts/shared.sh at the pinned commit.
RESTORED = {
    "tools/clang/scripts/update.py": ("storage",),
    "tools/clang/scripts/build.py": ("storage", "source"),
    "tools/rust/build_rust.py": ("source", "packages"),
    "tools/rust/build_bindgen.py": ("source",),
}
ENDPOINTS = {
    "storage": ("commondatastorage.9oo91eapis.qjz9zk", "commondatastorage.googleapis.com"),
    "source": ("chromium.9oo91esource.qjz9zk", "chromium.googlesource.com"),
    "packages": ("chrome-infra-packages.8pp2p8t.qjz9zk", "chrome-infra-packages.appspot.com"),
}


class ScriptIdentity:
    def __init__(self, core: Path, platform: str):
        self.platform = platform
        self.paths = set((core / "domain_substitution.list").read_text().splitlines())
        self.rules = []
        for line in (core / "domain_regex.list").read_text().splitlines():
            if line:
                pattern, replacement = line.split("#")
                self.rules.append((re.compile(pattern), replacement))

    def expected(self, relative: str, data: bytes) -> bytes:
        if relative not in self.paths:
            return data
        try:
            text = data.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            text = data.decode("iso-8859-1")
            encoding = "iso-8859-1"
        for pattern, replacement in self.rules:
            text = pattern.sub(replacement, text)
        if self.platform == "linux":
            for key in RESTORED.get(relative, ()):
                text = text.replace(*ENDPOINTS[key])
        return text.encode(encoding)

    def matches(self, relative: str, canonical: bytes, donor: bytes) -> bool:
        return donor == canonical or donor == self.expected(relative, canonical)
