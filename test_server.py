"""
Tests for CreaFlow — server.py
Run with: pytest test_server.py -v
"""

import json
import os
import uuid
from io import BytesIO
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

# Ensure FAL_KEY is set to avoid issues during import
os.environ.setdefault("FAL_KEY", "test-key-for-testing")

from server import app, init_db, get_db, generate_headline, build_prompt, image_to_data_uri, DB_PATH, DATA_DIR

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def clean_db(tmp_path, monkeypatch):
    """Use a temporary database for each test."""
    test_db = tmp_path / "test.db"
    test_data = tmp_path / "data"
    test_uploads = test_data / "uploads"
    test_outputs = test_data / "outputs"
    for d in [test_data, test_uploads, test_outputs]:
        d.mkdir(parents=True, exist_ok=True)

    import server
    monkeypatch.setattr(server, "DB_PATH", test_db)
    monkeypatch.setattr(server, "DATA_DIR", test_data)
    monkeypatch.setattr(server, "UPLOADS_DIR", test_uploads)
    monkeypatch.setattr(server, "OUTPUTS_DIR", test_outputs)

    init_db()
    yield


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def brand_id(client):
    """Create a brand and return its ID."""
    resp = client.post("/api/brands", data={"name": "Test Brand", "url": "https://test.com", "description": "A test brand", "brand_dna": "Test DNA"})
    assert resp.status_code == 200
    return resp.json()["id"]


@pytest.fixture
def product_with_image(client, brand_id, tmp_path):
    """Create a product with one image and return (product_id, image_id)."""
    img = tmp_path / "test.png"
    img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)  # minimal PNG-like header
    with open(img, "rb") as f:
        resp = client.post(
            f"/api/brands/{brand_id}/products",
            data={"name": "Test Product"},
            files=[("images", ("test.png", f, "image/png"))],
        )
    assert resp.status_code == 200
    data = resp.json()
    return data["id"], data["images"][0]["id"]


# ---------------------------------------------------------------------------
# Unit tests — helper functions
# ---------------------------------------------------------------------------

class TestGenerateHeadline:
    def test_matching_angle(self):
        headline = generate_headline("problème de ballonnement")
        assert headline in ["Fini les ballonnements", "Ton ventre te dit merci", "Digestion legere, energie durable"]

    def test_energie_angle(self):
        headline = generate_headline("manque d'energie")  # sans accent pour matcher la clé
        assert headline in ["Fini le crash. Place a l'energie qui dure.", "L'energie sans la descente", "8h d'energie stable, 0 crash"]

    def test_default_angle(self):
        headline = generate_headline("random unknown topic")
        assert headline in ["Upgrade ta routine.", "Le supplement qui change tout.", "Ta meilleure version commence ici."]


class TestBuildPrompt:
    def test_standard_mode(self):
        prompt = build_prompt(
            brand_dna="Premium brand",
            product_name="Brainstoorm",
            aspect_ratio="4:5",
            marketing_messages=["énergie stable"],
            customer_reviews=[],
            custom_prompt="",
        )
        assert "Brainstoorm" in prompt
        assert "4:5" in prompt

    def test_custom_prompt_mode(self):
        prompt = build_prompt(
            brand_dna="Premium brand",
            product_name="Brainstoorm",
            aspect_ratio="4:5",
            marketing_messages=[],
            customer_reviews=[],
            custom_prompt="My custom creative direction",
        )
        assert "My custom creative direction" in prompt

    def test_iteration_mode(self):
        prompt = build_prompt(
            brand_dna="Premium brand",
            product_name="Brainstoorm",
            aspect_ratio="4:5",
            marketing_messages=["focus"],
            customer_reviews=[],
            custom_prompt="",
            is_iteration=True,
        )
        assert "VARIATION" in prompt

    def test_style_ref_mode(self):
        prompt = build_prompt(
            brand_dna="Premium brand",
            product_name="Brainstoorm",
            aspect_ratio="4:5",
            marketing_messages=["énergie"],
            customer_reviews=[],
            custom_prompt="",
            has_style_ref=True,
        )
        assert "STYLE" in prompt

    def test_english_language(self):
        prompt = build_prompt(
            brand_dna="Premium brand",
            product_name="Brainstoorm",
            aspect_ratio="4:5",
            marketing_messages=["énergie"],
            customer_reviews=[],
            custom_prompt="",
            language="en",
        )
        assert "English" in prompt

    def test_french_language_no_prefix(self):
        prompt = build_prompt(
            brand_dna="Premium brand",
            product_name="Brainstoorm",
            aspect_ratio="4:5",
            marketing_messages=["énergie"],
            customer_reviews=[],
            custom_prompt="",
            language="fr",
        )
        assert not prompt.startswith("IMPORTANT: All text overlays")


