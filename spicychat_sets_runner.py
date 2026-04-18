#!/usr/bin/env python3
# crushon_set_runner.py — Runs structured probe message sets from message_sets.json
#
# Attaches to YOUR real Brave browser and runs selected sets from the JSON file,
# saving per-set results with metadata for analysis.

import argparse
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("crushon_set_runner.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

DEFAULT_DELAY        = 5   # seconds between messages
DEFAULT_SET_DELAY    = 10  # seconds between sets
MESSAGE_TIMEOUT      = 60  # seconds to wait for AI response


def connect_to_brave(port: int = 9224) -> webdriver.Chrome:
    """Attach Selenium to the already-running Brave instance."""
    options = Options()
    options.binary_location = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
    options.add_experimental_option("debuggerAddress", f"127.0.0.1:{port}")

    try:
        service = Service(ChromeDriverManager(driver_version="147.0.7727.102").install())
        driver = webdriver.Chrome(service=service, options=options)
        log.info(f"Connected to Brave — current URL: {driver.current_url}")
        return driver
    except Exception as e:
        log.error(f"Could not connect to Brave on port {port}: {e}")
        raise


def find_chat_input(driver) -> object | None:
    """Find the message input box."""
    selectors = [
        (By.CSS_SELECTOR, "textarea[placeholder]"),
        (By.CSS_SELECTOR, "div[contenteditable='true']"),
        (By.CSS_SELECTOR, "textarea"),
    ]
    for by, selector in selectors:
        try:
            el = WebDriverWait(driver, 3).until(
                EC.presence_of_element_located((by, selector))
            )
            if el.is_displayed() and el.is_enabled():
                return el
        except TimeoutException:
            continue
    return None


def count_ai_spans(driver) -> int:
    """Count current AI message spans before sending a new message."""
    return driver.execute_script("""
        const allSpans = Array.from(document.querySelectorAll('span'));
        const ai = allSpans.filter(el => {
            const cls = el.className || '';
            return cls.includes('leading-6') &&
                   cls.includes('last:mb-0') &&
                   !cls.includes('text-white');
        });
        return ai.length;
    """)


def wait_and_extract_response(driver, span_count_before: int, timeout: int = 60) -> str | None:
    """
    Waits for NEW AI spans to appear after span_count_before,
    monitors until streaming stops, returns only the new response text.
    AI messages:   <span class="leading-6 mb-[10px] last:mb-0">
    User messages: <span class="leading-6 mb-[10px] last:mb-0 text-white !dark:text-white">
    """
    start_time = time.time()
    stable_count = 0
    prev_text = ""

    while time.time() - start_time < timeout:
        time.sleep(1.0)

        current_text = driver.execute_script("""
            const before = arguments[0];
            const allSpans = Array.from(document.querySelectorAll('span'));
            const aiSpans = allSpans.filter(el => {
                const cls = el.className || '';
                return cls.includes('leading-6') &&
                       cls.includes('last:mb-0') &&
                       !cls.includes('text-white');
            });
            const newSpans = aiSpans.slice(before);
            if (newSpans.length === 0) return "";
            return newSpans.map(s => s.innerText.trim()).filter(Boolean).join('\\n');
        """, span_count_before)

        if not current_text:
            stable_count = 0
            continue

        if current_text == prev_text:
            stable_count += 1
            if stable_count >= 3:
                return current_text
        else:
            stable_count = 0

        prev_text = current_text

    return prev_text if prev_text else None


def send_message(driver, message: str) -> dict:
    """Send a single message and return its response dict."""
    chat_input = find_chat_input(driver)
    if chat_input is None:
        log.error("Could not find chat input")
        return {"message": message, "response": None, "status": "error_no_input"}

    span_count_before = count_ai_spans(driver)

    chat_input.click()
    time.sleep(0.3)
    chat_input.send_keys(Keys.CONTROL + "a")
    time.sleep(0.1)
    chat_input.send_keys(message)
    time.sleep(0.5)
    chat_input.send_keys(Keys.RETURN)

    send_time = datetime.now(timezone.utc).isoformat()
    log.info(f"    Waiting for response (spans before: {span_count_before})...")

    response_text = wait_and_extract_response(driver, span_count_before, timeout=MESSAGE_TIMEOUT)

    status = "success" if response_text else "timeout"
    if response_text:
        preview = response_text.replace('\n', ' ')
        log.info(f"    Response: {preview[:80]}...")
    else:
        log.warning("    No response captured (timeout)")

    return {
        "message": message,
        "response": response_text,
        "timestamp": send_time,
        "status": status,
    }


def run_set(driver, probe_set: dict, delay: int, platform: str) -> dict:
    """Run all messages in a single probe set and return structured results."""
    set_id   = probe_set["id"]
    set_name = probe_set["name"]
    messages = probe_set["messages"]

    log.info(f"\n{'='*60}")
    log.info(f"SET {set_id}: {set_name}")
    log.info(f"Purpose: {probe_set['purpose'][:100]}...")
    log.info(f"Messages: {len(messages)}")
    log.info(f"{'='*60}")

    set_results = []
    for i, message in enumerate(messages):
        log.info(f"  [{i+1}/{len(messages)}] {message[:70]}...")
        result = send_message(driver, message)
        result["index"] = i + 1
        set_results.append(result)

        if i < len(messages) - 1:
            time.sleep(delay)

    successful = sum(1 for r in set_results if r["status"] == "success")
    log.info(f"  Set {set_id} done — {successful}/{len(messages)} successful")

    return {
        "set_id":        set_id,
        "set_name":      set_name,
        "purpose":       probe_set["purpose"],
        "paper":         probe_set.get("paper"),
        "what_to_code":  probe_set.get("what_to_code"),
        "platform":      platform,
        "total":         len(messages),
        "successful":    successful,
        "run_at":        datetime.now(timezone.utc).isoformat(),
        "results":       set_results,
    }


def load_sets(json_path: str, set_ids: list[str] | None) -> list[dict]:
    """Load and filter sets from the JSON file."""
    data = json.loads(Path(json_path).read_text(encoding="utf-8"))
    all_sets = data["sets"]

    if not set_ids:
        return all_sets

    # Filter by requested IDs (case-insensitive)
    requested = {s.upper() for s in set_ids}
    filtered  = [s for s in all_sets if s["id"].upper() in requested]

    missing = requested - {s["id"].upper() for s in filtered}
    if missing:
        log.warning(f"Set IDs not found in JSON: {missing}")

    return filtered


def print_summary(all_set_results: list[dict]) -> None:
    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    for sr in all_set_results:
        print(f"\n[{sr['set_id']}] {sr['set_name']}")
        print(f"     Successful: {sr['successful']}/{sr['total']}")
        print(f"     What to code: {sr.get('what_to_code', 'N/A')[:80]}")
        for r in sr["results"]:
            status = "✓" if r["status"] == "success" else "✗"
            msg    = r["message"][:38].ljust(38)
            resp   = str(r.get("response") or "NO RESPONSE")[:50].replace('\n', ' ')
            print(f"     {status} [{r['index']:>2}] {msg} → {resp}")


def main():
    parser = argparse.ArgumentParser(
        description="Run structured probe message sets against SpicyChat/CrushOn AI companions."
    )
    parser.add_argument(
        "--sets",    default="message_sets.json",
        help="Path to message_sets.json (default: message_sets.json)"
    )
    parser.add_argument(
        "--run",     nargs="+", metavar="SET_ID",
        help="Which set IDs to run, e.g. --run S1 S3 S5. Omit to run ALL sets."
    )
    parser.add_argument(
        "--output",  default="set_results.json",
        help="Output file for results (default: set_results.json)"
    )
    parser.add_argument("--port",      type=int, default=9224)
    parser.add_argument("--delay",     type=int, default=DEFAULT_DELAY,
                        help="Seconds between messages within a set")
    parser.add_argument("--set-delay", type=int, default=DEFAULT_SET_DELAY,
                        help="Seconds between sets")
    parser.add_argument("--url",       default=None,
                        help="Navigate Brave to this URL before starting")
    args = parser.parse_args()

    probe_sets = load_sets(args.sets, args.run)
    if not probe_sets:
        log.error("No sets to run — check your --run IDs or JSON path")
        return

    log.info(f"Sets to run: {[s['id'] for s in probe_sets]}")

    driver  = connect_to_brave(args.port)
    platform = driver.current_url.split('/')[2] if '/' in driver.current_url else "unknown"

    if args.url:
        driver.get(args.url)
        time.sleep(3)
        platform = driver.current_url.split('/')[2]

    if "spicychat.ai" not in driver.current_url and "crushon.ai" not in driver.current_url:
        log.warning(f"Brave is not on spicychat.ai or crushon.ai — current URL: {driver.current_url}")
        log.warning("Navigate to your character's chat page first, or use --url <chat_url>")
        driver.quit()
        return

    all_set_results = []
    for i, probe_set in enumerate(probe_sets):
        result = run_set(driver, probe_set, args.delay, platform)
        all_set_results.append(result)

        if i < len(probe_sets) - 1:
            log.info(f"  Waiting {args.set_delay}s before next set...")
            time.sleep(args.set_delay)

    # Save full output
    output = {
        "metadata": {
            "platform":   platform,
            "sets_run":   [s["set_id"] for s in all_set_results],
            "total_sets": len(all_set_results),
            "run_at":     datetime.now(timezone.utc).isoformat(),
        },
        "coding_scheme": json.loads(
            Path(args.sets).read_text(encoding="utf-8")
        ).get("coding_scheme", {}),
        "sets": all_set_results,
    }
    Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=False))
    log.info(f"\nResults saved to {args.output}")

    print_summary(all_set_results)


if __name__ == "__main__":
    main()