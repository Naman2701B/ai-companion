#!/usr/bin/env python3

import argparse
import asyncio
import json
import os
import re
import time
import logging
from pathlib import Path

import pandas as pd
from tqdm import tqdm

try:
    from playwright.async_api import async_playwright, TimeoutError as PWTimeout
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False
    print("WARNING: playwright not installed. Run: pip install playwright && playwright install chromium")

try:
    from google import genai
    from google.genai import types as genai_types
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    print("WARNING: google-genai not installed. Run: pip install google-genai")

# Use utf-8 on both handlers to avoid cp1252 encoding errors on Windows
_file_handler = logging.FileHandler("verify_apps.log", encoding="utf-8")
_stream_handler = logging.StreamHandler()
# Wrap stdout so un-encodable Unicode chars are replaced instead of crashing
import sys, io
_stream_handler.setStream(
    io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace", line_buffering=True)
)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_file_handler, _stream_handler],
)
log = logging.getLogger(__name__)

MODEL = "gemma-3-12b-it"          # free tier: 30 RPM, 14,400 RPD — much more generous
MAX_TOKENS = 2048
PAGE_TIMEOUT_MS = 20_000          # 20s per page load
PAGE_TEXT_LIMIT = 6000            # chars of page text sent to Gemini
SLEEP_BETWEEN_PAGES = 1.5        # seconds
API_SLEEP = 2.5                  # seconds between calls (Gemma 30 RPM = 1 req/2s minimum)

OUTPUT_FIELDS = [
    "app_type",
    "web_accessible",
    "web_url",
    "login_required",
    "login_methods",
    "age_verification_required",
    "age_verification_method",
    "subscription_required_for_long_chat",
    "all_features_available_without_subscription",
    "subscription_features",
    "subscription_cost",
    "languages_supported",
]

SYSTEM_PROMPT = """You are a research assistant verifying AI companion app metadata.
You will receive:
  - App title and description
  - The text content scraped from the app's website (may be partial)
  - Current field values that may be defaults/wrong

Return ONLY a JSON object with these exact keys (no markdown, no preamble):

{
  "app_type": "companion" | "general_purpose" | "mixed" | "other",
  "web_accessible": true | false,
  "web_url": "<URL of web chat interface or null>",
  "login_required": true | false,
  "login_methods": "<comma-separated providers or null>",
  "age_verification_required": true | false,
  "age_verification_method": "<self-declaration | ID upload | credit card | null>",
  "subscription_required_for_long_chat": true | false,
  "all_features_available_without_subscription": true | false,
  "subscription_features": "<what paid plan unlocks or null>",
  "subscription_cost": "<monthly price with currency or null>",
  "languages_supported": "<ISO 639-1 codes comma-separated or null>"
}

RULES:
- app_type "companion": primary marketing is relational/emotional AI persona (girlfriend, boyfriend, waifu, roleplay partner)
- app_type "general_purpose": LLM interface (ChatGPT, Claude, Gemini, Grok, Copilot, Perplexity, etc.)
- app_type "mixed": both companion AND general-purpose/task features
- app_type "other": task-specific (homework, coding, translation, fitness, etc.) — NOT companion
- web_accessible true ONLY if the site has a functional AI chat interface (not just a landing/marketing page)
- login_required true if ANY cap exists before requiring login (even 5-10 free messages counts)
- subscription_required_for_long_chat true if sending thousands of messages needs a paid plan
- If you cannot determine a field from the available text, keep the current value or set to null
- Never invent pricing; only report what you can see on the page or in the description"""


def clean_text(text: str, limit: int = PAGE_TEXT_LIMIT) -> str:
    """Strip excess whitespace and truncate."""
    text = re.sub(r"\s+", " ", text or "").strip()
    return text[:limit]


def parse_llm_response(text: str) -> dict | None:
    text = text.strip()
    # Strip markdown fences if present
    text = re.sub(r"^```json\s*", "", text)
    text = re.sub(r"^```\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to extract first JSON object
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group())
            except Exception:
                pass
    return None


def should_skip(row: pd.Series, force: bool = False) -> bool:
    """Skip rows that have been KB-verified (web_url starts with http)."""
    if force:
        return False
    # Only skip if web_url is a real URL (starts with http) — meaning it was
    # manually verified via the knowledge base, not just a default placeholder.
    wu = str(row.get("web_url", "") or "").strip()
    if wu.startswith("http"):
        return True
    return False


async def fetch_page_text(page, url: str) -> str:
    """Load URL in Playwright and return visible text."""
    try:
        await page.goto(url, timeout=PAGE_TIMEOUT_MS, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)  # let JS render
        text = await page.evaluate("() => document.body ? document.body.innerText : ''")
        return clean_text(text)
    except Exception as e:
        log.warning(f"Failed to fetch {url}: {e}")
        return ""


