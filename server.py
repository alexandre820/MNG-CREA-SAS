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
import secrets
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
        # Tagging + status + naming + persona linking
        "ALTER TABLE generated_images ADD COLUMN tags TEXT DEFAULT '{}'",  # JSON: 8-cat tags
        "ALTER TABLE generated_images ADD COLUMN status_tag TEXT DEFAULT 'draft'",  # draft|to_test|winner|killed
        "ALTER TABLE generated_images ADD COLUMN naming TEXT DEFAULT ''",  # auto-generated naming convention
        "ALTER TABLE generated_images ADD COLUMN variant_parent_id TEXT DEFAULT NULL",  # for A/B variants
        "ALTER TABLE generated_images ADD COLUMN persona_id TEXT DEFAULT NULL",
        "ALTER TABLE generations ADD COLUMN persona_id TEXT DEFAULT NULL",
        "ALTER TABLE generations ADD COLUMN generation_kind TEXT DEFAULT 'standard'",  # standard|persona_batch|ab_variants|hook_gen
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

        -- Personas
        CREATE TABLE IF NOT EXISTS personas (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            description TEXT DEFAULT '',
            age_range TEXT DEFAULT '',
            pain_points TEXT DEFAULT '[]',
            desires TEXT DEFAULT '[]',
            is_default INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Expert insights — comments on winners that train Expert Static skill
        CREATE TABLE IF NOT EXISTS expert_insights (
            id TEXT PRIMARY KEY,
            brand_id TEXT,
            image_id TEXT,
            source TEXT DEFAULT 'mng',
            competitor_name TEXT DEFAULT '',
            why_it_works TEXT NOT NULL,
            pattern_type TEXT DEFAULT '',
            tags TEXT DEFAULT '[]',
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Auto briefs — extracted from Meta Ads Library / TrendTrack
        CREATE TABLE IF NOT EXISTS auto_briefs (
            id TEXT PRIMARY KEY,
            brand_id TEXT,
            source_url TEXT NOT NULL,
            source_type TEXT DEFAULT 'meta_ads_library',
            competitor_name TEXT DEFAULT '',
            extracted_copy TEXT DEFAULT '',
            extracted_headline TEXT DEFAULT '',
            visual_path TEXT DEFAULT '',
            detected_tags TEXT DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- Snapshots — shareable reports
        CREATE TABLE IF NOT EXISTS snapshots (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            type TEXT DEFAULT 'studio',
            payload TEXT NOT NULL,
            public_token TEXT UNIQUE,
            is_live INTEGER DEFAULT 0,
            created_at TEXT DEFAULT (datetime('now'))
        );

        -- AI Tasks — predefined query workflows
        CREATE TABLE IF NOT EXISTS ai_tasks (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            query_kind TEXT NOT NULL,
            last_run TEXT,
            last_result TEXT DEFAULT '{}',
            is_default INTEGER DEFAULT 0,
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


def seed_defaults():
    """Seed 4 default personas + default AI tasks for MNG."""
    conn = get_db()
    # Personas
    if conn.execute("SELECT COUNT(*) FROM personas").fetchone()[0] == 0:
        defaults = [
            (str(uuid.uuid4()), "Entrepreneur 30-45", "Cadre/dirigeant urbain, rythme intense, recherche focus + énergie sans crash", "30-45",
             json.dumps(["stress chronique", "crash énergie 14h", "manque de focus", "fatigue mentale"]),
             json.dumps(["focus stable 10h", "énergie sans nervosité", "alternative au 4ème café", "performance mentale"])),
            (str(uuid.uuid4()), "Active Woman 28-40", "Nana active 28-40 qui fait tout bien (sport, alim) mais perd pas de ventre, problème digestion/ballonnements", "28-40",
             json.dumps(["ballonnements", "ventre gonflé", "digestion lente", "stress qui touche l'intestin"]),
             json.dumps(["ventre plat", "digestion fluide", "énergie stable", "naturel sans diète"])),
            (str(uuid.uuid4()), "Étudiant stressé 18-28", "Étudiant en école/fac qui révise tard, anxiété + concentration", "18-28",
             json.dumps(["anxiété examens", "concentration courte", "fatigue cognitive", "stress permanent"]),
             json.dumps(["concentration longue", "calme mental", "mémorisation", "alternative red bull"])),
            (str(uuid.uuid4()), "Sportif perfectionniste 25-45", "Sportif amateur/semi-pro qui veut perf + récup, alimentation propre", "25-45",
             json.dumps(["récupération lente", "fatigue post-training", "ballonnements pré-séance", "manque de focus en compétition"]),
             json.dumps(["récup rapide", "performance pic", "endurance mentale", "ingrédients clean label"])),
        ]
        for p in defaults:
            conn.execute("INSERT INTO personas (id, name, description, age_range, pain_points, desires, is_default) VALUES (?,?,?,?,?,?,1)", p)
        print("[SEED] Default personas created", flush=True)

    # AI Tasks
    if conn.execute("SELECT COUNT(*) FROM ai_tasks").fetchone()[0] == 0:
        tasks = [
            (str(uuid.uuid4()), "\U0001F4CA Quel angle convertit le mieux ce mois-ci ?", "best_angle"),
            (str(uuid.uuid4()), "\U0001F465 Quelle persona scale le plus ?", "best_persona"),
            (str(uuid.uuid4()), "\U0001F3A8 Mes UGC vs Statics : qui gagne ?", "ugc_vs_static"),
            (str(uuid.uuid4()), "\U0001F3C6 Top 5 hooks de mes winners", "top_hooks"),
        ]
        for t in tasks:
            conn.execute("INSERT INTO ai_tasks (id, name, query_kind, is_default) VALUES (?,?,?,1)", t)
        print("[SEED] Default AI tasks created", flush=True)

    conn.commit()
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
            prompt = f"IMPORTANT: All text overlays and headlines must be in English.\n\n{prompt}"
        try:
            bias = get_winner_bias_for_prompt()
            if bias:
                prompt = prompt + "\n" + bias
        except NameError:
            pass
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
    seed_defaults()
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

    headers = {"Authorization": f"Bearer {TRENDTRACK_API_KEY}"}

    # Brands à inclure (mushroom coffee + suppléments + DTC FR de référence)
    TARGET_BRANDS = [
        ("RYZE Superfoods", "db2a6deb-2452-4e2a-87dc-e96650f60459"),
        ("Bonjour", "af35a925-7fe2-43c3-8565-e40bf7b01502"),
        ("French Mush", "0e463119-4c59-4162-9089-e47906b1b0a4"),
        ("Wake Nutrition", "5cc26b79-2f5e-42a5-bf06-d040e2e9dd32"),
        ("Dyna", "345b1ac1-304c-43b7-8c7a-85dddce3e222"),
        ("AG1", "3b1b2e9c-7873-40c7-9442-a4d37ce412ab"),
        ("Everyday Dose", "461e5c73-ca20-4ae2-8072-2bc1ed0a8343"),
        ("Four Sigmatic", "aab4cbf7-ba6d-42c0-9507-70900b1f6de4"),
        ("Spacegoods", "81464660-b02b-420a-bfa7-43a06929199c"),
        ("Naali", "2b2a7ce5-4854-406c-8218-e173773dccae"),
        ("Humble+", "52886604-cfee-43e9-b88e-f71980d1f01b"),
        ("IM8 Health", "74dd7484-2a3f-449b-8174-c47e21b4efd5"),
        ("Goli", "89223c90-e4b8-4c19-9f21-c0605c31a143"),
        ("900.care", "c3ed97e7-5a3e-4004-aeac-d3a905ec810c"),
        ("My Variations", "51acb0af-a258-4721-b055-ee4d5dd13de2"),
        ("Flytex", "c045ee98-7e2c-4442-a10b-91e0045f6980"),
        ("Lilly Skin", "98ea7adc-1239-4ed3-97ab-ddfad3bc0fcd"),
        ("Fincut", "569832a7-e4dc-4630-8311-1abf4bf20642"),
        ("Pacha", "30471bb0-3a8d-41df-b55e-dccf05a42219"),
    ]

    per_brand_cap = 3
    all_ads = []
    credits_remaining = "?"

    # Pull top ads per brand in parallel
    from concurrent.futures import ThreadPoolExecutor

    def fetch_brand_ads(name_id):
        name, brand_id = name_id
        params = {"sortBy": period, "limit": per_brand_cap}
        if media_type and media_type != "all":
            params["mediaType"] = media_type
        try:
            r = requests.get(
                f"{TRENDTRACK_BASE}/v1/brandtrackers/{brand_id}/top-ads",
                headers=headers, params=params, timeout=30,
            )
            if r.status_code == 429:
                # Rate limited — wait and retry once
                import time as _t
                _t.sleep(1)
                r = requests.get(
                    f"{TRENDTRACK_BASE}/v1/brandtrackers/{brand_id}/top-ads",
                    headers=headers, params=params, timeout=30,
                )
            if r.status_code != 200:
                return [], r.headers.get("X-Credits-Remaining", "?"), name
            return r.json().get("data", []), r.headers.get("X-Credits-Remaining", "?"), name
        except Exception as e:
            return [], "?", name

    # Smaller batches to avoid rate limit
    with ThreadPoolExecutor(max_workers=4) as ex:
        results = list(ex.map(fetch_brand_ads, TARGET_BRANDS))

    for result, (brand_name, brand_id) in zip(results, TARGET_BRANDS):
        data_items, creds, _ = result
        if creds and creds != "?":
            credits_remaining = creds
        for item in (data_items or []):
            if not item:
                continue
            ad = item.get("ad") or {}
            metrics = (item.get("metrics") or ad.get("metrics") or {})
            rank = ad.get("rank") or {}
            content = ad.get("content") or {}
            media = ad.get("media") or {}
            tt_thumb = media.get("thumbnailUrl", "")
            advertiser = ad.get("advertiser", {})
            from urllib.parse import quote
            all_ads.append({
                "id": ad.get("id", ""),
                "brand_name": brand_name,
                "brand_logo": advertiser.get("logoUrl", ""),
                "thumbnail": f"/api/proxy-image?url={quote(tt_thumb, safe='')}" if tt_thumb else "",
                "thumbnail_raw": tt_thumb,
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
                "facebook_page_id": advertiser.get("facebookPageId", ""),
            })

    return {
        "ads": all_ads,
        "credits_remaining": credits_remaining,
        "period": period,
        "media_type": media_type,
        "brands_count": len(TARGET_BRANDS),
        "per_brand_cap": per_brand_cap,
    }


@app.get("/api/proxy-image")
async def proxy_image(url: str):
    """Proxy external images to bypass CORS / hotlink restrictions."""
    try:
        r = requests.get(url, timeout=10, stream=True, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        })
        r.raise_for_status()
        content_type = r.headers.get("content-type", "image/jpeg")
        from fastapi.responses import Response
        return Response(content=r.content, media_type=content_type, headers={
            "Cache-Control": "public, max-age=86400"
        })
    except Exception as e:
        raise HTTPException(404, f"Image fetch failed: {e}")


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


# ===== Tags + Status + Naming =====

@app.get("/api/generated-images/{image_id}/tags")
async def get_image_tags(image_id: str):
    conn = get_db()
    row = conn.execute("SELECT tags, status_tag, naming, persona_id FROM generated_images WHERE id=?", (image_id,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    return {
        "tags": json.loads(row["tags"] or "{}"),
        "status_tag": row["status_tag"],
        "naming": row["naming"],
        "persona_id": row["persona_id"],
    }


@app.put("/api/generated-images/{image_id}/tags")
async def update_image_tags(image_id: str, request: Request):
    data = await request.json()
    conn = get_db()
    if "tags" in data:
        conn.execute("UPDATE generated_images SET tags=? WHERE id=?", (json.dumps(data["tags"]), image_id))
    if "status_tag" in data:
        conn.execute("UPDATE generated_images SET status_tag=? WHERE id=?", (data["status_tag"], image_id))
    if "naming" in data:
        conn.execute("UPDATE generated_images SET naming=? WHERE id=?", (data["naming"], image_id))
    conn.commit()
    conn.close()
    return {"ok": True}


# ===== Personas =====

@app.get("/api/personas")
async def list_personas():
    conn = get_db()
    rows = conn.execute("SELECT * FROM personas ORDER BY is_default DESC, created_at ASC").fetchall()
    conn.close()
    return [{
        "id": r["id"], "name": r["name"], "description": r["description"],
        "age_range": r["age_range"],
        "pain_points": json.loads(r["pain_points"] or "[]"),
        "desires": json.loads(r["desires"] or "[]"),
        "is_default": bool(r["is_default"]),
    } for r in rows]


@app.post("/api/personas")
async def create_persona(request: Request):
    data = await request.json()
    pid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("INSERT INTO personas (id, name, description, age_range, pain_points, desires) VALUES (?,?,?,?,?,?)",
        (pid, data["name"], data.get("description", ""), data.get("age_range", ""),
         json.dumps(data.get("pain_points", [])), json.dumps(data.get("desires", []))))
    conn.commit()
    conn.close()
    return {"id": pid}


@app.delete("/api/personas/{persona_id}")
async def delete_persona(persona_id: str):
    conn = get_db()
    conn.execute("DELETE FROM personas WHERE id=? AND is_default=0", (persona_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ===== Expert Insights =====

@app.get("/api/expert-insights")
async def list_expert_insights(brand_id: Optional[str] = None, source: Optional[str] = None):
    conn = get_db()
    q = "SELECT * FROM expert_insights WHERE 1=1"
    params = []
    if brand_id:
        q += " AND brand_id=?"
        params.append(brand_id)
    if source:
        q += " AND source=?"
        params.append(source)
    q += " ORDER BY created_at DESC"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/expert-insights")
async def create_expert_insight(request: Request):
    data = await request.json()
    iid = str(uuid.uuid4())
    conn = get_db()
    conn.execute(
        "INSERT INTO expert_insights (id, brand_id, image_id, source, competitor_name, why_it_works, pattern_type, tags) VALUES (?,?,?,?,?,?,?,?)",
        (iid, data.get("brand_id"), data.get("image_id"), data.get("source", "mng"),
         data.get("competitor_name", ""), data["why_it_works"], data.get("pattern_type", ""),
         json.dumps(data.get("tags", []))))
    conn.commit()
    conn.close()
    return {"id": iid}


@app.delete("/api/expert-insights/{insight_id}")
async def delete_expert_insight(insight_id: str):
    conn = get_db()
    conn.execute("DELETE FROM expert_insights WHERE id=?", (insight_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ===== Auto Briefs =====

@app.get("/api/auto-briefs")
async def list_briefs(brand_id: Optional[str] = None):
    conn = get_db()
    q = "SELECT * FROM auto_briefs"
    params = []
    if brand_id:
        q += " WHERE brand_id=?"
        params.append(brand_id)
    q += " ORDER BY created_at DESC LIMIT 100"
    rows = conn.execute(q, params).fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.delete("/api/auto-briefs/{brief_id}")
async def delete_brief(brief_id: str):
    conn = get_db()
    conn.execute("DELETE FROM auto_briefs WHERE id=?", (brief_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ===== Snapshots =====

@app.get("/api/snapshots")
async def list_snapshots():
    conn = get_db()
    rows = conn.execute("SELECT id, name, type, public_token, is_live, created_at FROM snapshots ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


@app.post("/api/snapshots")
async def create_snapshot(request: Request):
    data = await request.json()
    sid = str(uuid.uuid4())
    token = secrets.token_urlsafe(12)
    conn = get_db()
    conn.execute("INSERT INTO snapshots (id, name, type, payload, public_token, is_live) VALUES (?,?,?,?,?,?)",
        (sid, data["name"], data.get("type", "studio"), json.dumps(data.get("payload", {})), token, int(data.get("is_live", 0))))
    conn.commit()
    conn.close()
    return {"id": sid, "public_token": token, "url": f"/s/{token}"}


@app.get("/s/{token}")
async def get_public_snapshot(token: str):
    conn = get_db()
    row = conn.execute("SELECT * FROM snapshots WHERE public_token=?", (token,)).fetchone()
    conn.close()
    if not row:
        raise HTTPException(404)
    payload = json.loads(row["payload"])
    return JSONResponse({
        "name": row["name"], "type": row["type"], "payload": payload,
        "is_live": bool(row["is_live"]), "created_at": row["created_at"]
    })


@app.delete("/api/snapshots/{snapshot_id}")
async def delete_snapshot(snapshot_id: str):
    conn = get_db()
    conn.execute("DELETE FROM snapshots WHERE id=?", (snapshot_id,))
    conn.commit()
    conn.close()
    return {"ok": True}


# ===== AI Tasks =====

@app.get("/api/ai-tasks")
async def list_ai_tasks():
    conn = get_db()
    rows = conn.execute("SELECT * FROM ai_tasks ORDER BY is_default DESC, created_at ASC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ===== Export ZIP by tag =====

@app.get("/api/export-zip")
async def export_zip(status_tag: Optional[str] = None, brand_id: Optional[str] = None):
    """Export all images matching filter as ZIP."""
    import zipfile
    import io
    conn = get_db()
    q = "SELECT id, filename, filepath FROM generated_images WHERE 1=1"
    params = []
    if status_tag:
        q += " AND status_tag=?"
        params.append(status_tag)
    rows = conn.execute(q, params).fetchall()
    conn.close()

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for r in rows:
            fp = Path(r["filepath"])
            if fp.exists():
                zf.write(fp, fp.name)
    buf.seek(0)
    from fastapi.responses import StreamingResponse
    fname = f"creaflow_{status_tag or 'all'}_{datetime.now().strftime('%Y%m%d')}.zip"
    return StreamingResponse(buf, media_type='application/zip', headers={
        "Content-Disposition": f'attachment; filename="{fname}"'
    })


# ===========================================================================
# Wave 1B — Generation logic (hooks, persona batch, A/B variants, AI tasks, naming)
# ===========================================================================

# ----- Hook generator -----

HOOK_TYPOLOGIES = {
    "pov": [
        "POV: tu prends ton 4ème café et ton ventre te dit non",
        "POV: tu te réveilles avec l'énergie d'un athlète sans café",
    ],
    "question": [
        "Pourquoi 90% des entrepreneurs arrêtent leur café à 14h ?",
        "Tu pensais que le café était sain ? Lis ça.",
    ],
    "choc": [
        "Stop. Ton café est en train de te détruire.",
        "Personne ne te dit ce qui se passe vraiment quand tu bois ton 4ème café.",
    ],
    "listicle": [
        "5 raisons pour lesquelles ton café te ballonne (et la solution)",
        "3 erreurs à éviter avec ta routine matinale",
    ],
    "prix": [
        "0,87€ par tasse pour remplacer ton Starbucks à 5€",
        "-20% cette semaine seulement sur le café le plus dosé de France",
    ],
}


def label_map(t):
    return {"pov": "🎬 POV", "question": "❓ Question", "choc": "⚡ Choc", "listicle": "📋 Listicle", "prix": "💰 Prix"}.get(t, t)


@app.post("/api/generate-hooks")
async def generate_hooks(request: Request):
    """Génère 10 hooks (2 par typologie) à partir d'un angle + brand_dna."""
    data = await request.json()
    angle = data.get("angle", "").strip()
    brand_dna = data.get("brand_dna", "")

    # Pour MNG: adapte les hooks templates avec l'angle donné
    angle_keywords = {
        "ballonnements": {"problem": "ballonne", "fix": "digestion fluide", "audience": "femmes actives"},
        "crash": {"problem": "crashe à 14h", "fix": "énergie stable 10h", "audience": "entrepreneurs"},
        "focus": {"problem": "perds ton focus", "fix": "10h de focus laser", "audience": "créatifs"},
        "stress": {"problem": "stress qui te ronge", "fix": "calme mental", "audience": "cadres"},
        "default": {"problem": "café qui te détruit", "fix": "alternative qui marche", "audience": "tous"},
    }
    # Detect angle keyword
    al = angle.lower()
    kw_key = "default"
    for k in ["ballonnement", "crash", "focus", "stress"]:
        if k in al:
            kw_key = k if k != "ballonnement" else "ballonnements"
            break
    kw = angle_keywords.get(kw_key, angle_keywords["default"])

    hooks_by_type = {
        "pov": [
            f"POV: tu prends ton café et ton corps te dit '{kw['problem']}'",
            f"POV: 1ère semaine sans café — {kw['fix']}",
        ],
        "question": [
            f"Pourquoi {kw['audience']} arrêtent le café classique ?",
            f"T'as déjà essayé un café qui te donne {kw['fix']} ?",
        ],
        "choc": [
            f"Stop. Ton café te {kw['problem']}. Voici la solution.",
            f"Le café que tu bois te coûte plus cher que tu crois ({kw['problem']}).",
        ],
        "listicle": [
            f"5 raisons pour lesquelles ton café te {kw['problem']}",
            f"3 trucs que les {kw['audience']} font différemment au petit-déj",
        ],
        "prix": [
            f"0,87€ la tasse pour {kw['fix']}",
            f"-20% cette semaine sur le café le + dosé de France",
        ],
    }

    return {
        "angle": angle,
        "hooks": [
            {"type": t, "label": label_map(t), "text": txt}
            for t, txts in hooks_by_type.items()
            for txt in txts
        ],
    }


# ----- Personas batch generation -----

@app.post("/api/generate/personas-batch")
async def generate_personas_batch(request: Request):
    """
    Lance 1 génération par persona (par défaut les 4 personas).
    Body: {brand_id, product_id, angle, persona_ids: [...], aspect_ratio, language, formats}
    Retourne la liste des generation_id créés.
    """
    data = await request.json()
    brand_id = data.get("brand_id")
    product_id = data.get("product_id")
    angle = data.get("angle", "")
    persona_ids = data.get("persona_ids", [])
    aspect = data.get("aspect_ratio", "4:5")
    lang = data.get("language", "fr")
    formats_json = data.get("formats", '["4:5"]')

    if not brand_id:
        raise HTTPException(400, "brand_id required")

    conn = get_db()
    if not persona_ids:
        # Default: all default personas
        rows = conn.execute("SELECT id FROM personas WHERE is_default=1").fetchall()
        persona_ids = [r["id"] for r in rows]

    gen_ids = []
    for pid in persona_ids:
        prow = conn.execute("SELECT * FROM personas WHERE id=?", (pid,)).fetchone()
        if not prow:
            continue
        gid = str(uuid.uuid4())
        # Tailor angle to persona
        persona_context = f"Persona ciblée : {prow['name']} ({prow['age_range']}). Pain points: {', '.join(json.loads(prow['pain_points'] or '[]')[:3])}. Désirs: {', '.join(json.loads(prow['desires'] or '[]')[:3])}."
        custom = f"{angle}\n\nADAPTE LE MESSAGE À CETTE PERSONA: {persona_context}"

        conn.execute("""
            INSERT INTO generations (id, brand_id, product_id, status, num_creations, aspect_ratio, custom_prompt, language, formats, persona_id, generation_kind)
            VALUES (?, ?, ?, 'pending', 1, ?, ?, ?, ?, ?, 'persona_batch')
        """, (gid, brand_id, product_id, aspect, custom, lang, formats_json, pid))
        gen_ids.append(gid)
        # Launch background generation
        Thread(target=run_generation, args=(gid,), daemon=True).start()
    conn.commit()
    conn.close()
    return {"generation_ids": gen_ids, "count": len(gen_ids)}


# ----- A/B variants generation -----

@app.post("/api/generate/ab-variants")
async def generate_ab_variants(request: Request):
    """
    Crée N variantes d'une image existante (subtle changes).
    Body: {parent_image_id, count: 5}
    """
    data = await request.json()
    parent_image_id = data.get("parent_image_id")
    count = int(data.get("count", 5))

    conn = get_db()
    parent = conn.execute("SELECT gi.*, g.brand_id, g.product_id, g.aspect_ratio, g.language, g.formats FROM generated_images gi JOIN generations g ON gi.generation_id=g.id WHERE gi.id=?", (parent_image_id,)).fetchone()
    if not parent:
        conn.close()
        raise HTTPException(404)

    variant_instructions = [
        "Variante 1 — Change la couleur de fond (autre teinte de la palette MNG)",
        "Variante 2 — Repositionne le headline (haut → bas, ou centre)",
        "Variante 3 — Change le CTA (couleur ou wording subtil)",
        "Variante 4 — Variation typo headline (poids, italique, taille)",
        "Variante 5 — Variation cadrage produit (close-up vs lifestyle)",
        "Variante 6 — Ajout d'un trust badge supplémentaire",
        "Variante 7 — Variation arrière-plan (uni vs texturé)",
        "Variante 8 — Repositionnement éléments secondaires (bullets, étoiles)",
        "Variante 9 — Changement d'éclairage (soft vs contrast)",
        "Variante 10 — Variation accent color sur un mot-clé",
    ][:count]

    gen_ids = []
    for instr in variant_instructions:
        gid = str(uuid.uuid4())
        custom = f"VARIANTE A/B (Andromeda-safe): {instr}\n\nGarde la même structure générale et le même message que la créa parent. Change UNIQUEMENT l'élément demandé."
        conn.execute("""
            INSERT INTO generations (id, brand_id, product_id, status, num_creations, aspect_ratio, custom_prompt, language, formats, generation_kind)
            VALUES (?, ?, ?, 'pending', 1, ?, ?, ?, ?, 'ab_variants')
        """, (gid, parent["brand_id"], parent["product_id"], parent["aspect_ratio"], custom, parent["language"], parent["formats"]))
        # Save parent_id on the future image (we'll pass via the threading)
        Thread(target=_run_ab_variant, args=(gid, parent_image_id), daemon=True).start()
        gen_ids.append(gid)
    conn.commit()
    conn.close()
    return {"generation_ids": gen_ids, "count": len(gen_ids), "parent_image_id": parent_image_id}


def _run_ab_variant(gen_id: str, parent_image_id: str):
    """Wrapper that runs run_generation then sets variant_parent_id on the new image."""
    run_generation(gen_id)
    try:
        conn = get_db()
        # Find images for this generation
        imgs = conn.execute("SELECT id FROM generated_images WHERE generation_id=?", (gen_id,)).fetchall()
        for img in imgs:
            conn.execute("UPDATE generated_images SET variant_parent_id=? WHERE id=?", (parent_image_id, img["id"]))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[ab_variant] failed to set parent: {e}")


# ----- AI Tasks runner -----

@app.post("/api/ai-tasks/{task_id}/run")
async def run_ai_task(task_id: str):
    """Execute a predefined AI task and return results."""
    conn = get_db()
    task = conn.execute("SELECT * FROM ai_tasks WHERE id=?", (task_id,)).fetchone()
    if not task:
        conn.close()
        raise HTTPException(404)

    kind = task["query_kind"]
    result = {}

    if kind == "best_angle":
        # Group winners by detected angle (from prompt_used)
        rows = conn.execute("""
            SELECT gi.id, gi.prompt_used, gi.status_tag
            FROM generated_images gi
            WHERE gi.status_tag IN ('winner', 'to_test')
        """).fetchall()
        from collections import Counter
        angles = Counter()
        for r in rows:
            p = (r["prompt_used"] or "").lower()
            for a in ["ballonnements", "crash", "focus", "stress", "digestion", "énergie"]:
                if a in p:
                    angles[a] += 1
                    break
        result = {
            "ranked_angles": [{"angle": a, "count": c} for a, c in angles.most_common()],
            "winner_angle": angles.most_common(1)[0][0] if angles else None,
            "total_winners": sum(angles.values()),
        }

    elif kind == "best_persona":
        rows = conn.execute("""
            SELECT g.persona_id, p.name, COUNT(*) as cnt
            FROM generations g
            JOIN generated_images gi ON gi.generation_id=g.id
            LEFT JOIN personas p ON p.id=g.persona_id
            WHERE gi.status_tag='winner' AND g.persona_id IS NOT NULL
            GROUP BY g.persona_id
            ORDER BY cnt DESC
        """).fetchall()
        result = {"ranked_personas": [{"persona_id": r["persona_id"], "name": r["name"], "winner_count": r["cnt"]} for r in rows]}

    elif kind == "ugc_vs_static":
        # Detect UGC vs static from prompts
        rows = conn.execute("""
            SELECT gi.prompt_used, gi.status_tag
            FROM generated_images gi WHERE gi.status_tag IN ('winner', 'killed')
        """).fetchall()
        ugc_winners = ugc_total = static_winners = static_total = 0
        for r in rows:
            p = (r["prompt_used"] or "").lower()
            is_ugc = any(k in p for k in ["face cam", "ugc", "selfie", "talking head"])
            if is_ugc:
                ugc_total += 1
                if r["status_tag"] == "winner": ugc_winners += 1
            else:
                static_total += 1
                if r["status_tag"] == "winner": static_winners += 1
        result = {
            "ugc": {"winners": ugc_winners, "total": ugc_total, "win_rate": round(ugc_winners/ugc_total*100, 1) if ugc_total else 0},
            "static": {"winners": static_winners, "total": static_total, "win_rate": round(static_winners/static_total*100, 1) if static_total else 0},
            "winner_format": "static" if (static_total and (static_winners/static_total) > (ugc_winners/max(ugc_total,1))) else "ugc",
        }

    elif kind == "top_hooks":
        # Top hook patterns from winners + expert insights
        insights = conn.execute("SELECT why_it_works FROM expert_insights WHERE pattern_type='hook_pattern' ORDER BY created_at DESC LIMIT 10").fetchall()
        result = {
            "top_hooks": [r["why_it_works"][:200] for r in insights],
            "count": len(insights),
        }

    # Update task last_run
    conn.execute("UPDATE ai_tasks SET last_run=datetime('now'), last_result=? WHERE id=?",
        (json.dumps(result), task_id))
    conn.commit()
    conn.close()
    return {"task": dict(task), "result": result}


# ----- Naming convention generator -----

def generate_naming(brand_name: str = "MNG", product_name: str = "", hook_type: str = "", angle: str = "", persona: str = "", aspect: str = "", version: int = 1) -> str:
    """Generate Meta-friendly ad name: MNG_Brainstoorm_PainPoint_Ballonnements_FRwoman28-40_4-5_v1_2026-04-28"""
    parts = [
        brand_name,
        (product_name or "Brainstoorm").replace(" ", ""),
        hook_type or "Pain",
        (angle or "")[:30].replace(" ", ""),
        (persona or "")[:25].replace(" ", "-"),
        aspect.replace(":", "-") if aspect else "4-5",
        f"v{version}",
        datetime.now().strftime("%Y%m%d"),
    ]
    return "_".join(p for p in parts if p).strip("_")


@app.post("/api/generate-naming")
async def api_generate_naming(request: Request):
    data = await request.json()
    name = generate_naming(
        brand_name=data.get("brand_name", "MNG"),
        product_name=data.get("product_name", ""),
        hook_type=data.get("hook_type", ""),
        angle=data.get("angle", ""),
        persona=data.get("persona", ""),
        aspect=data.get("aspect", "4:5"),
        version=int(data.get("version", 1)),
    )
    return {"naming": name}


# =====================================================================
# Wave 1B — AI Tagging engine + Brief auto (TrendTrack / Meta Ads Library)
# =====================================================================

TAG_CATEGORIES = {
    "hook_type": ["pov", "question", "choc", "listicle", "prix", "social_proof", "comparison"],
    "messaging_angle": ["ballonnements", "crash_energie", "focus", "stress", "alternative_cafe", "naturel", "sommeil", "performance"],
    "visual_format": ["split", "listicle", "before_after", "mockup", "lifestyle", "studio_product", "ugc_screenshot", "color_block"],
    "asset_type": ["static", "ugc", "studio", "motion_design", "talking_head"],
    "offer": ["discount", "bundle", "free_trial", "free_shipping", "subscription", "no_offer"],
    "seasonality": ["bfcm", "rentree", "ramadan", "summer", "winter", "newyear", "evergreen"],
    "audience": ["entrepreneur", "active_woman", "student", "athlete", "general"],
    "funnel_stage": ["tofu", "mofu", "bofu"],
}


def auto_tag_creative(prompt_text: str = "", filename: str = "", angle: str = "") -> dict:
    """Tagging rule-based sur 8 categories a partir du prompt/naming/angle."""
    text = (prompt_text + " " + filename + " " + angle).lower()
    tags = {cat: [] for cat in TAG_CATEGORIES}

    # Hook type detection
    if "pov" in text or "point of view" in text: tags["hook_type"].append("pov")
    if "?" in text or "pourquoi" in text or "comment " in text: tags["hook_type"].append("question")
    if "stop" in text or "arrête" in text or "fini " in text: tags["hook_type"].append("choc")
    if any(k in text for k in ["5 raisons", "3 erreurs", "top ", "listicle"]): tags["hook_type"].append("listicle")
    if "€" in text or "%" in text or "prix" in text or "promo" in text: tags["hook_type"].append("prix")
    if "20 000" in text or "trustpilot" in text or "avis" in text or "★" in text: tags["hook_type"].append("social_proof")
    if "vs " in text or "comparé" in text or " versus " in text: tags["hook_type"].append("comparison")

    # Messaging angle
    angle_map = {
        "ballonnement": "ballonnements", "ventre": "ballonnements", "digestion": "ballonnements",
        "crash": "crash_energie", "energie": "crash_energie", "fatigue": "crash_energie",
        "focus": "focus", "concentration": "focus", "productivité": "focus",
        "stress": "stress", "anxiété": "stress", "cortisol": "stress",
        "alternative au café": "alternative_cafe", "remplacer le café": "alternative_cafe",
        "naturel": "naturel", "100%": "naturel", "bio": "naturel",
        "sommeil": "sommeil", "dormir": "sommeil",
        "performance": "performance", "sport": "performance",
    }
    for kw, t in angle_map.items():
        if kw in text and t not in tags["messaging_angle"]:
            tags["messaging_angle"].append(t)

    # Visual format
    if "split" in text or "before/after" in text or "avant/après" in text: tags["visual_format"].append("split")
    if "listicle" in text or "bulles" in text or "bullet" in text: tags["visual_format"].append("listicle")
    if "before" in text or "avant" in text and "après" in text: tags["visual_format"].append("before_after")
    if "mockup" in text: tags["visual_format"].append("mockup")
    if "lifestyle" in text or "ambiance" in text: tags["visual_format"].append("lifestyle")
    if "studio" in text or "fond uniforme" in text: tags["visual_format"].append("studio_product")
    if "selfie" in text or "facecam" in text or "ugc" in text: tags["visual_format"].append("ugc_screenshot")
    if "fond couleur" in text or "color block" in text: tags["visual_format"].append("color_block")

    # Asset type
    if "ugc" in text or "facecam" in text or "selfie" in text: tags["asset_type"].append("ugc")
    elif "studio" in text: tags["asset_type"].append("studio")
    elif "talking head" in text or "interview" in text: tags["asset_type"].append("talking_head")
    elif "motion" in text or "animation" in text: tags["asset_type"].append("motion_design")
    else: tags["asset_type"].append("static")

    # Offer
    if "-" in text and "%" in text: tags["offer"].append("discount")
    if "bundle" in text or "pack" in text: tags["offer"].append("bundle")
    if "essai" in text or "trial" in text or "satisfait ou remboursé" in text: tags["offer"].append("free_trial")
    if "livraison offerte" in text or "free shipping" in text: tags["offer"].append("free_shipping")
    if "abonnement" in text or "subscription" in text: tags["offer"].append("subscription")
    if not any(tags["offer"]): tags["offer"].append("no_offer")

    # Seasonality (mostly evergreen)
    if "bfcm" in text or "black friday" in text or "noël" in text: tags["seasonality"].append("bfcm")
    elif "rentrée" in text or "back to school" in text: tags["seasonality"].append("rentree")
    elif "ramadan" in text: tags["seasonality"].append("ramadan")
    elif "été" in text or "summer" in text: tags["seasonality"].append("summer")
    elif "hiver" in text or "winter" in text: tags["seasonality"].append("winter")
    elif "nouvel an" in text or "new year" in text: tags["seasonality"].append("newyear")
    else: tags["seasonality"].append("evergreen")

    # Audience
    if "entrepreneur" in text or "cadre" in text or "dirigeant" in text: tags["audience"].append("entrepreneur")
    if "femme active" in text or "maman" in text or "ventre plat" in text: tags["audience"].append("active_woman")
    if "étudiant" in text or "examens" in text or "fac" in text: tags["audience"].append("student")
    if "sport" in text or "athlète" in text or "training" in text: tags["audience"].append("athlete")
    if not any(tags["audience"]): tags["audience"].append("general")

    # Funnel stage (heuristic)
    if any(k in text for k in ["pourquoi", "savais-tu", "découvre", "pain point"]): tags["funnel_stage"].append("tofu")
    elif any(k in text for k in ["vs", "compare", "alternative", "trust"]): tags["funnel_stage"].append("mofu")
    elif any(k in text for k in ["essaie", "achète", "commande", "promo", "%"]): tags["funnel_stage"].append("bofu")
    else: tags["funnel_stage"].append("mofu")

    return tags


@app.post("/api/auto-tag/{image_id}")
async def auto_tag_image(image_id: str):
    """Auto-tag d'une image a partir de son prompt."""
    conn = get_db()
    img = conn.execute("SELECT * FROM generated_images WHERE id=?", (image_id,)).fetchone()
    if not img:
        conn.close()
        raise HTTPException(404)
    tags = auto_tag_creative(prompt_text=img["prompt_used"] or "", filename=img["filename"] or "")
    conn.execute("UPDATE generated_images SET tags=? WHERE id=?", (json.dumps(tags), image_id))
    conn.commit()
    conn.close()
    return {"image_id": image_id, "tags": tags}


@app.post("/api/auto-tag-all")
async def auto_tag_all():
    """Auto-tag toutes les images sans tags."""
    conn = get_db()
    rows = conn.execute("SELECT id, prompt_used, filename FROM generated_images WHERE tags='{}' OR tags IS NULL").fetchall()
    count = 0
    for r in rows:
        tags = auto_tag_creative(prompt_text=r["prompt_used"] or "", filename=r["filename"] or "")
        conn.execute("UPDATE generated_images SET tags=? WHERE id=?", (json.dumps(tags), r["id"]))
        count += 1
    conn.commit()
    conn.close()
    return {"tagged_count": count}


@app.get("/api/tag-categories")
async def get_tag_categories():
    return TAG_CATEGORIES


# ---------- Brief auto: TrendTrack ----------

@app.post("/api/auto-brief/trendtrack")
async def auto_brief_trendtrack(request: Request):
    """
    Extrait les infos creas d'une ad TrendTrack via son ad_id.
    Body: {ad_id: "facebook_xxx", brand_id: "..."}
    Retourne: copy, headline, visuel, tags detectes.
    """
    data = await request.json()
    ad_id = data.get("ad_id", "").strip()
    brand_id = data.get("brand_id", "")
    if not ad_id:
        raise HTTPException(400, "ad_id required")
    if not TRENDTRACK_API_KEY:
        raise HTTPException(503, "TrendTrack not configured")

    headers = {"Authorization": f"Bearer {TRENDTRACK_API_KEY}"}
    try:
        r = requests.get(f"{TRENDTRACK_BASE}/v1/ads/{ad_id}", headers=headers, timeout=20)
        r.raise_for_status()
        d = r.json().get("data", r.json())
    except Exception as e:
        raise HTTPException(500, f"TrendTrack fetch failed: {e}")

    ad = d.get("ad") or d
    content = ad.get("content") or {}
    media = ad.get("media") or {}
    advertiser = ad.get("advertiser") or {}

    extracted_copy = content.get("body", "")
    extracted_headline = content.get("title") or content.get("ctaDescription") or ""
    visual_url = media.get("thumbnailUrl", "")

    # Auto-tag
    detected_tags = auto_tag_creative(prompt_text=extracted_copy + " " + extracted_headline)

    bid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("""
        INSERT INTO auto_briefs (id, brand_id, source_url, source_type, competitor_name, extracted_copy, extracted_headline, visual_path, detected_tags)
        VALUES (?, ?, ?, 'trendtrack', ?, ?, ?, ?, ?)
    """, (bid, brand_id, f"trendtrack://{ad_id}", advertiser.get("name", ""), extracted_copy, extracted_headline, visual_url, json.dumps(detected_tags)))
    conn.commit()
    conn.close()
    return {
        "id": bid,
        "competitor_name": advertiser.get("name", ""),
        "extracted_headline": extracted_headline,
        "extracted_copy": extracted_copy,
        "visual_url": visual_url,
        "detected_tags": detected_tags,
    }


# ---------- Brief auto: Meta Ads Library (best-effort) ----------

import re as _re


@app.post("/api/auto-brief/meta-library")
async def auto_brief_meta_library(request: Request):
    """
    Extrait les infos creas d'une URL Meta Ads Library (best-effort, Meta bloque souvent).
    Body: {url, brand_id, screenshot_text (optional), competitor_name (optional)}
    """
    data = await request.json()
    url = data.get("url", "").strip()
    brand_id = data.get("brand_id", "")
    screenshot_text = data.get("screenshot_text", "")
    competitor_name = data.get("competitor_name", "")

    if not url:
        raise HTTPException(400, "url required")

    # Try to extract brand from URL
    brand_match = _re.search(r"q=([^&]+)", url)
    if brand_match and not competitor_name:
        competitor_name = brand_match.group(1).replace("%22", "").replace('"', '').strip()

    # Try basic fetch — usually 403 but worth trying
    extracted_copy = screenshot_text or ""
    extracted_headline = ""
    visual_url = ""
    try:
        r = requests.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if r.status_code == 200:
            html = r.text
            # Try to find body text from JSON-LD or og:description
            og_match = _re.search(r'<meta[^>]+property=["\']og:description["\'][^>]+content=["\']([^"\']+)', html)
            if og_match:
                extracted_copy = extracted_copy or og_match.group(1)
            img_match = _re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', html)
            if img_match:
                visual_url = img_match.group(1)
    except Exception:
        pass

    # If user provided screenshot text, take first short line as headline
    if screenshot_text and not extracted_headline:
        first_line = screenshot_text.strip().split("\n")[0]
        extracted_headline = first_line[:150]

    detected_tags = auto_tag_creative(prompt_text=extracted_copy + " " + extracted_headline)

    bid = str(uuid.uuid4())
    conn = get_db()
    conn.execute("""
        INSERT INTO auto_briefs (id, brand_id, source_url, source_type, competitor_name, extracted_copy, extracted_headline, visual_path, detected_tags)
        VALUES (?, ?, ?, 'meta_ads_library', ?, ?, ?, ?, ?)
    """, (bid, brand_id, url, competitor_name, extracted_copy, extracted_headline, visual_url, json.dumps(detected_tags)))
    conn.commit()
    conn.close()
    return {
        "id": bid,
        "competitor_name": competitor_name,
        "extracted_headline": extracted_headline,
        "extracted_copy": extracted_copy,
        "visual_url": visual_url,
        "detected_tags": detected_tags,
        "scraping_note": "Meta bloque souvent — colle un screenshot pour les infos manquantes" if not extracted_copy else None,
    }


# =============================================================================
# Wave 1B — Performance feedback loop + Brand Intel hub
# =============================================================================

# Fallback stub for auto_tag_creative (kept for safety; real impl is at line ~2785)
def auto_tag_creative_fallback(prompt_text="", filename="", angle=""):
    return {}
try:
    auto_tag_creative
except NameError:
    auto_tag_creative = auto_tag_creative_fallback


@app.post("/api/feedback-loop/sync")
async def sync_feedback_loop():
    """
    Match Ad Doctor verdicts to generated_images by naming.
    Auto-update status_tag based on MNG thresholds:
      - CPA < 27 + purchases >= 5 → winner
      - CPA > 45 OR (spend > 75 AND 0 purchase) → killed
      - else: keep current status
    Returns count of updated images + winning angles/personas.
    """
    data_path = DATA_DIR / "mng_top_7d.json"
    if not data_path.exists():
        return {"ok": False, "error": "No Ad Doctor data. Run /api/ad-doctor/refresh first."}

    try:
        ad_data = json.loads(data_path.read_text())
    except Exception as e:
        return {"ok": False, "error": str(e)}

    conn = get_db()
    rows = conn.execute("SELECT id, naming, prompt_used FROM generated_images").fetchall()
    naming_to_id = {(r["naming"] or "").lower(): r["id"] for r in rows if r["naming"]}

    updated = 0
    auto_winners = []
    auto_killed = []

    for ad in ad_data:
        ad_name = (ad.get("ad_name") or "").lower()
        if not ad_name:
            continue
        # Match by partial inclusion
        matched_id = None
        for naming, img_id in naming_to_id.items():
            if naming and (naming in ad_name or ad_name in naming):
                matched_id = img_id
                break
        if not matched_id:
            continue

        cpa = ad.get("cpa") or 0
        purchases = ad.get("purchases", 0)
        spend = ad.get("spend", 0)

        new_status = None
        if cpa and cpa != float('inf') and cpa < 27 and purchases >= 5:
            new_status = "winner"
            auto_winners.append({"image_id": matched_id, "ad_name": ad.get("ad_name"), "cpa": cpa})
        elif (cpa and cpa > 45 and cpa != float('inf')) or (spend > 75 and purchases == 0):
            new_status = "killed"
            auto_killed.append({"image_id": matched_id, "ad_name": ad.get("ad_name"), "cpa": cpa})

        if new_status:
            conn.execute("UPDATE generated_images SET status_tag=? WHERE id=?", (new_status, matched_id))
            updated += 1

    conn.commit()
    conn.close()
    return {
        "ok": True,
        "updated_count": updated,
        "auto_winners": auto_winners,
        "auto_killed": auto_killed,
    }


@app.get("/api/feedback-loop/learnings")
async def get_learnings(brand_id: Optional[str] = None):
    """
    Aggregate learnings from current winners (status_tag='winner') + expert insights.
    Returns angle/persona/hook biases for prompt builder.
    """
    conn = get_db()
    # Winners
    winners = conn.execute("""
        SELECT gi.id, gi.tags, gi.prompt_used, gi.persona_id, p.name as persona_name
        FROM generated_images gi
        LEFT JOIN personas p ON p.id=gi.persona_id
        WHERE gi.status_tag='winner'
    """).fetchall()

    from collections import Counter
    angle_counts = Counter()
    hook_counts = Counter()
    persona_counts = Counter()
    visual_counts = Counter()

    for w in winners:
        try:
            tags = json.loads(w["tags"] or "{}")
            for a in tags.get("messaging_angle", []):
                angle_counts[a] += 1
            for h in tags.get("hook_type", []):
                hook_counts[h] += 1
            for v in tags.get("visual_format", []):
                visual_counts[v] += 1
        except Exception:
            pass
        if w["persona_name"]:
            persona_counts[w["persona_name"]] += 1

    # Expert insights
    insights = conn.execute("SELECT pattern_type, why_it_works, source FROM expert_insights ORDER BY created_at DESC LIMIT 20").fetchall()

    conn.close()

    return {
        "winner_count": len(winners),
        "top_angles": [{"angle": a, "count": c} for a, c in angle_counts.most_common(5)],
        "top_hooks": [{"hook": h, "count": c} for h, c in hook_counts.most_common(5)],
        "top_personas": [{"persona": p, "count": c} for p, c in persona_counts.most_common(5)],
        "top_visuals": [{"visual": v, "count": c} for v, c in visual_counts.most_common(5)],
        "expert_insights": [{
            "pattern_type": i["pattern_type"],
            "source": i["source"],
            "why_it_works": (i["why_it_works"] or "")[:300],
        } for i in insights],
    }


def get_winner_bias_for_prompt(brand_id: str = None) -> str:
    """Build a context string with winner learnings to inject in build_prompt()."""
    try:
        conn = get_db()
        winners = conn.execute("SELECT tags FROM generated_images WHERE status_tag='winner'").fetchall()
        from collections import Counter
        angles = Counter()
        hooks = Counter()
        for w in winners:
            try:
                tags = json.loads(w["tags"] or "{}")
                for a in tags.get("messaging_angle", []): angles[a] += 1
                for h in tags.get("hook_type", []): hooks[h] += 1
            except Exception:
                pass
        # Recent expert insights
        insights = conn.execute("SELECT why_it_works FROM expert_insights ORDER BY created_at DESC LIMIT 5").fetchall()
        conn.close()

        if not winners and not insights:
            return ""

        bias_parts = []
        if angles:
            top_a = ", ".join(a for a, _ in angles.most_common(3))
            bias_parts.append(f"Angles winners MNG (les + souvent gagnants): {top_a}")
        if hooks:
            top_h = ", ".join(h for h, _ in hooks.most_common(3))
            bias_parts.append(f"Hook types winners: {top_h}")
        if insights:
            bias_parts.append("Insights expert: " + " | ".join((i["why_it_works"] or "")[:120] for i in insights[:3]))

        return "\n\nLEARNINGS DES WINNERS MNG (à privilégier):\n" + "\n".join(bias_parts)
    except Exception:
        return ""


# -----------------------------------------------------------------------------
# Brand Intel Hub
# -----------------------------------------------------------------------------

@app.get("/api/brand-intel/{competitor_name}")
async def get_brand_intel(competitor_name: str):
    """
    Aggregated patterns for a competitor brand from auto_briefs + recent TrendTrack data.
    Returns: dominant hooks, angles, offers, formats, copy patterns.
    """
    from urllib.parse import unquote
    competitor_name = unquote(competitor_name)
    conn = get_db()

    briefs = conn.execute("""
        SELECT detected_tags, extracted_copy, extracted_headline
        FROM auto_briefs
        WHERE competitor_name LIKE ?
        ORDER BY created_at DESC LIMIT 50
    """, (f"%{competitor_name}%",)).fetchall()

    from collections import Counter
    hook_c = Counter()
    angle_c = Counter()
    offer_c = Counter()
    visual_c = Counter()
    headlines = []

    for b in briefs:
        try:
            tags = json.loads(b["detected_tags"] or "{}")
            for h in tags.get("hook_type", []): hook_c[h] += 1
            for a in tags.get("messaging_angle", []): angle_c[a] += 1
            for o in tags.get("offer", []): offer_c[o] += 1
            for v in tags.get("visual_format", []): visual_c[v] += 1
        except Exception:
            pass
        if b["extracted_headline"]:
            headlines.append(b["extracted_headline"])

    conn.close()

    total = len(briefs)
    return {
        "competitor": competitor_name,
        "total_creatives_analyzed": total,
        "dominant_hooks": [{"hook": h, "count": c, "pct": round(c/total*100,1) if total else 0} for h, c in hook_c.most_common(5)],
        "dominant_angles": [{"angle": a, "count": c, "pct": round(c/total*100,1) if total else 0} for a, c in angle_c.most_common(5)],
        "dominant_offers": [{"offer": o, "count": c} for o, c in offer_c.most_common(3)],
        "dominant_visuals": [{"visual": v, "count": c} for v, c in visual_c.most_common(5)],
        "sample_headlines": headlines[:10],
    }


@app.get("/api/brand-intel")
async def list_brand_intels():
    """List all competitors that have intel collected."""
    conn = get_db()
    rows = conn.execute("""
        SELECT competitor_name, COUNT(*) as creative_count
        FROM auto_briefs
        WHERE competitor_name != ''
        GROUP BY competitor_name
        ORDER BY creative_count DESC
    """).fetchall()
    conn.close()
    return [{"competitor": r["competitor_name"], "creative_count": r["creative_count"]} for r in rows]


@app.post("/api/brand-intel/build")
async def build_brand_intel(request: Request):
    """
    Pull all top-ads from a TrendTrack brandtracker and create auto_briefs for each.
    Body: {brandtracker_id: "...", competitor_name: "..."}
    """
    data = await request.json()
    btr_id = data.get("brandtracker_id", "")
    name = data.get("competitor_name", "")
    if not btr_id or not name:
        raise HTTPException(400, "brandtracker_id + competitor_name required")
    if not TRENDTRACK_API_KEY:
        raise HTTPException(503, "TrendTrack not configured")

    headers = {"Authorization": f"Bearer {TRENDTRACK_API_KEY}"}
    try:
        r = requests.get(
            f"{TRENDTRACK_BASE}/v1/brandtrackers/{btr_id}/top-ads",
            headers=headers,
            params={"sortBy": "currentRank", "limit": 30, "mediaType": "image"},
            timeout=30,
        )
        r.raise_for_status()
        items = r.json().get("data", []) or []
    except Exception as e:
        raise HTTPException(500, f"TrendTrack fetch failed: {e}")

    conn = get_db()
    count = 0
    for item in items:
        ad = item.get("ad") or {}
        content = ad.get("content") or {}
        media = ad.get("media") or {}
        ad_id = ad.get("id", "")
        # Skip if already exists
        existing = conn.execute("SELECT id FROM auto_briefs WHERE source_url=?", (f"trendtrack://{ad_id}",)).fetchone()
        if existing:
            continue
        copy = content.get("body", "") or ""
        headline = content.get("title") or content.get("ctaDescription") or ""
        tags = auto_tag_creative(prompt_text=copy + " " + headline)
        bid = str(uuid.uuid4())
        conn.execute("""
            INSERT INTO auto_briefs (id, source_url, source_type, competitor_name, extracted_copy, extracted_headline, visual_path, detected_tags)
            VALUES (?, ?, 'trendtrack', ?, ?, ?, ?, ?)
        """, (bid, f"trendtrack://{ad_id}", name, copy, headline, media.get("thumbnailUrl", ""), json.dumps(tags)))
        count += 1
    conn.commit()
    conn.close()
    return {"ok": True, "competitor": name, "creatives_added": count, "total_in_response": len(items)}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8899, reload=True)
