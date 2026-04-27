#!/usr/bin/env python3
"""
CreaFlow — Local Ad Creative Generation Tool
FastAPI backend with FAL API integration
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import sqlite3
import time
import uuid
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from threading import Thread
from typing import List, Optional

import requests
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).parent
DATA_DIR = BASE_DIR / "data"
UPLOADS_DIR = DATA_DIR / "uploads"
OUTPUTS_DIR = DATA_DIR / "outputs"
DB_PATH = DATA_DIR / "creaflow.db"

# Try env var first, then fallback to .env file
FAL_KEY = os.environ.get("FAL_KEY", "")
if not FAL_KEY:
    _env_path = BASE_DIR / ".env"
    if _env_path.exists():
        for line in _env_path.read_text().splitlines():
            if line.startswith("FAL_KEY="):
                FAL_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
FAL_BASE = "https://queue.fal.run"
TEXT2IMG_ENDPOINT = "fal-ai/nano-banana-2"
EDIT_ENDPOINT = "fal-ai/nano-banana-2/edit"
POLL_INTERVAL = 2
MAX_POLL_TIME = 300

# TrendTrack API
TRENDTRACK_API_KEY = os.environ.get("TRENDTRACK_API_KEY", "")
if not TRENDTRACK_API_KEY:
    _env_path = BASE_DIR / ".env"
    if _env_path.exists():
        for line in _env_path.read_text().splitlines():
            if line.startswith("TRENDTRACK_API_KEY="):
                TRENDTRACK_API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
TRENDTRACK_BASE = "https://api.trendtrack.io"

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}

# Ensure dirs exist
for d in [DATA_DIR, UPLOADS_DIR, OUTPUTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS brands (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            url TEXT DEFAULT '',
            description TEXT DEFAULT '',
            brand_dna TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS products (
            id TEXT PRIMARY KEY,
            brand_id TEXT NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS product_images (
            id TEXT PRIMARY KEY,
            product_id TEXT NOT NULL REFERENCES products(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS generations (
            id TEXT PRIMARY KEY,
            brand_id TEXT NOT NULL REFERENCES brands(id),
            product_id TEXT,
            status TEXT DEFAULT 'pending',
            num_creations INTEGER DEFAULT 1,
            aspect_ratio TEXT DEFAULT '4:5',
            resolution TEXT DEFAULT '2K',
            prompt_text TEXT DEFAULT '',
            marketing_messages TEXT DEFAULT '[]',
            customer_reviews TEXT DEFAULT '[]',
            custom_prompt TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT,
            error TEXT
        );

        CREATE TABLE IF NOT EXISTS generated_images (
            id TEXT PRIMARY KEY,
            generation_id TEXT NOT NULL REFERENCES generations(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            prompt_used TEXT DEFAULT '',
            fal_request_id TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        -- Inspiration library: competitor/reference creatives
        CREATE TABLE IF NOT EXISTS inspirations (
            id TEXT PRIMARY KEY,
            brand_id TEXT NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            filepath TEXT NOT NULL,
            label TEXT DEFAULT '',
            source TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now'))
        );

    """)
    # Safe ALTER TABLEs (ignore if column exists)
    for stmt in [
        "ALTER TABLE generated_images ADD COLUMN rating TEXT DEFAULT NULL",
        "ALTER TABLE generated_images ADD COLUMN comment TEXT DEFAULT ''",
        "ALTER TABLE generations ADD COLUMN iteration_of TEXT DEFAULT NULL",
        "ALTER TABLE generations ADD COLUMN style_ref_id TEXT DEFAULT NULL",
        # New columns
        "ALTER TABLE products ADD COLUMN page_url TEXT DEFAULT ''",
        "ALTER TABLE products ADD COLUMN brief TEXT DEFAULT ''",
        "ALTER TABLE competitors ADD COLUMN market TEXT DEFAULT 'EU'",
        "ALTER TABLE generations ADD COLUMN language TEXT DEFAULT 'fr'",
        "ALTER TABLE generations ADD COLUMN formats TEXT DEFAULT '[\"4:5\"]'",
        "ALTER TABLE generations ADD COLUMN structure_id TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(stmt)
        except Exception:
            pass
    conn.executescript("""

        -- Saved prompt templates
        CREATE TABLE IF NOT EXISTS prompt_templates (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            angle TEXT DEFAULT '',
            prompt_text TEXT DEFAULT '',
            persona TEXT DEFAULT '',
            desire TEXT DEFAULT '',
            awareness TEXT DEFAULT '',
            composition_style TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Brand strategy data
        CREATE TABLE IF NOT EXISTS brand_strategy (
            id TEXT PRIMARY KEY,
            brand_id TEXT NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
            avatar_description TEXT DEFAULT '',
            avatar_pain_points TEXT DEFAULT '[]',
            avatar_desires TEXT DEFAULT '[]',
            usp TEXT DEFAULT '',
            offers TEXT DEFAULT '[]',
            tone_of_voice TEXT DEFAULT '',
            updated_at TEXT DEFAULT (datetime('now'))
        );

        -- Competitors
        CREATE TABLE IF NOT EXISTS competitors (
            id TEXT PRIMARY KEY,
            brand_id TEXT NOT NULL REFERENCES brands(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            url TEXT DEFAULT '',
            type TEXT DEFAULT 'direct',
            notes TEXT DEFAULT '',
            created_at TEXT DEFAULT (datetime('now'))
        );
    """)
    conn.commit()
    conn.close()


def seed_mng_strategy():
    """Pre-populate MNG strategy data if brand exists and has no strategy."""
    conn = get_db()
    brand = conn.execute("SELECT id FROM brands WHERE name = 'Mush n Go'").fetchone()
    if brand:
        existing = conn.execute("SELECT id FROM brand_strategy WHERE brand_id = ?", (brand["id"],)).fetchone()
        if not existing:
            conn.execute(
                """INSERT INTO brand_strategy (id, brand_id, avatar_description, avatar_pain_points, avatar_desires, usp, offers, tone_of_voice)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    str(uuid.uuid4()),
                    brand["id"],
                    "Entrepreneur/cadre 25-45 ans, urbain, mode de vie actif, cherche à optimiser sa performance mentale et physique sans les effets négatifs du café classique",
                    json.dumps(["Crash d'énergie après le café", "Ballonnements et problèmes digestifs", "Difficulté à se concentrer longtemps", "Stress chronique au travail", "Sommeil de mauvaise qualité"]),
                    json.dumps(["Énergie stable toute la journée", "Focus et clarté mentale", "Solution naturelle et clean", "Routine simple et efficace", "Performer sans sacrifier sa santé"]),
                    "Le seul café français aux 8 adaptogènes qui booste ton cerveau sans crash. Formule unique Brainstoorm avec Lion's Mane, Ashwagandha, Rhodiola et caféine NewCaff®.",
                    json.dumps(["Abonnement -20% + livraison gratuite", "Pack Entrepreneur (3 produits)"]),
                    "Direct, confiant, un peu provocateur. Pas corporate. Parle comme un pote qui sait de quoi il parle. Tutoiement. Emojis ok mais pas trop.",
                )
            )
            conn.commit()
            print("[SEED] MNG strategy data created", flush=True)
    conn.close()


# ---------------------------------------------------------------------------
# FAL API Service
# ---------------------------------------------------------------------------
def fal_headers():
    return {
        "Authorization": f"Key {FAL_KEY}",
        "Content-Type": "application/json",
    }


def fal_submit(endpoint: str, payload: dict) -> dict:
    """Submit a job to FAL. Returns the full response with status_url and response_url."""
    url = f"{FAL_BASE}/{endpoint}"
    resp = requests.post(url, headers=fal_headers(), json=payload)
    resp.raise_for_status()
    data = resp.json()
    print(f"[FAL] Submitted to {endpoint}, request_id={data['request_id']}", flush=True)
    return data


def fal_poll(submit_response: dict) -> dict | None:
    """Poll a FAL job using the URLs returned by submit."""
    status_url = submit_response["status_url"]
    result_url = submit_response["response_url"]
    request_id = submit_response["request_id"]
    start = time.time()
    print(f"[FAL] Polling {request_id}...", flush=True)
    while time.time() - start < MAX_POLL_TIME:
        resp = requests.get(status_url, headers=fal_headers())
        resp.raise_for_status()
        status = resp.json()
        print(f"[FAL] Status: {status.get('status')} (elapsed: {time.time()-start:.0f}s)", flush=True)
        if status.get("status") == "COMPLETED":
            result = requests.get(result_url, headers=fal_headers())
            result.raise_for_status()
            data = result.json()
            print(f"[FAL] Got result with {len(data.get('images', []))} images", flush=True)
            return data
        elif status.get("status") in ("FAILED", "CANCELLED"):
            print(f"[FAL] FAILED/CANCELLED: {status}", flush=True)
            return None
        time.sleep(POLL_INTERVAL)
    print(f"[FAL] TIMEOUT after {MAX_POLL_TIME}s", flush=True)
    return None


def fal_download(url: str, save_path: Path):
    resp = requests.get(url, stream=True)
    resp.raise_for_status()
    with open(save_path, "wb") as f:
        for chunk in resp.iter_content(chunk_size=8192):
            f.write(chunk)


def image_to_data_uri(image_path: Path) -> str:
    ext = image_path.suffix.lower()
    mime = "image/png" if ext == ".png" else "image/jpeg" if ext in (".jpg", ".jpeg") else "image/webp"
    with open(image_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")
    return f"data:{mime};base64,{b64}"


import random

# 5 proven ad structures based on competitive analysis (competitive-creative-patterns.md)
# {headline} = generated headline, {product} = product name, {aspect} = aspect ratio, {bg_color} = background color, {headline_style} = headline font style
AD_STRUCTURES = [
    {
        "id": "S1_pain_solution",
        "name": "Pain Point + Solution",
        "description": "Bold pain point headline + product hero + trust bar",
        "prompt_template": "Static ad creative, vertical {aspect}. TOP: Bold {headline_style} headline text '{headline}' on {bg_color} background, large and attention-grabbing. MIDDLE: Product photography of the {product} with natural ingredients (mushrooms, coffee beans, herbs) beautifully arranged. BOTTOM: Trust bar with stars rating, customer count, and guarantee badge. Clean, premium, editorial magazine feel. The product packaging must be clearly visible and prominent."
    },
    {
        "id": "S2_comparison",
        "name": "Comparaison Split",
        "description": "Us vs them split layout with checkmarks",
        "prompt_template": "Static ad creative, vertical {aspect}. SPLIT LAYOUT: Left side labeled 'Cafe Classique' with red X marks next to problems (crash, bloating, anxiety) on gray/muted background. Right side labeled '{product}' with green checkmarks next to benefits (stable energy, digestion, focus) on bright/vibrant background. Product photo on the right side. Bottom trust bar. Clean graphic design, not photographic."
    },
    {
        "id": "S3_price_slash",
        "name": "Prix Barré + Urgence",
        "description": "Crossed-out price + deal + CTA button",
        "prompt_template": "Static ad creative, vertical {aspect}. TOP: Large crossed-out old price in red with new lower price in bold black/white. Discount percentage badge. MIDDLE: Beautiful product photography of {product} with coffee cup and natural setting. BOTTOM: Bold CTA button 'COMMANDER MAINTENANT' + 'Livraison offerte' text + trust badges. Warm, inviting, premium feel with urgency."
    },
    {
        "id": "S4_listicle",
        "name": "Listicle Bénéfices",
        "description": "Product center + benefit bubbles around it",
        "prompt_template": "Static ad creative, vertical {aspect}. CENTER: {product} product packaging beautifully photographed with natural ingredients. AROUND THE PRODUCT: 4 benefit bubbles/badges with checkmarks: Focus, Energy, Digestion, Anti-stress. Each bubble has a small icon and short text. TOP: Brand headline. BOTTOM: Trust bar with rating and guarantee. Clean, organized, magazine editorial layout."
    },
    {
        "id": "S5_testimonial",
        "name": "Témoignage Client",
        "description": "Customer quote + stars + product",
        "prompt_template": "Static ad creative, vertical {aspect}. TOP: Large quotation marks, then customer testimonial quote in elegant serif italic font: '{headline}'. Customer first name below the quote. MIDDLE: Five gold stars + 'Trustpilot Excellent 4.8/5' badge. BOTTOM: Small product photo of {product} + trust bar with '20 000+ clients satisfaits'. Warm, authentic, trustworthy feel. Light/cream background."
    }
]

# Headline generators per angle theme — validated from competitive analysis
ANGLE_HEADLINES = {
    "ballonnements": [
        "Ton café te ballonne. Celui-ci répare ta digestion.",
        "Fini le ventre gonflé après le café.",
        "Si ton ventre gonfle après chaque café, lis ça.",
    ],
    "crash_energie": [
        "Fini le crash de 14h.",
        "Ton café te détruit. Celui-ci te booste.",
        "Énergie stable 10h. Sans crash. Sans nervosité.",
    ],
    "focus": [
        "10h de focus. 1 tasse.",
        "Ta meilleure version commence ici.",
        "Focus laser sans la nervosité du café.",
    ],
    "alternative_cafe": [
        "Le café le + dosé de France. 6225mg d'adaptogènes.",
        "Upgrade ta routine café.",
        "Ton rituel café, mais en version supérieure.",
    ],
    "stress": [
        "Ton stress te ballonne.",
        "Moins de stress, plus de clarté.",
        "Le stress détruit ton ventre. La solution existe.",
    ],
    "naturel": [
        "3 champignons + 3 plantes. 0 cochonnerie.",
        "100% naturel. 6225mg par dose.",
        "La formule la + concentrée du marché français.",
    ],
    "prix": [
        "0,87€ par tasse. Ton Starbucks coûte 5€.",
        "-20% + livraison offerte.",
        "Essaie 60 jours. Satisfaite ou remboursée.",
    ],
    "social_proof": [
        "20 000+ clients. 4.8/5 sur Trustpilot.",
        "Elles l'ont testé. Elles ne reviennent plus au café.",
        "Le secret de 20 000 françaises pour un ventre plat.",
    ],
}

def generate_headline(angle_text: str) -> str:
    """Pick a relevant headline based on the user's angle."""
    angle_lower = angle_text.lower()
    for key, headlines in ANGLE_HEADLINES.items():
        if key in angle_lower:
            return random.choice(headlines)
    # Fallback: pick from a random angle
    all_headlines = [h for headlines in ANGLE_HEADLINES.values() for h in headlines]
    return random.choice(all_headlines)


def build_prompt(brand_dna: str, product_name: str, aspect_ratio: str,
                 marketing_messages: list, customer_reviews: list,
                 custom_prompt: str, is_iteration: bool = False,
                 has_style_ref: bool = False,
                 strategy_context: str = "",
                 feedback_context: str = "",
                 language: str = "fr",
                 structure_id: str = "") -> str:
    """Build a generation prompt from brand DNA + product + marketing context."""

    def _apply_language(prompt: str) -> str:
        if language and language != "fr":
            return f"IMPORTANT: All text overlays and headlines must be in English.\n\n{prompt}"
        return prompt

    # Determine the angle
    angle_text = ""
    if marketing_messages:
        angle_text = marketing_messages[0]
    elif custom_prompt:
        angle_text = custom_prompt

    # ITERATION MODE: create a variation of an existing creative
    if is_iteration:
        headline = generate_headline(angle_text) if angle_text else random.choice(ANGLE_HEADLINES["default"])
        variation_type = random.choice([
            "Keep the same overall composition but change the headline, color grading, and model/person.",
            "Keep the same message and product placement but use a completely different composition layout.",
            "Keep the same concept but make it more dramatic — stronger contrast, bolder typography, more intense lighting.",
            "Keep the same idea but make it feel more UGC/authentic — less polished, more relatable.",
            "Keep the same angle but change from lifestyle to product-hero focus (or vice versa).",
            "Same message, different visual metaphor. Find a new way to illustrate the concept.",
        ])
        return _apply_language(f"""Create a VARIATION of the reference ad creative. This is an iteration — keep the brand and product consistent but create something fresh.

ITERATION RULE: {variation_type}

Angle: {angle_text or 'general brand awareness'}
New headline to use: "{headline}"
Product: {product_name}

The product MUST be "{product_name}" by Mush n Go — use the exact product packshot from the reference images.
Style: Professional advertising photography for Meta/Instagram. {aspect_ratio} format.
{brand_dna[:300]}
{strategy_context}
{feedback_context}""")

    # STYLE REFERENCE MODE: match the style of an inspiration image
    if has_style_ref:
        headline = generate_headline(angle_text) if angle_text else random.choice(ANGLE_HEADLINES["default"])
        return _apply_language(f"""Create a static ad creative for Meta/Instagram, matching the STYLE and COMPOSITION of the reference inspiration image.

Copy the visual style (lighting, composition, color grading, layout) from the reference image but adapt it for:
- Product: {product_name} by Mush n Go (use the product packshot from the reference images)
- Angle: {angle_text or 'general brand awareness'}
- Headline: "{headline}"

The product MUST be "{product_name}" by Mush n Go — use the exact product packshot from the reference images. Do NOT use any other product.
{aspect_ratio} format. Professional quality.
{brand_dna[:300]}
{strategy_context}
{feedback_context}""")

    # If user gave a full custom prompt, use it directly with brand context
    if custom_prompt and not marketing_messages:
        return _apply_language(f"""Static advertisement for Meta/Instagram. {aspect_ratio} format.

{custom_prompt}

The product shown MUST be the exact product from the reference image — use it faithfully.
Brand: Mush n Go — premium French adaptogenic mushroom supplement brand.
Product: {product_name}.
Style: Professional advertising photography, warm directional lighting, editorial quality. Purple (#7643DE) accent color.
{brand_dna[:400]}
{strategy_context}
{feedback_context}""")

    # STANDARD MODE: generate from angle
    all_headlines = [h for headlines in ANGLE_HEADLINES.values() for h in headlines]
    headline = generate_headline(angle_text) if angle_text else random.choice(all_headlines)

    # Pick a structure — specific one if structure_id provided, else random
    selected_structure = None
    if structure_id:
        for s in AD_STRUCTURES:
            if s["id"] == structure_id:
                selected_structure = s
                break
    if not selected_structure:
        selected_structure = random.choice(AD_STRUCTURES)

    # Fill in the prompt template placeholders
    bg_colors = ["deep purple (#7643DE)", "sage green (#8FAE8B)", "warm cream (#F5F0E8)", "dark charcoal (#1A1A1A)"]
    headline_styles = ["serif bold italic", "sans-serif bold uppercase", "handwritten bold"]
    composition = selected_structure["prompt_template"]
    composition = composition.replace("{headline}", headline)
    composition = composition.replace("{product}", product_name)
    composition = composition.replace("{aspect}", aspect_ratio)
    composition = composition.replace("{bg_color}", random.choice(bg_colors))
    composition = composition.replace("{headline_style}", random.choice(headline_styles))

    return _apply_language(f"""Static advertisement for Meta/Instagram. {aspect_ratio} vertical format.

{composition}

The product shown MUST be "{product_name}" by Mush n Go — use the exact product packshot from the reference image faithfully. Do NOT substitute with another product.

Style: Professional advertising photography, warm directional lighting, editorial quality.
Color accents: purple (#7643DE), warm amber, dark backgrounds.
{brand_dna[:300]}
{strategy_context}
{feedback_context}""")


FORMAT_TO_RESOLUTION = {
    "1:1": "1024x1024",
    "4:5": "1024x1280",
    "9:16": "1024x1792",
    "16:9": "1792x1024",
}


def run_generation(generation_id: str):
    """Background worker that runs the FAL generation."""
    conn = get_db()
    try:
        gen = dict(conn.execute("SELECT * FROM generations WHERE id = ?", (generation_id,)).fetchone())

        # Get brand DNA
        brand = conn.execute("SELECT * FROM brands WHERE id = ?", (gen["brand_id"],)).fetchone()
        brand_dna = brand["brand_dna"] if brand else ""

        # Get product info and images
        product_name = "Unknown Product"
        product_image_paths = []
        if gen["product_id"]:
            product = conn.execute("SELECT * FROM products WHERE id = ?", (gen["product_id"],)).fetchone()
            if product:
                product_name = product["name"]
            images = conn.execute(
                "SELECT * FROM product_images WHERE product_id = ?",
                (gen["product_id"],)
            ).fetchall()
            product_image_paths = [Path(img["filepath"]) for img in images if Path(img["filepath"]).exists()]

        marketing_messages = json.loads(gen["marketing_messages"])
        customer_reviews = json.loads(gen["customer_reviews"])

        # Load strategy data if available
        strategy = conn.execute("SELECT * FROM brand_strategy WHERE brand_id = ?", (gen["brand_id"],)).fetchone()
        strategy_context = ""
        if strategy:
            strategy_context = f"""
TARGET AVATAR: {strategy['avatar_description']}
PAIN POINTS: {strategy['avatar_pain_points']}
DESIRES: {strategy['avatar_desires']}
USP: {strategy['usp']}
TONE: {strategy['tone_of_voice']}"""

        # Update status
        conn.execute("UPDATE generations SET status = 'generating' WHERE id = ?", (generation_id,))
        conn.commit()

        # Create output directory for this generation
        gen_output_dir = OUTPUTS_DIR / generation_id
        gen_output_dir.mkdir(parents=True, exist_ok=True)

        total_created = 0
        num_creations = gen["num_creations"]

        # Generate images in batches of 4 (FAL limit)
        remaining = num_creations
        batch_num = 0

        while remaining > 0:
            batch_size = min(remaining, 4)
            batch_num += 1

            # Build context for iteration/style ref
            is_iteration = bool(gen.get("iteration_of"))
            has_style_ref = bool(gen.get("style_ref_id"))

            # Build feedback context from past verdicts
            feedback_rows = conn.execute("""
                SELECT gi.rating, gi.comment, gi.prompt_used
                FROM generated_images gi
                WHERE gi.rating IS NOT NULL
                ORDER BY gi.created_at DESC LIMIT 20
            """).fetchall()
            feedback_lines = []
            for frow in feedback_rows:
                v = frow['rating']
                c = frow['comment'] or ''
                if v == 'winner':
                    feedback_lines.append(f"WINNER: {c}" if c else "WINNER (no comment)")
                elif v == 'kill':
                    feedback_lines.append(f"AVOID: {c}" if c else "KILLED (no detail)")
            feedback_context = ""
            if feedback_lines:
                feedback_context = "CREATIVE FEEDBACK FROM USER (learn from this):\n" + "\n".join(feedback_lines[:15])

            # Parse formats for multi-format generation
            try:
                gen_formats = json.loads(gen.get("formats", '["4:5"]'))
            except (json.JSONDecodeError, TypeError):
                gen_formats = [gen["aspect_ratio"]]
            if not gen_formats:
                gen_formats = [gen["aspect_ratio"]]

            gen_language = gen.get("language", "fr") or "fr"

            gen_structure_id = gen.get("structure_id", "") or ""

            prompt = build_prompt(
                brand_dna=brand_dna,
                product_name=product_name,
                aspect_ratio=gen["aspect_ratio"],
                marketing_messages=marketing_messages,
                customer_reviews=customer_reviews,
                custom_prompt=gen["custom_prompt"],
                is_iteration=is_iteration,
                has_style_ref=has_style_ref,
                strategy_context=strategy_context,
                feedback_context=feedback_context,
                language=gen_language,
                structure_id=gen_structure_id,
            )

            # Collect reference images: product packshots + style ref + iteration source
            ref_images = [image_to_data_uri(p) for p in product_image_paths[:4]]

            # Add style reference (inspiration image) if set
            if gen.get("style_ref_id"):
                style_img = conn.execute("SELECT filepath FROM inspirations WHERE id = ?", (gen["style_ref_id"],)).fetchone()
                if style_img and Path(style_img["filepath"]).exists():
                    ref_images.append(image_to_data_uri(Path(style_img["filepath"])))

            # Add iteration source image if set
            if gen.get("iteration_of"):
                iter_img = conn.execute("SELECT filepath FROM generated_images WHERE id = ?", (gen["iteration_of"],)).fetchone()
                if iter_img and Path(iter_img["filepath"]).exists():
                    ref_images.append(image_to_data_uri(Path(iter_img["filepath"])))

            # Loop through each requested format
            for fmt_idx, fmt in enumerate(gen_formats):
                fmt_aspect = fmt if fmt in FORMAT_TO_RESOLUTION else gen["aspect_ratio"]
                payload = {
                    "prompt": prompt,
                    "aspect_ratio": fmt_aspect,
                    "num_images": batch_size,
                    "output_format": "png",
                    "resolution": gen["resolution"],
                }

                # Use edit endpoint if we have any reference images
                if ref_images:
                    endpoint = EDIT_ENDPOINT
                    payload["image_urls"] = ref_images[:14]
                else:
                    endpoint = TEXT2IMG_ENDPOINT

                try:
                    submit_resp = fal_submit(endpoint, payload)
                    request_id = submit_resp["request_id"]
                    result = fal_poll(submit_resp)

                    if result and "images" in result:
                        for i, img_data in enumerate(result["images"]):
                            img_url = img_data.get("url", "")
                            if not img_url:
                                continue
                            img_id = str(uuid.uuid4())
                            fmt_label = fmt_aspect.replace(":", "x")
                            filename = f"crea_{batch_num}_{fmt_label}_{i+1}.png"
                            filepath = gen_output_dir / filename
                            fal_download(img_url, filepath)

                            conn.execute(
                                "INSERT INTO generated_images (id, generation_id, filename, filepath, prompt_used, fal_request_id) VALUES (?, ?, ?, ?, ?, ?)",
                                (img_id, generation_id, filename, str(filepath), prompt[:500], request_id)
                            )
                            total_created += 1
                            conn.commit()

                except Exception as e:
                    import traceback
                    print(f"Batch {batch_num} format {fmt_aspect} error: {e}", flush=True)
                    traceback.print_exc()

            remaining -= batch_size

        # Mark complete
        conn.execute(
            "UPDATE generations SET status = 'completed', completed_at = datetime('now') WHERE id = ?",
            (generation_id,)
        )
        conn.commit()

    except Exception as e:
        conn.execute(
            "UPDATE generations SET status = 'failed', error = ? WHERE id = ?",
            (str(e), generation_id)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# FastAPI App
# ---------------------------------------------------------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    seed_mng_strategy()
    yield

app = FastAPI(title="CreaFlow", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Static files ---
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
app.mount("/uploads", StaticFiles(directory=str(UPLOADS_DIR)), name="uploads")
app.mount("/outputs", StaticFiles(directory=str(OUTPUTS_DIR)), name="outputs")


@app.get("/")
async def index():
    return FileResponse(str(BASE_DIR / "static" / "index.html"))


# --- API: Stats ---
@app.get("/api/stats")
async def get_stats():
    conn = get_db()
    brands = conn.execute("SELECT COUNT(*) as c FROM brands").fetchone()["c"]
    products = conn.execute("SELECT COUNT(*) as c FROM products").fetchone()["c"]
    total_images = conn.execute("SELECT COUNT(*) as c FROM generated_images").fetchone()["c"]
    total_gens = conn.execute("SELECT COUNT(*) as c FROM generations").fetchone()["c"]
    conn.close()
    return {
        "brands": brands,
        "products": products,
        "total_images": total_images,
        "total_generations": total_gens,
    }


# --- API: Brands ---
@app.get("/api/brands")
async def list_brands():
    conn = get_db()
    rows = conn.execute("SELECT * FROM brands ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/brands")
async def create_brand(
    name: str = Form(...),
    url: str = Form(""),
    description: str = Form(""),
    brand_dna: str = Form(""),
):
    brand_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO brands (id, name, url, description, brand_dna) VALUES (?, ?, ?, ?, ?)",
        (brand_id, name, url, description, brand_dna)
    )
    conn.commit()
    brand = dict(conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone())
    conn.close()
    return brand


@app.get("/api/brands/{brand_id}")
async def get_brand(brand_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404, "Brand not found")
    return dict(row)


@app.put("/api/brands/{brand_id}")
async def update_brand(
    brand_id: str,
    name: str = Form(None),
    url: str = Form(None),
    description: str = Form(None),
    brand_dna: str = Form(None),
):
    conn = get_db()
    brand = conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()
    if not brand:
        conn.close()
        raise HTTPException(404, "Brand not found")
    updates = {}
    if name is not None:
        updates["name"] = name
    if url is not None:
        updates["url"] = url
    if description is not None:
        updates["description"] = description
    if brand_dna is not None:
        updates["brand_dna"] = brand_dna
    if updates:
        sets = ", ".join(f"{k} = ?" for k in updates)
        vals = list(updates.values()) + [brand_id]
        conn.execute(f"UPDATE brands SET {sets}, updated_at = datetime('now') WHERE id = ?", vals)
        conn.commit()
    result = dict(conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone())
    conn.close()
    return result


@app.delete("/api/brands/{brand_id}")
async def delete_brand(brand_id: str):
    conn = get_db()
    conn.execute("DELETE FROM brands WHERE id = ?", (brand_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# --- API: Products ---
@app.get("/api/brands/{brand_id}/products")
async def list_products(brand_id: str):
    conn = get_db()
    products = conn.execute(
        "SELECT * FROM products WHERE brand_id = ? ORDER BY created_at DESC",
        (brand_id,)
    ).fetchall()
    result = []
    for p in products:
        pd = dict(p)
        images = conn.execute(
            "SELECT * FROM product_images WHERE product_id = ?", (p["id"],)
        ).fetchall()
        pd["images"] = [dict(i) for i in images]
        result.append(pd)
    conn.close()
    return result


@app.post("/api/brands/{brand_id}/products")
async def create_product(
    brand_id: str,
    name: str = Form(...),
    images: List[UploadFile] = File(default=[]),
):
    product_id = str(uuid.uuid4())
    conn = get_db()

    # Check brand exists
    brand = conn.execute("SELECT id FROM brands WHERE id = ?", (brand_id,)).fetchone()
    if not brand:
        conn.close()
        raise HTTPException(404, "Brand not found")

    conn.execute(
        "INSERT INTO products (id, brand_id, name) VALUES (?, ?, ?)",
        (product_id, brand_id, name)
    )

    # Save uploaded images
    product_dir = UPLOADS_DIR / brand_id / product_id
    product_dir.mkdir(parents=True, exist_ok=True)

    for img_file in images:
        if img_file.filename:
            img_id = str(uuid.uuid4())
            ext = Path(img_file.filename).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                continue
            filename = f"{img_id}{ext}"
            filepath = product_dir / filename
            content = await img_file.read()
            filepath.write_bytes(content)
            conn.execute(
                "INSERT INTO product_images (id, product_id, filename, filepath) VALUES (?, ?, ?, ?)",
                (img_id, product_id, img_file.filename, str(filepath))
            )

    conn.commit()
    # Return product with images
    pd = dict(conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone())
    imgs = conn.execute("SELECT * FROM product_images WHERE product_id = ?", (product_id,)).fetchall()
    pd["images"] = [dict(i) for i in imgs]
    conn.close()
    return pd


@app.post("/api/products/{product_id}/images")
async def add_product_images(
    product_id: str,
    images: List[UploadFile] = File(...),
):
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        raise HTTPException(404, "Product not found")

    product_dir = UPLOADS_DIR / product["brand_id"] / product_id
    product_dir.mkdir(parents=True, exist_ok=True)

    added = []
    for img_file in images:
        if img_file.filename:
            img_id = str(uuid.uuid4())
            ext = Path(img_file.filename).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                continue
            filename = f"{img_id}{ext}"
            filepath = product_dir / filename
            content = await img_file.read()
            filepath.write_bytes(content)
            conn.execute(
                "INSERT INTO product_images (id, product_id, filename, filepath) VALUES (?, ?, ?, ?)",
                (img_id, product_id, img_file.filename, str(filepath))
            )
            added.append({"id": img_id, "filename": img_file.filename})
    conn.commit()
    conn.close()
    return added


@app.put("/api/products/{product_id}")
async def update_product(product_id: str, request: Request):
    data = await request.json()
    conn = get_db()
    product = conn.execute("SELECT * FROM products WHERE id = ?", (product_id,)).fetchone()
    if not product:
        conn.close()
        raise HTTPException(404, "Product not found")
    conn.execute("UPDATE products SET name=?, page_url=?, brief=? WHERE id=?",
        (data.get("name", product["name"]), data.get("page_url", product["page_url"]), data.get("brief", product["brief"]), product_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/product-images/{image_id}")
async def delete_product_image(image_id: str):
    conn = get_db()
    img = conn.execute("SELECT * FROM product_images WHERE id = ?", (image_id,)).fetchone()
    if img:
        filepath = Path(img["filepath"])
        if filepath.exists():
            filepath.unlink()
        conn.execute("DELETE FROM product_images WHERE id = ?", (image_id,))
        conn.commit()
    conn.close()
    return {"ok": True}


# --- API: Generations ---
@app.post("/api/generate")
async def start_generation(
    brand_id: str = Form(...),
    product_id: str = Form(""),
    num_creations: int = Form(4),
    aspect_ratio: str = Form("4:5"),
    resolution: str = Form("2K"),
    marketing_messages: str = Form("[]"),
    customer_reviews: str = Form("[]"),
    custom_prompt: str = Form(""),
    iteration_of: str = Form(""),
    style_ref_id: str = Form(""),
    language: str = Form("fr"),
    formats: str = Form('["4:5"]'),
    structure_id: str = Form(""),
):
    if not FAL_KEY:
        raise HTTPException(400, "FAL_KEY not configured. Set the FAL_KEY environment variable.")

    gen_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        """INSERT INTO generations
           (id, brand_id, product_id, num_creations, aspect_ratio, resolution,
            marketing_messages, customer_reviews, custom_prompt, status,
            iteration_of, style_ref_id, language, formats, structure_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
        (gen_id, brand_id, product_id or None, num_creations, aspect_ratio,
         resolution, marketing_messages, customer_reviews, custom_prompt,
         iteration_of or None, style_ref_id or None, language, formats,
         structure_id or "")
    )
    conn.commit()
    conn.close()

    # Launch background generation
    thread = Thread(target=run_generation, args=(gen_id,), daemon=True)
    thread.start()

    return {"id": gen_id, "status": "pending"}


@app.get("/api/estimate-cost")
async def estimate_cost(count: int = 1, formats: int = 1):
    cost_per_image = 0.04
    total = count * formats * cost_per_image
    return {"cost_per_image": cost_per_image, "total_cost": round(total, 2), "total_images": count * formats}


@app.get("/api/generations")
async def list_generations(request: Request):
    conn = get_db()
    params = request.query_params
    type_filter = params.get("type")
    product_id_filter = params.get("product_id")
    verdict_filter = params.get("verdict")

    query = "SELECT g.*, b.name as brand_name FROM generations g LEFT JOIN brands b ON g.brand_id = b.id"
    conditions = []
    bind_vals = []

    if type_filter:
        conditions.append("g.generation_mode = ?")
        bind_vals.append(type_filter)
    if product_id_filter:
        conditions.append("g.product_id = ?")
        bind_vals.append(product_id_filter)
    if verdict_filter:
        conditions.append("g.id IN (SELECT generation_id FROM generated_images WHERE rating = ?)")
        bind_vals.append(verdict_filter)

    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY g.created_at DESC"

    gens = conn.execute(query, bind_vals).fetchall()
    result = []
    for g in gens:
        gd = dict(g)
        images = conn.execute(
            "SELECT * FROM generated_images WHERE generation_id = ?", (g["id"],)
        ).fetchall()
        gd["images"] = [dict(i) for i in images]
        result.append(gd)
    conn.close()
    return result


@app.get("/api/generations/{gen_id}")
async def get_generation(gen_id: str):
    conn = get_db()
    g = conn.execute(
        "SELECT g.*, b.name as brand_name FROM generations g LEFT JOIN brands b ON g.brand_id = b.id WHERE g.id = ?",
        (gen_id,)
    ).fetchone()
    if not g:
        conn.close()
        raise HTTPException(404, "Generation not found")
    gd = dict(g)
    images = conn.execute(
        "SELECT * FROM generated_images WHERE generation_id = ?", (gen_id,)
    ).fetchall()
    gd["images"] = [dict(i) for i in images]
    conn.close()
    return gd


@app.delete("/api/generations/{gen_id}")
async def delete_generation(gen_id: str):
    conn = get_db()
    # Delete output files
    gen_dir = OUTPUTS_DIR / gen_id
    if gen_dir.exists():
        shutil.rmtree(gen_dir)
    conn.execute("DELETE FROM generated_images WHERE generation_id = ?", (gen_id,))
    conn.execute("DELETE FROM generations WHERE id = ?", (gen_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.delete("/api/generated-images/{image_id}")
async def delete_generated_image(image_id: str):
    conn = get_db()
    img = conn.execute("SELECT * FROM generated_images WHERE id = ?", (image_id,)).fetchone()
    if img:
        filepath = Path(img["filepath"])
        if filepath.exists():
            filepath.unlink()
        conn.execute("DELETE FROM generated_images WHERE id = ?", (image_id,))
        conn.commit()
    conn.close()
    return {"ok": True}


@app.post("/api/generated-images/{image_id}/regenerate")
async def regenerate_image(image_id: str):
    conn = get_db()
    img = conn.execute("SELECT * FROM generated_images WHERE id=?", (image_id,)).fetchone()
    if not img:
        conn.close()
        raise HTTPException(404)
    gen = conn.execute("SELECT * FROM generations WHERE id=?", (img["generation_id"],)).fetchone()
    if not gen:
        conn.close()
        raise HTTPException(404)
    # Create new generation with same params
    import threading
    new_id = str(uuid.uuid4())
    conn.execute("""INSERT INTO generations (id, brand_id, product_id, num_creations, aspect_ratio, resolution,
        marketing_messages, customer_reviews, custom_prompt, status, iteration_of, style_ref_id, language, formats, structure_id)
        VALUES (?,?,?,1,?,?,?,?,?,'pending',?,?,?,?,?)""",
        (new_id, gen["brand_id"], gen["product_id"], gen["aspect_ratio"], gen["resolution"],
         gen["marketing_messages"], gen["customer_reviews"], gen["custom_prompt"],
         gen.get("iteration_of"), gen.get("style_ref_id"),
         gen.get("language", "fr"), gen.get("formats", '["4:5"]'),
         gen.get("structure_id", "")))
    conn.commit()
    conn.close()
    threading.Thread(target=run_generation, args=(new_id,), daemon=True).start()
    return {"generation_id": new_id}


# --- API: Image serving ---
@app.get("/api/image/{image_id}")
async def serve_image(image_id: str):
    conn = get_db()
    img = conn.execute("SELECT filepath FROM generated_images WHERE id = ?", (image_id,)).fetchone()
    if not img:
        img = conn.execute("SELECT filepath FROM product_images WHERE id = ?", (image_id,)).fetchone()
    conn.close()
    if not img:
        raise HTTPException(404, "Image not found")
    filepath = Path(img["filepath"])
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(filepath))


# --- API: Settings ---
@app.get("/api/settings")
async def get_settings():
    conn = get_db()
    rows = conn.execute("SELECT * FROM settings").fetchall()
    conn.close()
    return {r["key"]: r["value"] for r in rows}


@app.post("/api/settings")
async def update_settings(data: dict):
    conn = get_db()
    for key, value in data.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, str(value))
        )
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/fal-status")
async def fal_status():
    return {"configured": bool(FAL_KEY)}


# --- API: Ad Structures & Angles ---
@app.get("/api/structures")
async def get_structures():
    return [{"id": s["id"], "name": s["name"], "description": s["description"]} for s in AD_STRUCTURES]


@app.get("/api/angles")
async def get_angles():
    return {k: v for k, v in ANGLE_HEADLINES.items()}


# --- API: Ad Doctor refresh ---
AD_DOCTOR_DIR = Path("/Users/alex/Claude Code/ad_doctor_mng")

@app.post("/api/ad-doctor/refresh")
async def refresh_ad_doctor():
    """
    Re-pull MNG Meta Ads data from Ad Doctor (last 7d).
    Saves to data/mng_top_7d.json so /api/my-top-ads serves fresh data.
    """
    if not AD_DOCTOR_DIR.exists():
        raise HTTPException(503, "Ad Doctor not available on this host")

    import subprocess
    try:
        # Run the pull via Ad Doctor's venv
        cmd = [
            "bash", "-c",
            f'cd "{AD_DOCTOR_DIR}" && source venv/bin/activate && python3 -c "'
            "from meta_client import MetaClient\n"
            "from classifier import parse_ad, classify\n"
            "from dataclasses import asdict\n"
            "import json\n"
            "mc = MetaClient()\n"
            "mc.ensure_valid_token()\n"
            "ads = mc.fetch_insights(date_preset='last_7d', active_only=False)\n"
            "results = []\n"
            "for raw in ads:\n"
            "    if float(raw.get('spend', 0)) < 5: continue\n"
            "    parsed = parse_ad(raw)\n"
            "    classified = classify(parsed)\n"
            "    results.append(asdict(classified))\n"
            "results.sort(key=lambda x: x.get('spend',0), reverse=True)\n"
            f"json.dump(results, open('{DATA_DIR}/mng_top_7d.json','w'), default=str)\n"
            "print(len(results))\n"
            '"'
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            return {"ok": False, "error": result.stderr[-500:]}

        # Parse result
        count = int(result.stdout.strip().split('\n')[-1]) if result.stdout.strip() else 0
        return {"ok": True, "count": count, "refreshed_at": datetime.now().isoformat()}
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Timeout (Meta API slow)"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


@app.get("/api/ad-doctor/thumbnail/{ad_id}")
async def get_ad_thumbnail(ad_id: str):
    """Get the creative thumbnail URL for a given Meta ad ID."""
    # Read META_ACCESS_TOKEN from Ad Doctor's .env
    token = ""
    env_path = AD_DOCTOR_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("META_ACCESS_TOKEN="):
                token = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    if not token:
        raise HTTPException(503, "Meta token not available")

    try:
        r = requests.get(
            f"https://graph.facebook.com/v23.0/{ad_id}",
            params={
                "fields": "creative{thumbnail_url,image_url,object_story_spec,asset_feed_spec}",
                "access_token": token,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        creative = data.get("creative", {})
        return {
            "ad_id": ad_id,
            "thumbnail_url": creative.get("thumbnail_url", ""),
            "image_url": creative.get("image_url", ""),
            "creative_id": creative.get("id", ""),
        }
    except Exception as e:
        return {"ad_id": ad_id, "thumbnail_url": "", "error": str(e)}


@app.get("/api/ad-doctor/status")
async def ad_doctor_status():
    """Check if Ad Doctor is reachable."""
    if not AD_DOCTOR_DIR.exists():
        return {"available": False, "reason": "Ad Doctor folder not found"}

    data_path = DATA_DIR / "mng_top_7d.json"
    last_refresh = None
    if data_path.exists():
        last_refresh = datetime.fromtimestamp(data_path.stat().st_mtime).isoformat()

    return {
        "available": True,
        "last_refresh": last_refresh,
        "data_exists": data_path.exists(),
    }


# --- API: TrendTrack — Top créas de la semaine ---
@app.get("/api/trendtrack/status")
async def trendtrack_status():
    return {"configured": bool(TRENDTRACK_API_KEY)}


@app.get("/api/trendtrack/top-ads")
async def trendtrack_top_ads(
    period: str = "rankDelta7d",
    limit: int = 30,
    media_type: str = "image",
):
    """
    Récupère les créas qui ont le plus monté dans le ranking sur 7/14/30 jours,
    cross-marques (toutes les marques trackées dans le workspace).
    period: rankDelta7d | rankDelta14d | rankDelta30d | reachDelta7d | currentRank
    media_type: image | video | all
    """
    if not TRENDTRACK_API_KEY:
        raise HTTPException(503, "TrendTrack API not configured")

    params = {"sortBy": period, "limit": min(limit, 100)}
    if media_type and media_type != "all":
        params["mediaType"] = media_type

    headers = {"Authorization": f"Bearer {TRENDTRACK_API_KEY}"}
    try:
        r = requests.get(
            f"{TRENDTRACK_BASE}/v1/workspace/top-ads",
            headers=headers,
            params=params,
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        # Simplify response for frontend
        ads = []
        for item in data.get("data", []):
            ad = item.get("ad", {})
            brand = item.get("brandtracker", {})
            metrics = item.get("metrics", {}) or ad.get("metrics", {})
            rank = ad.get("rank", {})
            content = ad.get("content", {})
            media = ad.get("media", {})
            ads.append({
                "id": ad.get("id", ""),
                "brand_name": brand.get("name", ""),
                "brand_logo": ad.get("advertiser", {}).get("logoUrl", ""),
                "thumbnail": media.get("thumbnailUrl", ""),
                "media_url": media.get("mediaUrl", ""),
                "media_type": media.get("type", "image"),
                "headline": content.get("title") or content.get("ctaDescription") or "",
                "body": (content.get("body") or "")[:200],
                "cta": content.get("callToAction", ""),
                "landing_page": content.get("landingPageUrl", ""),
                "days_running": ad.get("daysRunning", 0),
                "current_rank": rank.get("currentRank", 0),
                "rank_delta": rank.get("rankDelta", 0),
                "improvement_pct": rank.get("improvementPct", 0),
                "reach": metrics.get("totalReach", 0) or metrics.get("reach", 0),
                "reach_delta_7d": metrics.get("reachDelta7d", 0),
                "estimated_spend": metrics.get("estimatedSpend", 0),
                "duplicates": metrics.get("duplicates", 0),
                "first_seen": ad.get("firstSeenAt", ""),
                "facebook_page_id": brand.get("facebookPageId", ""),
            })
        return {
            "ads": ads,
            "credits_remaining": r.headers.get("X-Credits-Remaining", "?"),
            "credits_used": r.headers.get("X-Credits-Used", "?"),
            "period": period,
            "media_type": media_type,
        }
    except requests.exceptions.HTTPError as e:
        raise HTTPException(e.response.status_code, f"TrendTrack error: {e.response.text[:200]}")
    except Exception as e:
        raise HTTPException(500, f"TrendTrack request failed: {str(e)}")


def _get_meta_token() -> str:
    env_path = AD_DOCTOR_DIR / ".env"
    if env_path.exists():
        for line in env_path.read_text().splitlines():
            if line.startswith("META_ACCESS_TOKEN="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def _fetch_ad_thumbnail(ad_id: str, token: str) -> str:
    """Fetch single ad thumbnail. Returns URL or empty string."""
    if not ad_id or not token:
        return ""
    try:
        r = requests.get(
            f"https://graph.facebook.com/v23.0/{ad_id}",
            params={"fields": "creative{thumbnail_url,image_url}", "access_token": token},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            creative = data.get("creative", {})
            return creative.get("image_url") or creative.get("thumbnail_url", "")
    except Exception:
        pass
    return ""


def _load_thumbnail_cache() -> dict:
    cache_path = DATA_DIR / "meta_thumbnails_cache.json"
    if cache_path.exists():
        try:
            return json.loads(cache_path.read_text())
        except Exception:
            return {}
    return {}


def _save_thumbnail_cache(cache: dict) -> None:
    cache_path = DATA_DIR / "meta_thumbnails_cache.json"
    try:
        cache_path.write_text(json.dumps(cache))
    except Exception:
        pass


@app.get("/api/my-top-ads")
async def my_top_ads():
    """
    Pull MNG's own top static ads from last 7 days (via Ad Doctor data),
    enrichi avec recommandations d'itération Andromeda-safe + thumbnails Meta.
    """
    data_path = DATA_DIR / "mng_top_7d.json"
    if not data_path.exists():
        return {"ads": [], "error": "Data not available. Run Ad Doctor first."}

    try:
        ads = json.loads(data_path.read_text())
    except Exception as e:
        return {"ads": [], "error": f"Failed to load data: {e}"}

    # Fetch thumbnails for all ads (with caching)
    token = _get_meta_token()
    thumb_cache = _load_thumbnail_cache()
    thumbs_to_fetch = [a.get("ad_id") for a in ads if a.get("ad_id") and a.get("ad_id") not in thumb_cache]

    if thumbs_to_fetch and token:
        # Parallel fetch with thread pool
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ad_id: ex.submit(_fetch_ad_thumbnail, ad_id, token) for ad_id in thumbs_to_fetch}
            for ad_id, fut in futures.items():
                try:
                    thumb_cache[ad_id] = fut.result(timeout=10)
                except Exception:
                    thumb_cache[ad_id] = ""
        _save_thumbnail_cache(thumb_cache)

    # Filter statics only (S_ prefix or no V_ prefix), enrich with iteration ideas
    enriched = []
    for ad in ads:
        name = (ad.get("ad_name") or "").lower()
        # Detect static vs video
        is_static = name.startswith("s_") or "16/02 s_" in name or (
            not name.startswith("v_") and "_facecam" not in name and "broll" not in name
        )
        # Generate Andromeda-safe iteration recommendations based on verdict
        verdict = ad.get("verdict", "WATCH")
        cpa = ad.get("cpa", 0) or 0
        purchases = ad.get("purchases", 0)
        spend = ad.get("spend", 0)

        iterations = build_andromeda_iterations(ad, is_static)

        enriched.append({
            "ad_id": ad.get("ad_id"),
            "name": ad.get("ad_name", "?"),
            "thumbnail": thumb_cache.get(ad.get("ad_id"), ""),
            "campaign": ad.get("campaign_name", ""),
            "adset": ad.get("adset_name", ""),
            "spend": round(spend, 2),
            "purchases": purchases,
            "cpa": round(cpa, 2) if cpa and cpa != float('inf') else None,
            "roas": round(ad.get("roas", 0) or 0, 2),
            "ctr": round(ad.get("ctr", 0) or 0, 2),
            "hook_rate": round(ad.get("hook_rate", 0) or 0, 1),
            "frequency": round(ad.get("frequency", 0) or 0, 2),
            "verdict": verdict,
            "verdict_reason": ad.get("verdict_reason", ""),
            "iteration_level": ad.get("iteration_level", 0),
            "is_static": is_static,
            "iterations": iterations,
        })

    return {
        "ads": enriched,
        "total_spend_7d": round(sum(a["spend"] for a in enriched), 2),
        "total_purchases_7d": sum(a["purchases"] for a in enriched),
        "winners": [a for a in enriched if a["verdict"] in ("SCALE", "WINNER")],
        "watch": [a for a in enriched if a["verdict"] == "WATCH"],
        "iterate": [a for a in enriched if a["verdict"] == "ITERATE"],
        "kill": [a for a in enriched if a["verdict"] == "KILL"],
    }


def build_andromeda_iterations(ad: dict, is_static: bool) -> list[dict]:
    """
    Génère des recommandations Andromeda-safe basées sur les VRAIES règles MNG.

    Seuils MNG (config Ad Doctor) :
    - cpa_target: 27€ · cpa_breakeven: 37€ · cpa_kill: 45€
    - freq_scale_max: 2.5 · freq_iterate: 2.8 · freq_kill: 3.5
    - min_purchases_scale: 10 · min_purchases_decision: 5

    Niveaux d'itération (du classifier) :
    - Niveau 1 (hook_rate < 15%) → nouveau hook
    - Niveau 2 (hook_rate ok mais freq monte) → nouveau visuel
    - Niveau 3 (freq > 3.2) → nouvel angle/concept
    """
    name = (ad.get("ad_name") or "").lower()
    verdict = ad.get("verdict", "WATCH")
    iter_level = ad.get("iteration_level", "")
    cpa = ad.get("cpa", 0) or 0
    hook_rate = ad.get("hook_rate", 0) or 0
    ctr = ad.get("ctr", 0) or 0
    frequency = ad.get("frequency", 0) or 0
    purchases = ad.get("purchases", 0) or 0
    spend = ad.get("spend", 0) or 0

    # Detect angle from name
    angle = "ballonnements"
    if "intestin" in name: angle = "intestin"
    elif "stress" in name or "anxiete" in name: angle = "stress"
    elif "focus" in name: angle = "focus"
    elif "energie" in name or "crash" in name: angle = "crash_energie"
    elif "ventre" in name or "douleur" in name: angle = "douleur_ventre"

    iterations = []

    # ==================== SCALE ====================
    # Règle: cpa < 27€ + purchases >= 10 + freq < 2.5
    if verdict == "SCALE":
        iterations.append({
            "type": "scale_budget",
            "priority": "critical",
            "title": "🚀 SCALE — augmenter le budget",
            "rationale": f"CPA {cpa}€ < target 27€, {purchases} achats, freq {frequency} sous saturation. C'est un winner confirmé.",
            "action": "Monter budget adset +20-30% par jour. Surveiller freq — si dépasse 2.5, pause scaling.",
        })
        iterations.append({
            "type": "format_pivot_andromeda",
            "priority": "high",
            "title": "Format pivot (Andromeda-safe)",
            "rationale": "Pour multiplier sans cannibaliser : nouveau ratio = nouvelle entité créa pour Andromeda.",
            "action": f"Recréer en autre format (4:5 → 9:16 ou 1:1). MÊME angle '{angle}', composition adaptée au ratio.",
        })
        iterations.append({
            "type": "persona_widening",
            "priority": "medium",
            "title": "Élargir l'audience",
            "rationale": "Adset actuel performe — tester en CBO ou audience plus large pour scale sans saturer.",
            "action": "Dupliquer l'adset avec audience +30% large (LAL 5%, intérêts adjacents)",
        })

    # ==================== ITERATE ====================
    # Règle: cpa <= 45€ + freq entre 2.8 et 3.5 (fatigue imminente)
    elif verdict == "ITERATE":
        # Niveau 3 — Freq > 3.2 → nouvel angle
        if frequency > 3.2:
            iterations.append({
                "type": "iter_level_3",
                "priority": "critical",
                "title": "🔄 Niveau 3 — Nouvel angle",
                "rationale": f"Freq {frequency} critique. L'audience a saturé. Ré-itérer = brûler le budget.",
                "action": _angle_pivot_recommendation(angle),
            })
        # Niveau 1 — hook rate faible
        elif hook_rate < 15:
            iterations.append({
                "type": "iter_level_1",
                "priority": "critical",
                "title": "🪝 Niveau 1 — Nouveau hook",
                "rationale": f"Hook rate {hook_rate}% < 15%. Le contenu est bon mais l'attention pas captée.",
                "action": "Garder le body/CTA. Refaire UNIQUEMENT la 1ère seconde avec un pattern interrupt (close-up, texte géant, son inattendu).",
            })
        # Niveau 2 — visuel à varier
        else:
            iterations.append({
                "type": "iter_level_2",
                "priority": "high",
                "title": "🎨 Niveau 2 — Nouveau visuel",
                "rationale": f"Freq {frequency} en hausse, hook ok ({hook_rate}%). Varier le visuel = nouvelle entité Andromeda.",
                "action": "Garder hook + script. Changer fond, couleurs, layout, ou décor. Andromeda voit ça comme nouvelle créa.",
            })

    # ==================== KILL ====================
    # Règle: cpa > 45€ OU 0 achat à 75€+ OU freq > 3.5
    elif verdict == "KILL":
        if frequency > 3.5:
            kill_reason = f"Freq {frequency} > 3.5 — audience cramée"
            kill_action = "Pause cette ad. Si l'angle marchait avant, reprendre dans 30j avec nouveau visuel ET nouvelle audience."
        elif cpa > 45 and cpa != float('inf'):
            kill_reason = f"CPA {cpa}€ > kill 45€"
            kill_action = "Pause. Le concept ne convertit pas — pas d'itération, partir sur un autre angle."
        else:
            kill_reason = f"{spend:.0f}€ dépensés, 0 achat"
            kill_action = "Pause direct. Pas d'audience trouvée — concept mort."

        iterations.append({
            "type": "kill_pause",
            "priority": "critical",
            "title": "☠️ KILL — pause immédiate",
            "rationale": kill_reason,
            "action": kill_action,
        })

    # ==================== WATCH ====================
    # Règle: zone grise — pas assez de data ou metrics entre target/kill
    else:  # WATCH
        # Règle d'or absolue : pas de décision tant que pas assez de data
        if spend < 75 and purchases == 0:
            iterations.append({
                "type": "watch_no_data",
                "priority": "low",
                "title": "⏳ Attendre — règle 75€",
                "rationale": f"{spend:.0f}€ dépensés, 0 achat. Règle MNG : attendre 75€ avant kill si 0 vente.",
                "action": f"Continuer à laisser tourner jusqu'à 75€ ou 1ère vente. Ne PAS itérer maintenant — pas assez de data.",
            })
        elif purchases > 0 and purchases < 5:
            iterations.append({
                "type": "watch_pre_decision",
                "priority": "low",
                "title": "⏳ Attendre 5 ventes",
                "rationale": f"{purchases} ventes. Règle MNG : attendre 5 ventes minimum avant toute décision.",
                "action": "Laisser tourner. CPA actuel pas représentatif tant que < 5 conversions.",
            })
        elif cpa != float('inf') and 27 <= cpa <= 45:
            # Zone grise breakeven
            iterations.append({
                "type": "watch_grey_zone",
                "priority": "medium",
                "title": "🤔 Zone grise — surveiller",
                "rationale": f"CPA {cpa}€ entre target (27€) et kill (45€). Pas assez bon pour scale, pas assez mauvais pour kill.",
                "action": f"Si freq < 2.8 : laisser maturer 3-5j. Si freq monte vers 2.8 : pré-itérer (Niveau 2 visuel) avant fatigue.",
            })

    return iterations


def _angle_pivot_recommendation(current_angle: str) -> str:
    pivots = {
        "ballonnements": "Tester angle 'intestin-cerveau' (nouveau) ou 'ventre plat sans régime' (latéral)",
        "intestin": "Tester angle 'digestion lente' ou 'transit' (latéral même pain)",
        "stress": "Tester angle 'sommeil' ou 'fatigue mentale' (chaîne causale)",
        "focus": "Tester angle 'productivité' ou 'fatigue cognitive' (latéral)",
        "crash_energie": "Tester angle '14h crash' (spécifique) ou '4ème café' (situationnel)",
        "douleur_ventre": "Tester angle 'inconfort post-repas' ou 'spasmes' (variation pain)",
    }
    return pivots.get(current_angle, "Tester un angle latéral même persona, même pain mais formulé différemment")


@app.get("/api/trendtrack/usage")
async def trendtrack_usage():
    """Get TrendTrack credit balance."""
    if not TRENDTRACK_API_KEY:
        raise HTTPException(503, "TrendTrack API not configured")
    headers = {"Authorization": f"Bearer {TRENDTRACK_API_KEY}"}
    try:
        r = requests.get(f"{TRENDTRACK_BASE}/v1/usage", headers=headers, timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        raise HTTPException(500, f"TrendTrack usage failed: {str(e)}")


# --- API: Inspiration Library ---
INSPO_DIR = DATA_DIR / "inspirations"
INSPO_DIR.mkdir(parents=True, exist_ok=True)

@app.get("/api/inspirations")
async def list_inspirations():
    conn = get_db()
    rows = conn.execute("SELECT * FROM inspirations ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/inspirations")
async def upload_inspiration(
    brand_id: str = Form(...),
    label: str = Form(""),
    source: str = Form(""),
    tags: str = Form("[]"),
    images: list[UploadFile] = File(...),
):
    conn = get_db()
    added = []
    brand_inspo_dir = INSPO_DIR / brand_id
    brand_inspo_dir.mkdir(parents=True, exist_ok=True)
    for img_file in images:
        if img_file.filename:
            img_id = str(uuid.uuid4())
            ext = Path(img_file.filename).suffix.lower()
            if ext not in IMAGE_EXTENSIONS:
                continue
            filename = f"{img_id}{ext}"
            filepath = brand_inspo_dir / filename
            content = await img_file.read()
            filepath.write_bytes(content)
            conn.execute(
                "INSERT INTO inspirations (id, brand_id, filename, filepath, label, source, tags) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (img_id, brand_id, img_file.filename, str(filepath), label, source, tags)
            )
            added.append({"id": img_id, "filename": img_file.filename})
    conn.commit()
    conn.close()
    return added

@app.delete("/api/inspirations/{inspo_id}")
async def delete_inspiration(inspo_id: str):
    conn = get_db()
    img = conn.execute("SELECT * FROM inspirations WHERE id = ?", (inspo_id,)).fetchone()
    if img:
        filepath = Path(img["filepath"])
        if filepath.exists():
            filepath.unlink()
        conn.execute("DELETE FROM inspirations WHERE id = ?", (inspo_id,))
        conn.commit()
    conn.close()
    return {"ok": True}

@app.get("/api/inspiration-image/{inspo_id}")
async def serve_inspiration_image(inspo_id: str):
    conn = get_db()
    img = conn.execute("SELECT filepath FROM inspirations WHERE id = ?", (inspo_id,)).fetchone()
    conn.close()
    if not img:
        raise HTTPException(404, "Inspiration not found")
    filepath = Path(img["filepath"])
    if not filepath.exists():
        raise HTTPException(404, "File not found")
    return FileResponse(str(filepath))


# --- API: Winner/Kill Rating ---
@app.post("/api/generated-images/{image_id}/rate")
async def rate_image(image_id: str, data: dict):
    rating = data.get("rating")  # "winner", "kill", or null
    if rating not in ("winner", "kill", None):
        raise HTTPException(400, "Rating must be 'winner', 'kill', or null")
    conn = get_db()
    conn.execute("UPDATE generated_images SET rating = ? WHERE id = ?", (rating, image_id))
    comment = data.get("comment", None)
    if comment is not None:
        conn.execute("UPDATE generated_images SET comment = ? WHERE id = ?", (comment, image_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.put("/api/generated-images/{image_id}/comment")
async def update_image_comment(image_id: str, request: Request):
    data = await request.json()
    comment = data.get("comment", "")
    conn = get_db()
    conn.execute("UPDATE generated_images SET comment = ? WHERE id = ?", (comment, image_id))
    conn.commit()
    conn.close()
    return {"ok": True}


@app.get("/api/feedback-context")
async def get_feedback_context():
    conn = get_db()
    rows = conn.execute("""
        SELECT gi.rating, gi.comment, gi.prompt_used, g.aspect_ratio
        FROM generated_images gi
        JOIN generations g ON gi.generation_id = g.id
        WHERE gi.rating IS NOT NULL
        ORDER BY gi.created_at DESC LIMIT 30
    """).fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.get("/api/winners")
async def list_winners():
    """Get all winner-rated images for learning."""
    conn = get_db()
    rows = conn.execute(
        """SELECT gi.*, g.marketing_messages, g.custom_prompt, g.aspect_ratio
           FROM generated_images gi
           JOIN generations g ON gi.generation_id = g.id
           WHERE gi.rating = 'winner'
           ORDER BY gi.created_at DESC"""
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# --- API: Prompt Templates ---
@app.get("/api/templates")
async def list_templates():
    conn = get_db()
    rows = conn.execute("SELECT * FROM prompt_templates ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]

@app.post("/api/templates")
async def create_template(data: dict):
    tpl_id = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        """INSERT INTO prompt_templates (id, name, angle, prompt_text, persona, desire, awareness, composition_style)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (tpl_id, data.get("name", ""), data.get("angle", ""), data.get("prompt_text", ""),
         data.get("persona", ""), data.get("desire", ""), data.get("awareness", ""),
         data.get("composition_style", ""))
    )
    conn.commit()
    conn.close()
    return {"id": tpl_id}

@app.delete("/api/templates/{tpl_id}")
async def delete_template(tpl_id: str):
    conn = get_db()
    conn.execute("DELETE FROM prompt_templates WHERE id = ?", (tpl_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# --- API: Brand Strategy ---
@app.get("/api/brands/{brand_id}/strategy")
async def get_brand_strategy(brand_id: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM brand_strategy WHERE brand_id = ?", (brand_id,)).fetchone()
    conn.close()
    if not row:
        return None
    result = dict(row)
    # Parse JSON fields for the response
    for field in ("avatar_pain_points", "avatar_desires", "offers"):
        try:
            result[field] = json.loads(result[field])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


@app.post("/api/brands/{brand_id}/strategy")
async def upsert_brand_strategy(brand_id: str, data: dict):
    conn = get_db()
    # Verify brand exists
    brand = conn.execute("SELECT id FROM brands WHERE id = ?", (brand_id,)).fetchone()
    if not brand:
        conn.close()
        raise HTTPException(404, "Brand not found")

    existing = conn.execute("SELECT id FROM brand_strategy WHERE brand_id = ?", (brand_id,)).fetchone()

    # Serialize list fields to JSON strings
    for field in ("avatar_pain_points", "avatar_desires", "offers"):
        if field in data and isinstance(data[field], list):
            data[field] = json.dumps(data[field])

    if existing:
        sets = []
        vals = []
        for key in ("avatar_description", "avatar_pain_points", "avatar_desires", "usp", "offers", "tone_of_voice"):
            if key in data:
                sets.append(f"{key} = ?")
                vals.append(data[key])
        if sets:
            sets.append("updated_at = datetime('now')")
            vals.append(brand_id)
            conn.execute(f"UPDATE brand_strategy SET {', '.join(sets)} WHERE brand_id = ?", vals)
        strategy_id = existing["id"]
    else:
        strategy_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO brand_strategy (id, brand_id, avatar_description, avatar_pain_points, avatar_desires, usp, offers, tone_of_voice)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                strategy_id,
                brand_id,
                data.get("avatar_description", ""),
                data.get("avatar_pain_points", "[]"),
                data.get("avatar_desires", "[]"),
                data.get("usp", ""),
                data.get("offers", "[]"),
                data.get("tone_of_voice", ""),
            )
        )

    conn.commit()
    row = conn.execute("SELECT * FROM brand_strategy WHERE id = ?", (strategy_id,)).fetchone()
    conn.close()
    result = dict(row)
    for field in ("avatar_pain_points", "avatar_desires", "offers"):
        try:
            result[field] = json.loads(result[field])
        except (json.JSONDecodeError, TypeError):
            pass
    return result


# --- API: Competitors ---
@app.get("/api/brands/{brand_id}/competitors")
async def list_competitors(brand_id: str):
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM competitors WHERE brand_id = ? ORDER BY created_at DESC",
        (brand_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/brands/{brand_id}/competitors")
async def create_competitor(brand_id: str, data: dict):
    conn = get_db()
    brand = conn.execute("SELECT id FROM brands WHERE id = ?", (brand_id,)).fetchone()
    if not brand:
        conn.close()
        raise HTTPException(404, "Brand not found")

    comp_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO competitors (id, brand_id, name, url, type, notes, market) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (comp_id, brand_id, data.get("name", ""), data.get("url", ""),
         data.get("type", "direct"), data.get("notes", ""), data.get("market", "EU"))
    )
    conn.commit()
    row = conn.execute("SELECT * FROM competitors WHERE id = ?", (comp_id,)).fetchone()
    conn.close()
    return dict(row)


@app.delete("/api/competitors/{comp_id}")
async def delete_competitor(comp_id: str):
    conn = get_db()
    conn.execute("DELETE FROM competitors WHERE id = ?", (comp_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# --- API: Generate Strategy Angles ---
@app.post("/api/brands/{brand_id}/generate-strategy")
async def generate_strategy_angles(brand_id: str):
    """Use brand DNA + strategy + competitors to suggest the best creative angles/hooks."""
    conn = get_db()
    brand = conn.execute("SELECT * FROM brands WHERE id = ?", (brand_id,)).fetchone()
    if not brand:
        conn.close()
        raise HTTPException(404, "Brand not found")

    strategy = conn.execute("SELECT * FROM brand_strategy WHERE brand_id = ?", (brand_id,)).fetchone()
    competitors = conn.execute("SELECT * FROM competitors WHERE brand_id = ?", (brand_id,)).fetchall()
    conn.close()

    # Build suggested angles from strategy data
    suggested_angles = []

    if strategy:
        pain_points = json.loads(strategy["avatar_pain_points"]) if strategy["avatar_pain_points"] else []
        desires = json.loads(strategy["avatar_desires"]) if strategy["avatar_desires"] else []
        usp = strategy["usp"] or ""
        avatar = strategy["avatar_description"] or ""

        # Pain point angles (bottom of funnel — problem aware)
        for i, pain in enumerate(pain_points[:5]):
            suggested_angles.append({
                "angle": pain,
                "headline": generate_headline(pain),
                "funnel_stage": "problem_aware",
                "rationale": f"Cible directement un pain point de l'avatar ({avatar[:60]}...). Accroche emotionnelle forte.",
            })

        # Desire angles (middle of funnel — solution aware)
        for desire in desires[:5]:
            suggested_angles.append({
                "angle": desire,
                "headline": generate_headline(desire),
                "funnel_stage": "solution_aware",
                "rationale": f"Projette l'avatar vers le resultat desire. Positif et aspirationnel.",
            })

        # USP angle (bottom of funnel — product aware)
        if usp:
            suggested_angles.append({
                "angle": usp,
                "headline": generate_headline(usp),
                "funnel_stage": "product_aware",
                "rationale": "Met en avant le differenciateur cle du produit face aux concurrents.",
            })

        # Competitor differentiation angles
        for comp in competitors[:3]:
            comp_name = comp["name"]
            suggested_angles.append({
                "angle": f"Pourquoi {brand['name']} et pas {comp_name}",
                "headline": generate_headline("alternative_cafe"),
                "funnel_stage": "product_aware",
                "rationale": f"Angle comparatif vs {comp_name} ({comp['type']}). Positionne la marque comme superieure.",
            })

    else:
        # No strategy data — generate generic angles from brand DNA
        brand_dna = brand["brand_dna"] or ""
        for angle_key, headlines in ANGLE_HEADLINES.items():
            suggested_angles.append({
                "angle": angle_key,
                "headline": random.choice(headlines),
                "funnel_stage": "problem_aware",
                "rationale": f"Angle generique base sur le theme '{angle_key}'. Ajoutez une strategie de marque pour des suggestions plus precises.",
            })

    return {"suggested_angles": suggested_angles}


# --- API: Batch Angles (generate all angles at once) ---
@app.post("/api/generate-batch-angles")
async def generate_batch_angles(
    brand_id: str = Form(...),
    product_id: str = Form(...),
    aspect_ratio: str = Form("4:5"),
    resolution: str = Form("2K"),
):
    """Generate 1 creative per angle (8 total)."""
    if not FAL_KEY:
        raise HTTPException(400, "FAL_KEY not configured.")

    angles = [
        "ballonnements & digestion",
        "crash du cafe, fatigue apres-midi",
        "manque de focus et concentration",
        "stress et anxiete au quotidien",
        "energie stable toute la journee",
        "alternative naturelle au cafe",
        "sommeil de mauvaise qualite",
        "ingredients 100% naturels, zero cochonnerie",
    ]

    gen_ids = []
    conn = get_db()
    for angle in angles:
        gen_id = str(uuid.uuid4())
        conn.execute(
            """INSERT INTO generations
               (id, brand_id, product_id, num_creations, aspect_ratio, resolution,
                marketing_messages, customer_reviews, custom_prompt, status)
               VALUES (?, ?, ?, 1, ?, ?, ?, '[]', '', 'pending')""",
            (gen_id, brand_id, product_id, aspect_ratio, resolution, json.dumps([angle]))
        )
        gen_ids.append(gen_id)
    conn.commit()
    conn.close()

    # Launch all generations in background threads
    for gen_id in gen_ids:
        thread = Thread(target=run_generation, args=(gen_id,), daemon=True)
        thread.start()
        time.sleep(0.5)  # Stagger slightly to avoid rate limits

    return {"gen_ids": gen_ids, "count": len(gen_ids)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8899, reload=True)
