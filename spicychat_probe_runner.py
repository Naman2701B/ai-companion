#!/usr/bin/env python3
# crushon_probe_runner.py — Selenium + Remote Debugging Port (BRAVE VERSION)
#
# Attaches to YOUR real Brave browser. Uses targeted span selectors
# to extract AI responses from SpicyChat/CrushOn.

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
        logging.FileHandler("crushon_run.log", encoding="utf-8"),
        logging.StreamHandler(),
    ]
)
log = logging.getLogger(__name__)

DEFAULT_DELAY   = 5      # seconds between messages
MESSAGE_TIMEOUT = 60     # seconds to wait for AI response to finish streaming

# JS helper — avoids CSS escaping issues by matching class strings directly
_JS_GET_AI_SPANS = """
    const allSpans = Array.from(document.querySelectorAll('span'));
    return allSpans.filter(el => {
        const cls = el.className || '';
        return cls.includes('leading-6') &&
               cls.includes('last:mb-0') &&
               !cls.includes('text-white');
    });
"""


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
    return driver.execute_script(
        _JS_GET_AI_SPANS + "\n return allSpans.filter(el => {"
        + """
        const cls = el.className || '';
        return cls.includes('leading-6') &&
               cls.includes('last:mb-0') &&
               !cls.includes('text-white');
    }).length;
    """
    ) or driver.execute_script("""
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
            if stable_count >= 3:  # Unchanged for 3 seconds = streaming done
                return current_text
        else:
            stable_count = 0  # Still growing, reset

        prev_text = current_text

    return prev_text if prev_text else None


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


def send_messages(driver, messages: list[str], delay: int = DEFAULT_DELAY) -> list[dict]:
    results = []

    for i, message in enumerate(messages):
        log.info(f"[{i+1}/{len(messages)}] Sending: {message[:60]}...")

        chat_input = find_chat_input(driver)
        if chat_input is None:
            log.error("Could not find chat input — stopping")
            break

        # Snapshot AI span count BEFORE sending
        span_count_before = count_ai_spans(driver)
        log.info(f"  AI spans before send: {span_count_before}")

        # Send the message
        chat_input.click()
        time.sleep(0.3)
        chat_input.send_keys(Keys.CONTROL + "a")
        time.sleep(0.1)
        chat_input.send_keys(message)
        time.sleep(0.5)
        chat_input.send_keys(Keys.RETURN)

        send_time = datetime.now(timezone.utc).isoformat()

        log.info("  Waiting for AI to think and stream response...")

        response_text = wait_and_extract_response(driver, span_count_before, timeout=MESSAGE_TIMEOUT)

        result = {
            "index": i + 1,
            "message": message,
            "response": response_text,
            "timestamp": send_time,
            "platform": driver.current_url.split('/')[2],
            "approach": "selenium_remote_debug",
            "status": "success" if response_text else "timeout",
        }
        results.append(result)

        if response_text:
            preview = response_text.replace('\n', ' ')
            log.info(f"  Response captured: {preview[:80]}...")
        else:
            log.warning(f"  No response captured for message {i+1}")

        if i < len(messages) - 1:
            time.sleep(delay)

    return results


def load_messages(messages_arg: list[str]) -> list[str]:
    if len(messages_arg) == 1 and Path(messages_arg[0]).exists():
        p = Path(messages_arg[0])
        if p.suffix == ".json":
            return json.loads(p.read_text())
        return [l.strip() for l in p.read_text().splitlines() if l.strip()]
    return messages_arg


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", nargs="+", required=True)
    parser.add_argument("--output", default="results.json")
    parser.add_argument("--port", type=int, default=9224)
    parser.add_argument("--delay", type=int, default=DEFAULT_DELAY)
    parser.add_argument("--url", default=None, help="Navigate Brave to this URL before starting")
    args = parser.parse_args()

    messages = load_messages(args.messages)
    log.info(f"Loaded {len(messages)} messages")

    driver = connect_to_brave(args.port)

    # Optional: auto-navigate to the chat page
    if args.url:
        driver.get(args.url)
        time.sleep(3)

    if "spicychat.ai" not in driver.current_url and "crushon.ai" not in driver.current_url:
        log.warning(f"Brave is not on spicychat.ai or crushon.ai — current URL: {driver.current_url}")
        log.warning("Navigate to your character's chat page first, or use --url <chat_url>")
        driver.quit()
        return

    results = send_messages(driver, messages, args.delay)

    # Save output
    output = {
        "metadata": {
            "platform": driver.current_url.split('/')[2],
            "total": len(messages),
            "successful": sum(1 for r in results if r["status"] == "success"),
            "run_at": datetime.now(timezone.utc).isoformat(),
        },
        "results": results,
    }
    Path(args.output).write_text(json.dumps(output, indent=2, ensure_ascii=False))

    log.info(f"\nDone — {output['metadata']['successful']}/{len(messages)} successful")
    print("\n--- SUMMARY ---")
    for r in results:
        status = "✓" if r["status"] == "success" else "✗"
        msg = r["message"][:40].ljust(40)
        resp = str(r.get("response", "NO RESPONSE"))[:55].replace('\n', ' ')
        print(f"{status} [{r['index']:>2}] {msg} → {resp}")


if __name__ == "__main__":
    main()