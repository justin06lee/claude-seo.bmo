"""Tests for the codebase SEO inventory scanner (seo_inventory.py)."""
import importlib
import json
import os
import sys

_SCRIPTS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "skills", "seo", "scripts")
sys.path.insert(0, _SCRIPTS)

inv = importlib.import_module("seo_inventory")


def write(tmp_path, rel, content=""):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return p


def test_detects_next_app_router(tmp_path):
    write(tmp_path, "package.json", json.dumps({"dependencies": {"next": "14.0.0"}}))
    write(tmp_path, "app/page.tsx", "export default function Home() {}")
    fw = inv.detect_framework(str(tmp_path))
    assert fw["framework"] == "next"
    assert fw["router"] == "app"


def test_detects_next_pages_router(tmp_path):
    write(tmp_path, "package.json", json.dumps({"dependencies": {"next": "13.0.0"}}))
    write(tmp_path, "pages/index.tsx", "export default function Home() {}")
    fw = inv.detect_framework(str(tmp_path))
    assert fw["framework"] == "next"
    assert fw["router"] == "pages"


def test_detects_astro(tmp_path):
    write(tmp_path, "astro.config.mjs", "export default {}")
    assert inv.detect_framework(str(tmp_path))["framework"] == "astro"


def test_detects_static_html(tmp_path):
    write(tmp_path, "index.html", "<html></html>")
    assert inv.detect_framework(str(tmp_path))["framework"] == "static-html"


def test_meta_framework_wins_over_view_lib(tmp_path):
    # A Next app also depends on react; it must be identified as next, not spa.
    write(tmp_path, "package.json",
          json.dumps({"dependencies": {"next": "14", "react": "18"}}))
    write(tmp_path, "app/page.tsx", "")
    assert inv.detect_framework(str(tmp_path))["framework"] == "next"


def test_enumerate_routes_next_app(tmp_path):
    write(tmp_path, "package.json", json.dumps({"dependencies": {"next": "14"}}))
    write(tmp_path, "app/page.tsx", "")
    write(tmp_path, "app/about/page.tsx", "")
    write(tmp_path, "app/blog/[slug]/page.tsx", "")
    write(tmp_path, "app/components/button.tsx", "")  # not a route
    fw = inv.detect_framework(str(tmp_path))
    routes = inv.enumerate_routes(str(tmp_path), fw)
    assert routes["count"] == 3


def test_walk_skips_node_modules(tmp_path):
    write(tmp_path, "index.html", "")
    # A dependency ships thousands of .html files; none should be counted.
    for i in range(5):
        write(tmp_path, f"node_modules/pkg/docs/page{i}.html", "")
    fw = inv.detect_framework(str(tmp_path))
    routes = inv.enumerate_routes(str(tmp_path), fw)
    assert routes["count"] == 1


def test_seo_surface_detects_existing_and_missing(tmp_path):
    write(tmp_path, "package.json", json.dumps({"dependencies": {"next": "14"}}))
    write(tmp_path, "public/robots.txt", "User-agent: *")
    write(tmp_path, "app/page.tsx",
          '<script type="application/ld+json">{}</script>'
          '<meta name="description" content="hi">')
    fw = inv.detect_framework(str(tmp_path))
    surface = inv.inventory_seo_surface(str(tmp_path), fw)
    assert surface["robots"] is True
    assert surface["json_ld"] is True
    assert surface["meta_description"] is True
    # None of these were added.
    assert surface["sitemap"] is False
    assert surface["hreflang"] is False


def test_sitemap_via_dependency(tmp_path):
    write(tmp_path, "package.json",
          json.dumps({"dependencies": {"next": "14"}, "devDependencies": {"next-sitemap": "4"}}))
    fw = inv.detect_framework(str(tmp_path))
    assert inv.inventory_seo_surface(str(tmp_path), fw)["sitemap"] is True


def test_next_sitemap_route_file(tmp_path):
    write(tmp_path, "package.json", json.dumps({"dependencies": {"next": "14"}}))
    write(tmp_path, "app/sitemap.ts", "export default function sitemap() {}")
    fw = inv.detect_framework(str(tmp_path))
    assert inv.inventory_seo_surface(str(tmp_path), fw)["sitemap"] is True


def test_infers_ecommerce(tmp_path):
    write(tmp_path, "app/products/[id]/page.tsx", "add to cart button; sku list")
    write(tmp_path, "app/cart/page.tsx", "checkout")
    business = inv.infer_business_type(str(tmp_path))
    assert business["primary"] == "ecommerce"
    assert business["scores"]["ecommerce"] >= 2


def test_infers_publisher(tmp_path):
    write(tmp_path, "blog/first-post.md", "---\nauthor: Jane\npublished: 2026\n---\nbyline")
    write(tmp_path, "articles/second.md", "content")
    business = inv.infer_business_type(str(tmp_path))
    assert business["primary"] == "publisher"


def test_general_when_no_signal(tmp_path):
    write(tmp_path, "index.html", "<html><body>hello</body></html>")
    business = inv.infer_business_type(str(tmp_path))
    assert business["primary"] == "general"


def test_build_inventory_full_shape(tmp_path):
    write(tmp_path, "package.json", json.dumps({"dependencies": {"next": "14"}}))
    write(tmp_path, "app/page.tsx", "")
    write(tmp_path, "app/pricing/page.tsx", "free trial sign up /features")
    result = inv.build_inventory(str(tmp_path))
    assert result["status"] == "ok"
    assert result["framework"]["framework"] == "next"
    assert result["routes"]["count"] == 2
    assert "sitemap" in result["surface_gaps"]  # nothing was added
    assert set(result.keys()) == {
        "status", "root", "framework", "routes", "seo_surface",
        "surface_gaps", "business_type",
    }


def test_build_inventory_rejects_non_directory(tmp_path):
    result = inv.build_inventory(str(tmp_path / "does-not-exist"))
    assert result["status"] == "error"


def test_main_emits_json(tmp_path, capsys):
    write(tmp_path, "index.html", "<html></html>")
    code = inv.main([str(tmp_path)])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["status"] == "ok"
    assert out["framework"]["framework"] == "static-html"
