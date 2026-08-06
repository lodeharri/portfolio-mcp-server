"""Unit tests for the static asset mount served by the web router.

The web router mounts ``playground/static/`` at ``/static/`` and sets
``Cache-Control: public, max-age=31536000, immutable`` so the vendored
HTMX and CSS files cache across page loads. These tests pin:

* ``/static/htmx.min.js`` returns 200, the vendored bytes (sha256
  asserted against the on-disk file), the embedded version string,
  no CDN reference, and the immutable cache header.
* ``/static/style.css`` returns 200, contains every one of the 16
  canonical Solarized Phosphor tokens under :root, references the
  canonical hex values, and carries the same immutable cache header.
* An unknown subpath under ``/static/`` returns 404 (we don't leak
  the directory tree).

Per change 003-playground-ui tasks 1.2.2, 1.3.2.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

PLAYGROUND_STATIC_DIR = Path(__file__).resolve().parents[5] / "playground" / "static"

# Canonical Solarized Phosphor hex values from ethanschoonover.com.
# These are the 8 base tones + 8 accent colors referenced in the
# ``playground-ui`` spec, ``Solarized Phosphor Palette`` requirement.
SOLARIZED_BASE_HEXES: tuple[str, ...] = (
    "#002b36",  # base03
    "#073642",  # base02
    "#586e75",  # base01
    "#657b83",  # base00
    "#839496",  # base0
    "#93a1a1",  # base1
    "#eee8d5",  # base2
    "#fdf6e3",  # base3
)

SOLARIZED_ACCENT_HEXES: tuple[str, ...] = (
    "#b58900",  # yellow
    "#cb4b16",  # orange
    "#dc322f",  # red
    "#d33682",  # magenta
    "#6c71c4",  # violet
    "#268bd2",  # blue
    "#2aa198",  # cyan
    "#859900",  # green
)

SOLARIZED_ALL_HEXES: tuple[str, ...] = SOLARIZED_BASE_HEXES + SOLARIZED_ACCENT_HEXES


@pytest.fixture(scope="module")
def web_app():
    """Build a FastAPI app with the web router mounted.

    Uses the production ``create_app()`` factory so the wiring matches
    what ``uvicorn mcp_server.app:app`` exposes.
    """
    from mcp_server.app import create_app
    from mcp_server.config import AppConfig

    app = create_app(AppConfig(gemini_api_key=""))
    return app


@pytest.fixture(scope="module")
def client(web_app):
    from fastapi.testclient import TestClient

    return TestClient(web_app)


# ---------------------------------------------------------------------------
# Vendored HTMX
# ---------------------------------------------------------------------------


class TestVendoredHtmxAsset:
    def test_htmx_endpoint_returns_200(self, client: object) -> None:
        """``/static/htmx.min.js`` MUST return 200 with the vendored bytes."""
        response = client.get("/static/htmx.min.js")  # type: ignore[attr-defined]
        assert response.status_code == 200

    def test_htmx_endpoint_returns_vendored_bytes(self, client: object) -> None:
        """The response body MUST match the file committed to the repo
        (byte-for-byte) — any CDN fetch or transform would change the sha.
        """
        response = client.get("/static/htmx.min.js")  # type: ignore[attr-defined]
        body = response.content
        on_disk = PLAYGROUND_STATIC_DIR.joinpath("htmx.min.js").read_bytes()
        assert hashlib.sha256(body).hexdigest() == hashlib.sha256(on_disk).hexdigest(), (
            "Vendored HTMX byte mismatch — the served asset drifted from "
            "the file committed to the repo. Re-run the download."
        )

    def test_htmx_minified_is_substantial(self, client: object) -> None:
        """The vendored file is non-trivial — htmx 1.9.10 is ~48 KB
        minified (spec proposal quoted 14 KB which is the gzipped size;
        the on-disk archive is the uncompressed minified build per
        Decision #1, no transform).
        """
        on_disk = PLAYGROUND_STATIC_DIR.joinpath("htmx.min.js")
        size = on_disk.stat().st_size
        assert size > 10_000, (
            f"Vendored htmx is suspiciously small ({size} bytes) — did the "
            "download succeed? The 1.9.10 minified file is ~48 KB."
        )

    def test_htmx_contains_version_marker(self) -> None:
        """The vendored file MUST contain the embedded ``1.9.10`` string.

        htmx 1.x bundled with extensions does NOT ship a legacy
        ``/* htmx.org */`` banner; the canonical version marker is the
        ``version:"1.9.10"`` literal at byte ~1534.
        """
        text = PLAYGROUND_STATIC_DIR.joinpath("htmx.min.js").read_bytes().decode("utf-8")
        assert "1.9.10" in text, "Vendored HTMX must contain the embedded version string 1.9.10"

    def test_htmx_no_cdn_reference(self) -> None:
        """The vendored HTMX MUST NOT reference any external CDN.

        Decision #1: zero CDN at page load. Inspect the asset bytes for
        forbidden hostnames.
        """
        text = PLAYGROUND_STATIC_DIR.joinpath("htmx.min.js").read_text()
        for forbidden in ("unpkg.com", "jsdelivr.net", "cdnjs.cloudflare.com"):
            assert forbidden not in text, f"Vendored HTMX references forbidden CDN {forbidden!r}"

    def test_htmx_carries_immutable_cache_header(self, client: object) -> None:
        """The static mount MUST set ``Cache-Control: public, max-age=31536000,
        immutable`` so the vendored HTMX is cached across page loads.
        """
        response = client.get("/static/htmx.min.js")  # type: ignore[attr-defined]
        cache_control = response.headers.get("cache-control", "")
        assert "public" in cache_control
        assert "max-age=31536000" in cache_control
        assert "immutable" in cache_control

    def test_base_template_loads_htmx_with_sri_integrity(self, web_app) -> None:
        """REL-10: the HTMX ``<script>`` tag MUST carry an SRI integrity attribute.

        A future HTMX security advisory would otherwise let the
        browser serve a cached vulnerable copy forever. The
        ``integrity`` + ``crossorigin`` attributes force the browser
        to revalidate the asset against the SHA-384 hash whenever the
        tag is parsed.

        We assert the substring is present in the rendered template
        (and the attribute value matches the on-disk sha384 of the
        vendored file).
        """
        import base64
        import hashlib as _hl
        import re

        from starlette.testclient import TestClient

        with TestClient(web_app) as client:
            html = client.get("/").text

        # The script tag MUST carry an integrity attribute (sha384
        # preferred — sha512 wasn't ratified in any browser at the
        # time of writing).
        assert 'integrity="sha384-' in html, (
            "base.html script tag must declare SRI sha384 integrity"
        )
        assert 'crossorigin="anonymous"' in html, (
            "base.html script tag must declare crossorigin=anonymous for SRI"
        )

        # Extract the declared sha384 digest and assert it matches the
        # on-disk file. If anyone replaces the vendored HTMX the hash
        # MUST be updated alongside it.
        match = re.search(r'integrity="sha384-([A-Za-z0-9+/=]+)"', html)
        assert match is not None, "SRI integrity attribute not found in base.html"
        declared_b64 = match.group(1)
        declared_bytes = base64.b64decode(declared_b64)

        on_disk = PLAYGROUND_STATIC_DIR.joinpath("htmx.min.js").read_bytes()
        computed = _hl.sha384(on_disk).digest()
        assert declared_bytes == computed, (
            "SRI integrity hash in base.html is stale relative to the "
            "vendored htmx.min.js; regenerate via "
            "``base64.b64encode(hashlib.sha384(open("
            "'playground/static/htmx.min.js','rb').read()).digest()).decode()``"
        )


# ---------------------------------------------------------------------------
# Solarized Phosphor style sheet
# ---------------------------------------------------------------------------


class TestSolarizedPhosphorStyle:
    def test_style_css_endpoint_returns_200(self, client: object) -> None:
        response = client.get("/static/style.css")  # type: ignore[attr-defined]
        assert response.status_code == 200

    def test_style_css_references_all_16_tokens(self, client: object) -> None:
        """``/static/style.css`` MUST declare all 16 canonical tokens
        under :root (8 base tones + 8 accent colors). Each token name
        must appear in the file.
        """
        response = client.get("/static/style.css")  # type: ignore[attr-defined]
        text = response.text
        for token in (
            "--solar-base03",
            "--solar-base02",
            "--solar-base01",
            "--solar-base00",
            "--solar-base0",
            "--solar-base1",
            "--solar-base2",
            "--solar-base3",
            "--solar-yellow",
            "--solar-orange",
            "--solar-red",
            "--solar-magenta",
            "--solar-violet",
            "--solar-blue",
            "--solar-cyan",
            "--solar-green",
        ):
            needle = f"{token}:"
            assert needle in text, f"style.css must declare CSS custom property {needle}"

    def test_style_css_uses_canonical_hex_values(self, client: object) -> None:
        """Each canonical hex value from ethanschoonover.com MUST be
        present in the style sheet at least once.
        """
        response = client.get("/static/style.css")  # type: ignore[attr-defined]
        text = response.text
        for hex_value in SOLARIZED_ALL_HEXES:
            assert hex_value in text, (
                f"style.css must contain the canonical Solarized Phosphor hex {hex_value}"
            )

    def test_style_css_under_root_selector(self, client: object) -> None:
        """All 16 tokens MUST live under a single ``:root { ... }`` block.
        Spec requirement: Solarized Phosphor palette defined via CSS
        custom properties under :root.
        """
        response = client.get("/static/style.css")  # type: ignore[attr-defined]
        text = response.text
        # Find the :root block
        match = re.search(r":root\s*\{([^}]*)\}", text, flags=re.MULTILINE | re.DOTALL)
        assert match, "style.css must define a :root { ... } block"
        body = match.group(1)
        for hex_value in SOLARIZED_ALL_HEXES:
            assert hex_value in body, (
                f"canonical Solarized Phosphor hex {hex_value} must appear inside :root"
            )

    def test_style_css_no_external_stylesheet_dependency(self, client: object) -> None:
        """The style sheet MUST NOT @import or url() any external CSS — no
        CDN fallback (Decision #1, applies to every asset).
        """
        response = client.get("/static/style.css")  # type: ignore[attr-defined]
        text = response.text
        for forbidden in ("@import", "url(http", "url('http", 'url("http'):
            assert forbidden not in text, (
                f"style.css references forbidden external resource {forbidden!r}"
            )

    def test_style_css_carries_immutable_cache_header(self, client: object) -> None:
        """The style sheet MUST be served with the same immutable
        Cache-Control as the vendored HTMX so the browser caches it.
        """
        response = client.get("/static/style.css")  # type: ignore[attr-defined]
        cache_control = response.headers.get("cache-control", "")
        assert "public" in cache_control
        assert "max-age=31536000" in cache_control
        assert "immutable" in cache_control


# ---------------------------------------------------------------------------
# Security: unknown subpaths
# ---------------------------------------------------------------------------


class TestStaticMountSecurity:
    def test_unknown_static_subpath_is_404(self, client: object) -> None:
        """Unknown static files MUST return 404 — no directory listing,
        no guessing paths.
        """
        response = client.get("/static/this-file-does-not-exist.txt")  # type: ignore[attr-defined]
        assert response.status_code == 404

    def test_static_path_traversal_is_404(self, client: object) -> None:
        """Path traversal under ``/static/`` MUST NOT escape the
        playground/static directory. Starlette's StaticFiles blocks
        ``..`` traversal by design.
        """
        response = client.get("/static/../app.py")  # type: ignore[attr-defined]
        # Starlette returns 404 or 400 depending on parsing; both are
        # acceptable as long as the file is NOT served.
        assert response.status_code in (400, 404)
        assert b"create_app" not in response.content
