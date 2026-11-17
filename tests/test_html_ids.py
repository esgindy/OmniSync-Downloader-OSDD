"""Playwright-free smoke test: every $("id") used in JS exists as an id in index.html."""
from __future__ import annotations

import re
import unittest
from pathlib import Path

HTML = Path(__file__).resolve().parent.parent / "static" / "index.html"


class TestIndexHtmlIds(unittest.TestCase):
    def test_all_dollar_ids_exist(self):
        text = HTML.read_text(encoding="utf-8")
        html_ids = set(re.findall(r"""\bid\s*=\s*["']([^"']+)["']""", text))
        used = set(re.findall(r"""\$\(\s*["']([A-Za-z_][\w\-]*)["']\s*\)""", text))
        self.assertTrue(used, "no $(\"id\") usages found")
        # ids created dynamically inside JS strings/templates also appear as id="..." in text
        missing = sorted(used - html_ids)
        self.assertEqual(missing, [], f"ids used in JS but absent from HTML: {missing}")


if __name__ == "__main__":
    unittest.main()