async def get_page_texts(browser, row: pd.Series) -> dict:
    """Return dict of {source: text} for the app's website and store page."""
    texts = {}
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    )
    page = await context.new_page()

    # 1. Developer website
    site = str(row.get("developerWebsite", "") or "").strip()
    if site and site != "nan":
        if not site.startswith("http"):
            site = "https://" + site
        text = await fetch_page_text(page, site)
        if text:
            texts["website"] = text

    # 2. Store page as fallback (truncated — mostly has description)
    store_url = str(row.get("store_url", "") or "").strip()
    if store_url and store_url != "nan" and "website" not in texts:
        text = await fetch_page_text(page, store_url)
        if text:
            texts["store"] = text

    await context.close()
    return texts


def verify_with_gemini(model, row: pd.Series, page_texts: dict) -> dict | None:
    """Send app metadata + page text to Gemini Flash and return parsed field dict."""
    current = {f: row.get(f, None) for f in OUTPUT_FIELDS}

    website_text = page_texts.get("website") or page_texts.get("store") or ""

    # Gemini takes system + user as a single combined prompt
    prompt = f"""{SYSTEM_PROMPT}

APP METADATA:
Title: {row['title']}
Platform: {row.get('platform','')}
Genre: {row.get('genre','')}
Content Rating: {row.get('contentRating','')}
Developer Website: {row.get('developerWebsite','')}
Description: {str(row.get('description',''))[:600]}

CURRENT FIELD VALUES (may be defaults/wrong):
{json.dumps(current, indent=2)}

WEBSITE TEXT SCRAPED:
{website_text[:PAGE_TEXT_LIMIT] if website_text else '(no website text available)'}

Please verify and correct the fields based on all available evidence."""

    for attempt in range(4):  # up to 4 attempts per app
        try:
            resp = model.models.generate_content(
                model=MODEL,
                contents=prompt,
                config=genai_types.GenerateContentConfig(
                    max_output_tokens=MAX_TOKENS,
                    temperature=0.1,
                ),
            )
            text = resp.text.strip()
            result = parse_llm_response(text)
            if result:
                return result
            log.warning(f"Could not parse Gemini response for {row['title']}: {text[:200]}")
            return None  # bad response, don't retry

        except Exception as e:
            msg = str(e)

            # 503 — server overloaded, exponential backoff
            if "503" in msg or "UNAVAILABLE" in msg or "high demand" in msg.lower():
                wait = 30 * (2 ** attempt)  # 30s, 60s, 120s, 240s
                log.warning(f"503 on attempt {attempt+1} for '{row['title']}' — retrying in {wait}s")
                time.sleep(wait)
                continue

            # 429 — rate limited, wait longer
            elif "429" in msg or "quota" in msg.lower() or "RESOURCE_EXHAUSTED" in msg:
                wait = 60 * (attempt + 1)
                log.warning(f"Rate limited for '{row['title']}' — waiting {wait}s")
                time.sleep(wait)
                continue

            # Anything else — log and give up
            else:
                log.error(f"Gemini API error for {row['title']}: {e}")
                time.sleep(5)
                return None

    log.warning(f"All retries exhausted for '{row['title']}'")
    return None


async def process_batch(browser, client, rows: list[pd.Series], semaphore: asyncio.Semaphore, results: dict, sidecar_path: Path = None):
    """Process a batch of rows concurrently."""
    async def process_one(row):
        async with semaphore:
            title = row["title"]
            log.info(f"Verifying: {title}")
            try:
                page_texts = await get_page_texts(browser, row)
                await asyncio.sleep(SLEEP_BETWEEN_PAGES)

                verified = verify_with_gemini(client, row, page_texts)
                time.sleep(API_SLEEP)

                if verified:
                    results[title] = verified
                    log.info(f"  ✓ {title}: web={verified.get('web_accessible')}, type={verified.get('app_type')}")
                else:
                    results[title] = None
                    log.warning(f"  ✗ {title}: verification failed, keeping defaults")
                # Always record as processed (even failures) so resume skips it
                try:
                    with open(sidecar_path, "a", encoding="utf-8") as sf:
                        sf.write(title + "\n")
                except Exception:
                    pass
            except Exception as e:
                log.error(f"Error processing {title}: {e}")
                results[title] = None

    await asyncio.gather(*[process_one(row) for row in rows])


