"""Tests for web_routes.py (stdlib only).

    python3 -m unittest discover -s scripts -p "test_web_routes.py"
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

import web_routes


def _dist(*files: str) -> Path:
    root = Path(tempfile.mkdtemp())
    for f in files:
        p = root / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("<html></html>")
    return root


class Locations(unittest.TestCase):
    def test_static_routes_need_nothing(self):
        dist = _dist("index.html", "sign-in.html", "trip/new.html", "offline/index.html", "+not-found.html")
        self.assertEqual(web_routes.locations(dist), [])

    def test_param(self):
        dist = _dist("trip/[id].html", "trip/new.html")
        self.assertEqual(
            web_routes.locations(dist),
            ['location ~ "^/trip/[^/]+/?$" { try_files $uri $uri.html $uri/index.html "/trip/[id].html"; }'],
        )

    def test_param_index_and_nested(self):
        dist = _dist("trip/[id]/index.html", "trip/[id]/edit.html")
        self.assertEqual(
            web_routes.locations(dist),
            [
                'location ~ "^/trip/[^/]+/edit/?$" { try_files $uri $uri.html $uri/index.html "/trip/[id]/edit.html"; }',
                'location ~ "^/trip/[^/]+/?$" { try_files $uri $uri.html $uri/index.html "/trip/[id]/index.html"; }',
            ],
        )

    def test_catch_all_comes_after_params(self):
        dist = _dist("docs/[...rest].html", "docs/[page].html")
        out = web_routes.locations(dist)
        self.assertEqual(len(out), 2)
        self.assertIn('"^/docs/[^/]+/?$"', out[0])
        self.assertIn('"^/docs/.+/?$"', out[1])

    def test_groups_and_expo_files_skipped(self):
        dist = _dist("(tabs)/[id].html", "_expo/static/x/[id].html")
        self.assertEqual(web_routes.locations(dist), [])

    def test_static_segments_escaped(self):
        dist = _dist("a.b/[id].html")
        self.assertIn(r'"^/a\.b/[^/]+/?$"', web_routes.locations(dist)[0])

    def test_unsafe_names_refused(self):
        for bad in ('a"b/[id].html', "a b/[id].html", "a$b/[id].html", "a;b/[id].html"):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                web_routes.locations(_dist(bad))


if __name__ == "__main__":
    unittest.main()
