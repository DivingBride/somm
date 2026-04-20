import base64
import os
import re
import sys
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

# Load .env from the same directory as this file.
# override=False: real environment variables (Render, systemd, or the
# prod-dryrun script) always win; .env only fills in the gaps. This is
# the standard 12-factor pattern and prevents a stale local .env from
# clobbering prod-shaped config during dry-runs.
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=False)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SOMM_DATA_PATH = os.environ.get("SOMM_DATA_PATH", "../Wine Pairings/somm")

TABLE_FILES = [
    "Table_1___molecules_to_aromas_t.md",
    "Table_2___molecules_to_aromas_t.md",
    "Table_3___Master_Pairing.md",
    "Table_4___Cooking_Transformatio.md",
    "Table_5___wines_to_aromas.md",
    "Table_6___Physiological_Effects.md",
]

DEPLOYMENT_NOTE = """
---
DEPLOYMENT NOTE: This instance runs as a web application. The screenshot files
referenced in this skill are pre-loaded and passed as image inputs alongside
each user message when relevant. You do not need to read files from any path —
the relevant screenshots are already included in this conversation if applicable.
The reference tables above replace the file-based tables. Answer from this
assembled context only.
---
"""

SCREENSHOT_ROUTING = {
    r"anise|fennel|pastis|mint|sauvignon blanc|licorice": [
        "p041", "p042", "p044", "p045", "p046"
    ],
    r"sotolon|sherry|fino|manzanilla|oloroso|walnut|curry|vin jaune": [
        "p060", "p071", "p073"
    ],
    r"oak|barrel|vanilla|coconut|maple|whisky lactone": [
        "p078", "p079", "p085"
    ],
    r"beef|lamb|red meat|steak|roast": [
        "p091", "p092"
    ],
    r"gewurztraminer|lychee|ginger|scheurebe": [
        "p101"
    ],
    r"pineapple|strawberr": [
        "p108", "p117"
    ],
    r"rosemary": [
        "p124"
    ],
    r"saffron|bouillabaisse|paella": [
        "p134", "p138"
    ],
    r"ginger": [
        "p148", "p152", "p153"
    ],
    r"cheese|brie|camembert|gruyere|roquefort|stilton": [
        "p162", "p167"
    ],
    r"cinnamon|cassia|five.spice": [
        "p170", "p172", "p178"
    ],
    r"cold.tast|menthol|cucumber|wasabi|green pepper": [
        "p184", "p190"
    ],
    r"rotundone|hotrienol|syrah|black pepper": [
        "p197"
    ],
    r"tasting menu|muscat|pork|coconut": [
        "p200", "p201", "p202", "p205"
    ],
}

MAX_SCREENSHOTS = 6

# ---------------------------------------------------------------------------
# Startup: validate and assemble system prompt
# ---------------------------------------------------------------------------

somm_path = Path(SOMM_DATA_PATH).resolve()
screenshots_dir = somm_path / "screenshots"

print(f"[somm] SOMM_DATA_PATH resolved to: {somm_path}")


def _check_files() -> list[str]:
    missing = []
    required = (
        [somm_path / "SKILL.md"]
        + [somm_path / "references" / f for f in TABLE_FILES]
        + [somm_path / "references" / "book_citations.md"]
        + [somm_path / "screenshots" / "INDEX.md"]
    )
    for f in required:
        if not f.exists():
            missing.append(str(f))
    return missing


missing_files = _check_files()
if missing_files:
    print("[somm] ERROR: Missing required files:", file=sys.stderr)
    for f in missing_files:
        print(f"  - {f}", file=sys.stderr)
    sys.exit(1)


def _assemble_system_prompt() -> str:
    parts = []
    parts.append((somm_path / "SKILL.md").read_text(encoding="utf-8"))
    parts.append(DEPLOYMENT_NOTE)
    parts.append("\n\n---\n\n## REFERENCE TABLES\n\n")
    refs_dir = somm_path / "references"
    for fname in TABLE_FILES:
        parts.append(f"### {fname}\n\n")
        parts.append((refs_dir / fname).read_text(encoding="utf-8"))
        parts.append("\n\n")
    parts.append("\n\n---\n\n## BOOK CITATIONS\n\n")
    parts.append((refs_dir / "book_citations.md").read_text(encoding="utf-8"))
    parts.append("\n\n---\n\n## SCREENSHOT INDEX\n\n")
    parts.append((screenshots_dir / "INDEX.md").read_text(encoding="utf-8"))
    parts.append(
        "\n\nThe screenshots themselves are PNG image files located alongside this index. "
        "When a user asks a question related to a chapter that has ✅ screenshots, load and "
        "include the relevant screenshot images in the API call as vision inputs."
    )
    return "".join(parts)