async def run(args):
    if not PLAYWRIGHT_AVAILABLE:
        raise RuntimeError("playwright not installed")
    if not GENAI_AVAILABLE:
        raise RuntimeError("google-genai not installed. Run: pip install google-genai")

    # Load CSV
    df = pd.read_csv(args.input)
    log.info(f"Loaded {len(df)} rows from {args.input}")

    # Load existing output if resuming
    out_path = Path(args.output)
    # Sidecar file tracks titles actually processed by Gemma (not just defaults)
    sidecar_path = out_path.with_suffix(".processed.txt")

    if out_path.exists() and args.resume:
        done_df = pd.read_csv(out_path)
        # Use sidecar if it exists — it tracks only Gemma-processed titles
        if sidecar_path.exists():
            done_titles = set(
                l.strip() for l in sidecar_path.read_text(encoding="utf-8").splitlines()
                if l.strip()
            )
            log.info(f"Resuming from sidecar: {len(done_titles)} actually processed by Gemma")
        else:
            # Fallback: no sidecar means we cannot trust the output CSV row count
            # Start fresh — the output CSV will still be updated correctly
            done_titles = set()
            log.warning(
                "No sidecar file found — cannot determine which rows Gemma processed. "
                "Re-verifying all non-KB rows. Output CSV will be updated in place."
            )
    else:
        done_df = None
        done_titles = set()

    # Filter rows to verify
    to_verify = []
    skip_count = 0
    for _, row in df.iterrows():
        title = str(row["title"]).strip()
        if title in done_titles:
            skip_count += 1
            continue
        if not args.force and should_skip(row):
            skip_count += 1
            continue
        to_verify.append(row)

    log.info(f"Skipping {skip_count} already-verified rows")
    log.info(f"Verifying {len(to_verify)} rows")

    if not to_verify:
        log.info("Nothing to verify.")
        return

    # Init Gemini client
    api_key = args.api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError(
            "Set --api-key or GEMINI_API_KEY env var.\n"
            "Get a free key at: https://aistudio.google.com -> Get API key"
        )
    client = genai.Client(api_key=api_key)

    log.info("Validating Gemini API key...")
    try:
        client.models.generate_content(
            model=MODEL,
            contents="Reply with the word OK only.",
            config=genai_types.GenerateContentConfig(max_output_tokens=5),
        )
        log.info("Gemini API key valid — free tier active")
    except Exception as e:
        msg = str(e)
        # 429 = rate limited but key IS valid — safe to continue
        if "429" in msg or "quota" in msg.lower() or "RESOURCE_EXHAUSTED" in msg:
            log.warning("Rate limited on preflight — key is valid, continuing with delays")
        # 400/401/403 = bad key
        elif any(x in msg for x in ["400", "401", "403", "api_key", "invalid", "API_KEY_INVALID"]):
            log.error(f"Invalid Gemini API key: {e}")
            log.error("Keys start with 'AIza' — get one at: https://aistudio.google.com/apikey")
            raise SystemExit(1)
        else:
            raise

    results = {}
    semaphore = asyncio.Semaphore(args.concurrency)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)

        # Process in chunks, saving incrementally
        chunk_size = 20
        for i in tqdm(range(0, len(to_verify), chunk_size), desc="Batches"):
            chunk = to_verify[i : i + chunk_size]
            await process_batch(browser, client, chunk, semaphore, results, sidecar_path)

            # Save incremental checkpoint
            _save_checkpoint(df, results, done_df, out_path)
            log.info(f"Checkpoint saved ({len(results)} verified so far)")

        await browser.close()

    # Final save
    _save_checkpoint(df, results, done_df, out_path)
    log.info(f"Done. Output: {out_path}")


def _save_checkpoint(df: pd.DataFrame, results: dict, done_df, out_path: Path):
    """Merge verified results into df and save."""
    out = df.copy()
    for title, verified in results.items():
        if verified is None:
            continue
        mask = out["title"].str.strip() == title
        for field, value in verified.items():
            if field in out.columns:
                out.loc[mask, field] = value

    # Append previously-done rows if resuming
    if done_df is not None:
        already_done_mask = out["title"].isin(done_df["title"])
        out = pd.concat([done_df, out[~already_done_mask]], ignore_index=True)

    out.to_csv(out_path, index=False, encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="Verify AI companion app CSV fields")
    parser.add_argument("--input",       required=True, help="Input CSV path")
    parser.add_argument("--output",      required=True, help="Output CSV path")
    parser.add_argument("--api-key",     default=None,  help="Gemini API key from aistudio.google.com (or set GEMINI_API_KEY env var)")
    parser.add_argument("--concurrency", type=int, default=1, help="Parallel browser tabs (default: 1 for Gemini free tier stability)")
    parser.add_argument("--force",       action="store_true", help="Re-verify even KB-verified rows")
    parser.add_argument("--resume",      action="store_true", help="Resume from existing output file")
    parser.add_argument("--only-type",   default=None, help="Only verify rows of this app_type (companion/general_purpose/mixed/other)")
    parser.add_argument("--limit",       type=int, default=None, help="Only verify first N rows (for testing)")
    args = parser.parse_args()

    asyncio.run(run(args))


if __name__ == "__main__":
    main()