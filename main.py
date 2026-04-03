import base64
import os
import re
import sys
import time
import uuid
from collections import defaultdict
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# Load .env from the same directory as this file, overriding any existing env vars
_env_path = Path(__file__).parent / ".env"
load_dotenv(dotenv_path=_env_path, override=True)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
APP_PASSWORD = os.environ.get("APP_PASSWORD", "")
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
MAX_HISTORY_MESSAGES = 12

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
# Conversation history
# ---------------------------------------------------------------------------

# { session_id: [ {"role": "user"|"assistant", "content": ...}, ... ] }
session_history: dict[str, list] = defaultdict(list)


def _add_to_history(session_id: str, role: str, content):
    history = session_history[session_id]
    history.append({"role": role, "content": content})
    # Drop oldest pairs when over limit (keep last MAX_HISTORY_MESSAGES)
    while len(history) > MAX_HISTORY_MESSAGES:
        history.pop(0)


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(title="The Sommelier")
app.mount("/static", StaticFiles(directory="static"), name="static")

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


class AskRequest(BaseModel):
    question: str


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.post("/ask")
async def ask(
    body: AskRequest,
    authorization: str = Header(default=""),
    x_session_id: str = Header(default=""),
):
    # Auth
    expected = f"Bearer {APP_PASSWORD}"
    if not APP_PASSWORD or authorization != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    question = body.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="Question is required")

    session_id = x_session_id or str(uuid.uuid4())

    # Build user message content (text + relevant screenshots)
    screenshots = _get_relevant_screenshots(question)
    if screenshots:
        print(f"[somm] Attaching {len(screenshots)} screenshot(s) for session {session_id[:8]}")

    user_content: list = []
    for img in screenshots:
        user_content.append(img)
    user_content.append({"type": "text", "text": question})

    # Add to history
    _add_to_history(session_id, "user", user_content)

    # Build messages list from history
    messages = list(session_history[session_id])

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2048,
                temperature=0,
                system=SYSTEM_PROMPT,
                messages=messages,
            )
            answer = response.content[0].text
            break
        except anthropic.RateLimitError as e:
            if attempt < max_retries - 1:
                wait = (attempt + 1) * 30
                print(f"[somm] Rate limited, retrying in {wait}s (attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
            else:
                print(f"[somm] Rate limit exceeded after {max_retries} retries: {e}", file=sys.stderr)
                session_history[session_id].pop()
                raise HTTPException(
                    status_code=503,
                    detail="The sommelier is busy right now. Please wait a minute and try again.",
                )
        except Exception as e:
            print(f"[somm] Anthropic API error: {e}", file=sys.stderr)
            session_history[session_id].pop()
            raise HTTPException(
                status_code=500,
                detail="The sommelier is unavailable right now. Please try again.",
            )

    # Store assistant reply as plain text in history
    _add_to_history(session_id, "assistant", answer)

    return {"answer": answer}