SYSTEM_PROMPT = _assemble_system_prompt()
print(f"[somm] System prompt assembled: {len(SYSTEM_PROMPT):,} chars (~{len(SYSTEM_PROMPT)//4:,} tokens)")

# ---------------------------------------------------------------------------
# Screenshot helpers
# ---------------------------------------------------------------------------


def _load_screenshot(prefix: str):
    for fname in os.listdir(screenshots_dir):
        if fname.startswith(prefix) and fname.endswith(".png"):
            path = screenshots_dir / fname
            with open(path, "rb") as f:
                raw = f.read()
            # Detect actual format from magic bytes (some .png files are actually JPEG)
            media_type = "image/jpeg" if raw[:2] == b"\xff\xd8" else "image/png"
            data = base64.standard_b64encode(raw).decode("utf-8")
            return {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": media_type,
                    "data": data,
                },
            }
    return None


def _get_relevant_screenshots(question: str) -> list[dict]:
    q_lower = question.lower()
    prefixes_seen: set[str] = set()
    images = []

    for pattern, prefixes in SCREENSHOT_ROUTING.items():
        if re.search(pattern, q_lower):
            for prefix in prefixes:
                if prefix not in prefixes_seen:
                    prefixes_seen.add(prefix)
                    if len(images) < MAX_SCREENSHOTS:
                        img = _load_screenshot(prefix)
                        if img:
                            images.append(img)

    return images


# ---------------------------------------------------------------------------
# Anthropic client (shared across routers)
# ---------------------------------------------------------------------------

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="The Sommelier")
app.mount("/static", StaticFiles(directory="static"), name="static")

# Auth + admin + chats + preferences + chartier + saved routers. Phase 2
# retired the v1 APP_PASSWORD /ask endpoint; Phase 3 adds preferences
# injection (§7); Phase 4 adds the Chartier library and saved pairings
# (§8, §9).
import admin  # noqa: E402
import auth  # noqa: E402
import bootstrap  # noqa: E402
import chartier  # noqa: E402
import chartier_sync  # noqa: E402
import chats  # noqa: E402
import observability  # noqa: E402
import preferences  # noqa: E402
import saved  # noqa: E402

app.include_router(auth.router)
app.include_router(admin.router)
app.include_router(chats.router)
app.include_router(preferences.router)
app.include_router(chartier.router)
app.include_router(saved.router)

# Phase 5 (§12, §13.3): access log, /healthz, 5xx→admin-email handler,
# and the rate-limiter singleton consumed by auth.request_magic_link.
# install() must run AFTER routers are mounted so the middleware wraps
# every route including those registered below.
observability.install(app)


@app.on_event("startup")
async def _on_startup() -> None:
    await bootstrap.seed_admin_if_needed()
    await chartier_sync.sync_on_startup()


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/login")
async def login_page():
    return FileResponse("static/login.html")


@app.get("/admin")
async def admin_page():
    # The page itself fetches /api/me and redirects non-admins to /login.
    # Gating here at the HTTP layer would require a user-facing 401/403 UI
    # that we don't need for a static SPA shell.
    return FileResponse("static/admin.html")


@app.get("/preferences")
async def preferences_page():
    return FileResponse("static/preferences.html")


@app.get("/library")
async def library_page():
    return FileResponse("static/library.html")


@app.get("/saved")
async def saved_page():
    return FileResponse("static/saved.html")


@app.get("/saved/new")
async def saved_new_page():
    return FileResponse("static/saved_new.html")


@app.get("/health")
async def health():
    # Kept as a stable, cheap liveness probe for Render's default health
    # check path. The richer /healthz snapshot lives in observability.py.
    return {"status": "ok"}