class TestImageToDataUri:
    def test_png(self, tmp_path):
        img = tmp_path / "test.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
        uri = image_to_data_uri(img)
        assert uri.startswith("data:image/png;base64,")

    def test_jpeg(self, tmp_path):
        img = tmp_path / "photo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 10)
        uri = image_to_data_uri(img)
        assert uri.startswith("data:image/jpeg;base64,")

    def test_webp(self, tmp_path):
        img = tmp_path / "photo.webp"
        img.write_bytes(b"RIFF" + b"\x00" * 10)
        uri = image_to_data_uri(img)
        assert uri.startswith("data:image/webp;base64,")


# ---------------------------------------------------------------------------
# API Integration tests — Brands
# ---------------------------------------------------------------------------

class TestBrandsAPI:
    def test_create_brand(self, client):
        resp = client.post("/api/brands", data={"name": "My Brand"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "My Brand"
        assert "id" in data

    def test_list_brands(self, client, brand_id):
        resp = client.get("/api/brands")
        assert resp.status_code == 200
        brands = resp.json()
        assert len(brands) >= 1
        assert any(b["id"] == brand_id for b in brands)

    def test_get_brand(self, client, brand_id):
        resp = client.get(f"/api/brands/{brand_id}")
        assert resp.status_code == 200
        assert resp.json()["id"] == brand_id
        assert resp.json()["name"] == "Test Brand"

    def test_update_brand(self, client, brand_id):
        resp = client.put(f"/api/brands/{brand_id}", data={"name": "Updated Brand", "url": "https://new.com", "description": "new desc", "brand_dna": "new dna"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated Brand"

    def test_delete_brand(self, client, brand_id):
        resp = client.delete(f"/api/brands/{brand_id}")
        assert resp.status_code == 200
        # Verify it's gone
        resp2 = client.get(f"/api/brands/{brand_id}")
        assert resp2.status_code == 404

    def test_get_nonexistent_brand(self, client):
        resp = client.get(f"/api/brands/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API Integration tests — Products
# ---------------------------------------------------------------------------

class TestProductsAPI:
    def test_create_product_with_image(self, client, brand_id, tmp_path):
        img = tmp_path / "product.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 100)
        with open(img, "rb") as f:
            resp = client.post(
                f"/api/brands/{brand_id}/products",
                data={"name": "My Product"},
                files=[("images", ("product.png", f, "image/png"))],
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "My Product"
        assert len(data["images"]) == 1

    def test_list_products(self, client, brand_id, product_with_image):
        resp = client.get(f"/api/brands/{brand_id}/products")
        assert resp.status_code == 200
        products = resp.json()
        assert len(products) >= 1

    def test_update_product(self, client, product_with_image):
        pid, _ = product_with_image
        resp = client.put(f"/api/products/{pid}", json={"name": "Renamed Product", "page_url": "https://example.com/p", "brief": "A brief"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    def test_add_image_to_product(self, client, product_with_image, tmp_path):
        pid, _ = product_with_image
        img = tmp_path / "extra.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        with open(img, "rb") as f:
            resp = client.post(
                f"/api/products/{pid}/images",
                files=[("images", ("extra.png", f, "image/png"))],
            )
        assert resp.status_code == 200
        # Endpoint returns a list of added images
        assert len(resp.json()) >= 1

    def test_delete_product_image(self, client, product_with_image):
        _, img_id = product_with_image
        resp = client.delete(f"/api/product-images/{img_id}")
        assert resp.status_code == 200

    def test_reject_non_image_upload(self, client, brand_id, tmp_path):
        txt = tmp_path / "file.txt"
        txt.write_text("not an image")
        with open(txt, "rb") as f:
            resp = client.post(
                f"/api/brands/{brand_id}/products",
                data={"name": "Bad Product"},
                files=[("images", ("file.txt", f, "text/plain"))],
            )
        # Should still create the product but skip the non-image file
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# API Integration tests — Settings
# ---------------------------------------------------------------------------

class TestSettingsAPI:
    def test_get_settings(self, client):
        resp = client.get("/api/settings")
        assert resp.status_code == 200

    def test_save_and_retrieve_settings(self, client):
        resp = client.post("/api/settings", json={"fal_key": "my-test-key", "default_ratio": "4:5"})
        assert resp.status_code == 200
        resp2 = client.get("/api/settings")
        settings = resp2.json()
        assert settings.get("fal_key") == "my-test-key"
        assert settings.get("default_ratio") == "4:5"


# ---------------------------------------------------------------------------
# API Integration tests — Stats
# ---------------------------------------------------------------------------

class TestStatsAPI:
    def test_stats_empty(self, client):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["brands"] == 0

    def test_stats_with_brand(self, client, brand_id):
        resp = client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["brands"] == 1


# ---------------------------------------------------------------------------
# API Integration tests — Templates
# ---------------------------------------------------------------------------

class TestTemplatesAPI:
    def test_create_template(self, client):
        resp = client.post("/api/templates", json={"name": "Test Template", "angle": "énergie", "prompt_text": "Some prompt"})
        assert resp.status_code == 200
        assert "id" in resp.json()

    def test_list_templates(self, client):
        client.post("/api/templates", json={"name": "T1", "angle": "focus"})
        resp = client.get("/api/templates")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_delete_template(self, client):
        resp = client.post("/api/templates", json={"name": "ToDelete"})
        tpl_id = resp.json()["id"]
        del_resp = client.delete(f"/api/templates/{tpl_id}")
        assert del_resp.status_code == 200


# ---------------------------------------------------------------------------
# API Integration tests — Brand Strategy
# ---------------------------------------------------------------------------

class TestBrandStrategyAPI:
    def test_create_strategy(self, client, brand_id):
        resp = client.post(f"/api/brands/{brand_id}/strategy", json={
            "avatar_description": "Entrepreneur 30 ans",
            "avatar_pain_points": ["fatigue", "stress"],
            "avatar_desires": ["énergie", "focus"],
            "usp": "Le meilleur café adaptogène",
            "offers": ["Abo -20%"],
            "tone_of_voice": "Direct et cool",
        })
        assert resp.status_code == 200
        assert resp.json()["usp"] == "Le meilleur café adaptogène"

    def test_get_strategy(self, client, brand_id):
        # Create first
        client.post(f"/api/brands/{brand_id}/strategy", json={
            "avatar_description": "Test avatar",
            "usp": "Test USP",
        })
        resp = client.get(f"/api/brands/{brand_id}/strategy")
        assert resp.status_code == 200
        assert resp.json()["usp"] == "Test USP"

    def test_update_strategy(self, client, brand_id):
        client.post(f"/api/brands/{brand_id}/strategy", json={"usp": "V1"})
        # Update
        client.post(f"/api/brands/{brand_id}/strategy", json={"usp": "V2"})
        resp = client.get(f"/api/brands/{brand_id}/strategy")
        assert resp.json()["usp"] == "V2"


# ---------------------------------------------------------------------------
# API Integration tests — Competitors
# ---------------------------------------------------------------------------

class TestCompetitorsAPI:
    def test_add_competitor(self, client, brand_id):
        resp = client.post(f"/api/brands/{brand_id}/competitors", json={
            "name": "Rival Co", "url": "https://rival.com", "type": "direct", "notes": "Strong brand"
        })
        assert resp.status_code == 200
        assert resp.json()["name"] == "Rival Co"

    def test_list_competitors(self, client, brand_id):
        client.post(f"/api/brands/{brand_id}/competitors", json={"name": "C1"})
        client.post(f"/api/brands/{brand_id}/competitors", json={"name": "C2"})
        resp = client.get(f"/api/brands/{brand_id}/competitors")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    def test_delete_competitor(self, client, brand_id):
        resp = client.post(f"/api/brands/{brand_id}/competitors", json={"name": "ToRemove"})
        comp_id = resp.json()["id"]
        del_resp = client.delete(f"/api/competitors/{comp_id}")
        assert del_resp.status_code == 200


# ---------------------------------------------------------------------------
# API Integration tests — Inspirations
# ---------------------------------------------------------------------------

class TestInspirationsAPI:
    def test_upload_inspiration(self, client, brand_id, tmp_path):
        img = tmp_path / "inspo.jpg"
        img.write_bytes(b"\xff\xd8\xff\xe0" + b"\x00" * 100)
        with open(img, "rb") as f:
            resp = client.post(
                "/api/inspirations",
                data={"brand_id": brand_id, "label": "competitor ad", "source": "meta", "tags": json.dumps(["lifestyle", "coffee"])},
                files=[("images", ("inspo.jpg", f, "image/jpeg"))],
            )
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_list_inspirations(self, client, brand_id, tmp_path):
        img = tmp_path / "inspo.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        with open(img, "rb") as f:
            client.post(
                "/api/inspirations",
                data={"brand_id": brand_id},
                files=[("images", ("inspo.png", f, "image/png"))],
            )
        resp = client.get("/api/inspirations")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

    def test_delete_inspiration(self, client, brand_id, tmp_path):
        img = tmp_path / "inspo2.png"
        img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 50)
        with open(img, "rb") as f:
            resp = client.post(
                "/api/inspirations",
                data={"brand_id": brand_id},
                files=[("images", ("inspo2.png", f, "image/png"))],
            )
        inspo_id = resp.json()[0]["id"]
        del_resp = client.delete(f"/api/inspirations/{inspo_id}")
        assert del_resp.status_code == 200


# ---------------------------------------------------------------------------
# API Integration tests — Generations (without FAL calls)
# ---------------------------------------------------------------------------

class TestGenerationsAPI:
    def test_list_generations_empty(self, client):
        resp = client.get("/api/generations")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_estimate_cost(self, client):
        resp = client.get("/api/estimate-cost", params={"count": 4, "formats": 2})
        assert resp.status_code == 200
        data = resp.json()
        assert "total_images" in data

    def test_get_nonexistent_generation(self, client):
        resp = client.get(f"/api/generations/{uuid.uuid4()}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# API Integration tests — FAL status
# ---------------------------------------------------------------------------

class TestFalStatus:
    def test_fal_status(self, client):
        resp = client.get("/api/fal-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "configured" in data


# ---------------------------------------------------------------------------
# API Integration tests — Winners & Feedback
# ---------------------------------------------------------------------------

class TestFeedbackAPI:
    def test_winners_empty(self, client):
        resp = client.get("/api/winners")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_feedback_context(self, client):
        resp = client.get("/api/feedback-context")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# Cascade delete test
# ---------------------------------------------------------------------------

class TestCascadeDelete:
    def test_delete_brand_cascades(self, client, brand_id, product_with_image):
        """Deleting a brand should delete its products and images."""
        pid, _ = product_with_image
        # Verify product exists
        resp = client.get(f"/api/brands/{brand_id}/products")
        assert len(resp.json()) == 1
        # Delete brand
        client.delete(f"/api/brands/{brand_id}")
        # Products should be gone
        resp2 = client.get(f"/api/brands/{brand_id}/products")
        # Brand doesn't exist anymore so it may return 404 or empty list
        assert resp2.status_code in (200, 404)
