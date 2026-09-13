"""
transcribe.py — Transcriber module (audiototext.com)

Usable two ways:
    1. As a module: from transcribe import transcribe_audio_file
    2. As a script:  python transcribe.py

Uses Playwright's ASYNC API so it can be called from Flask/threaded
code without hitting the 'Sync API inside asyncio loop' error.

Environment variables (optional):
    TRANSCRIBE_AUDIO_DIR  — where to store MP3s / transcripts
    TRANSCRIBER_HEADLESS  — "true" to run headless (required on Render)
"""

import os
import re
import time
import glob
import asyncio
import logging
import threading

from playwright.async_api import async_playwright

# ============================================================ #
# CONFIG (works both locally and on Render)                     #
# ============================================================ #
# On Render, /tmp is the only writable directory.
# Locally, we fall back to a captcha_audio folder next to this file.
def _default_audio_dir() -> str:
    if os.name == "nt":  # Windows
        # Original hardcoded path
        return r"C:\Users\PC\Desktop\tikscraptranscribe\tiktok-scraper\captcha_audio"
    # Linux / Render / macOS
    return "/tmp/captcha_audio"

CAPTCHA_AUDIO_DIR = os.environ.get(
    "TRANSCRIBE_AUDIO_DIR",
    _default_audio_dir()
)
os.makedirs(CAPTCHA_AUDIO_DIR, exist_ok=True)

TRANSCRIBE_URL = "https://audiototext.com/"
UPLOAD_TIMEOUT = int(os.environ.get("TRANSCRIBE_UPLOAD_TIMEOUT", "60"))
RESULT_TIMEOUT = int(os.environ.get("TRANSCRIBE_RESULT_TIMEOUT", "600"))
POLL_INTERVAL = int(os.environ.get("TRANSCRIBE_POLL_INTERVAL", "3"))

# Headless: force True on Render via env var, default False locally
_env_headless = os.environ.get("TRANSCRIBER_HEADLESS", "").lower()
if _env_headless in ("1", "true", "yes"):
    HEADLESS = True
elif _env_headless in ("0", "false", "no"):
    HEADLESS = False
else:
    # Auto-detect: headless on Linux (likely a server), visible on Windows
    HEADLESS = (os.name != "nt")


# ============================================================ #
# FILTERS                                                        #
# ============================================================ #
UI_BLOCKLIST_FRAGMENTS = [
    "audiototext.com",
    "free audio to text converter",
    "change language",
    "sign in", "sign up", "log in", "login", "register",
    "100% free", "no signup required", "secure processing",
    "why choose", "lightning-fast", "99% accuracy",
    "all audio formats", "multi-speaker", "instant processing",
    "100% secure & private",
    "powerful features at your fingertips",
    "how our audio to text converter works",
    "upload your audio file",
    "ai processing", "review & edit", "download results",
    "ready in minutes, not hours",
    "perfect for every use case",
    "meeting transcription", "interview & podcast",
    "educational content", "content creation",
    "trusted by professionals worldwide",
    "frequently asked questions",
    "start converting audio to text",
    "start transcribing now", "it's free!",
    "privacy policy", "terms of service", "contact us",
    "© 2026 audiototext", "all rights reserved",
    "processing audio file",
    "start transcription", "transcribe", "start over",
    "copy", "download", "edit", "share", "try again",
    "supported formats", "drag and drop",
    "features", "how it works", "faq", "getting started",
    "technology", "about us",
]

SPEAKER_LABEL_RE = re.compile(r"^SPK[_\s-]?\d+$", re.IGNORECASE)
TIMESTAMP_RE = re.compile(r"^\d{1,2}:\d{2}(?::\d{2})?$")


def _is_ui_noise(line: str) -> bool:
    l = line.strip().lower()
    if not l:
        return True
    if TIMESTAMP_RE.fullmatch(line.strip()):
        return True
    if SPEAKER_LABEL_RE.fullmatch(line.strip()):
        return True
    for frag in UI_BLOCKLIST_FRAGMENTS:
        if frag in l:
            return True
    return False


def _clean_transcript(text: str) -> str:
    """Keep only real transcript lines, strip punctuation & spaces, uppercase."""
    if not text:
        return ""
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s:
            continue
        if _is_ui_noise(s):
            continue
        s = re.sub(r"[.,!?;:'\"\-]", "", s)
        s = re.sub(r"\s+", "", s)
        if not s:
            continue
        out.append(s)
    return "\n".join(out).strip().upper()


# ============================================================ #
# HELPERS                                                        #
# ============================================================ #
def find_latest_mp3() -> str | None:
    files = glob.glob(os.path.join(CAPTCHA_AUDIO_DIR, "*.mp3"))
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _log(status, msg):
    if status is not None:
        status["message"] = msg
    print(msg)


# ============================================================ #
# SYNC ENTRY POINT (spawns its own thread + event loop)          #
# ============================================================ #
def transcribe_audio_file(audio_path: str, status=None) -> str | None:
    """
    Sync entry point. Runs the async transcriber in a brand-new thread
    with its own event loop so it's safe to call from Flask / any
    thread that already has an asyncio loop.
    """
    result = {"value": None, "error": None}

    def _run():
        try:
            result["value"] = asyncio.run(_transcribe_async(audio_path, status))
        except Exception as e:
            result["error"] = e
            print(f"❌ Async transcriber crashed: {e}")
            import traceback
            traceback.print_exc()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    t.join()

    if result["error"]:
        return None
    return result["value"]


# ============================================================ #
# ASYNC TRANSCRIBER                                              #
# ============================================================ #
async def _transcribe_async(audio_path: str, status=None) -> str | None:
    if not os.path.isfile(audio_path):
        _log(status, f"❌ Cannot transcribe — file not found: {audio_path}")
        return None

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=HEADLESS,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(viewport={"width": 1280, "height": 900})
        page = await context.new_page()
        try:
            return await _do_transcribe_async(page, audio_path, status)
        finally:
            try:
                await browser.close()
            except Exception:
                pass


async def _do_transcribe_async(page, audio_path: str, status=None) -> str | None:
    _log(status, f"\n🎙️  Opening {TRANSCRIBE_URL} ...")
    try:
        await page.goto(TRANSCRIBE_URL, wait_until="domcontentloaded", timeout=60000)
    except Exception as e:
        _log(status, f"❌ Failed to open transcriber: {e}")
        return None
    try:
        await page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    await asyncio.sleep(2)

    if not await _upload_file_async(page, audio_path, status=status):
        _log(status, "❌ Upload did not confirm.")
        return None

    await asyncio.sleep(2)

    if not await _click_start_async(page, status=status):
        return None

    _log(status, "⏳ Waiting for transcription to complete...")
    transcript = None
    deadline = time.time() + RESULT_TIMEOUT
    start_time = time.time()

    while time.time() < deadline:
        await asyncio.sleep(POLL_INTERVAL)
        transcript = await _try_find_transcript_async(page)
        if transcript:
            break
        try:
            body = await page.locator("body").inner_text(timeout=3000)
            preview = ""
            for ln in body.splitlines():
                s = ln.strip()
                if s and "audiototext" not in s.lower():
                    preview = s
                    break
            elapsed = int(time.time() - start_time)
            print(f"   [{elapsed:>3}s] {preview[:80]}")
        except Exception:
            pass

    if not transcript:
        _log(status, "❌ No transcript appeared within the timeout.")
        try:
            debug_png = os.path.join(CAPTCHA_AUDIO_DIR, "transcriber_debug.png")
            await page.screenshot(path=debug_png, full_page=True)
            print(f"   [debug] Screenshot saved: {debug_png}")
        except Exception:
            pass
        return None

    _log(status, f"✅ Transcript captured ({len(transcript)} chars)")

    txt_path = os.path.splitext(audio_path)[0] + ".txt"
    try:
        with open(txt_path, "w", encoding="utf-8") as f:
            f.write(transcript)
        _log(status, f"💾 Transcript saved: {txt_path}")
    except Exception as e:
        print(f"⚠️ Could not save transcript file: {e}")

    return transcript


async def _upload_file_async(page, audio_path: str, status=None) -> bool:
    _log(status, f"📤 Uploading {os.path.basename(audio_path)} ...")
    file_name = os.path.basename(audio_path)

    candidates = [
        'input[type="file"][accept*=".mp3"]',
        'input[type="file"][accept*="audio"]',
        'input[type="file"]',
    ]

    for sel in candidates:
        try:
            loc = page.locator(sel)
            n = await loc.count()
            if n == 0:
                continue
            for i in range(n):
                el = loc.nth(i)
                try:
                    await el.set_input_files(audio_path, timeout=20000)
                    print(f"   [try] set_input_files on '{sel}' [{i}]")
                except Exception as e:
                    print(f"   [try] '{sel}' [{i}] failed: {e}")
                    continue
                await asyncio.sleep(2)
                try:
                    page_text = await page.locator("body").inner_text(timeout=3000)
                except Exception:
                    page_text = ""
                if file_name in page_text:
                    _log(status, "   ✅ Filename detected on page — upload accepted")
                    return True
        except Exception as e:
            print(f"   [try] '{sel}' errored: {e}")
            continue
    return False


async def _click_start_async(page, status=None) -> bool:
    _log(status, "▶ Looking for 'Start Transcription' button...")
    selectors = [
        'button:has-text("Start Transcription")',
        'button:has-text("Transcription")',
        'button:has-text("Transcribe")',
        'button:has-text("Convert")',
    ]
    deadline = time.time() + UPLOAD_TIMEOUT
    while time.time() < deadline:
        for sel in selectors:
            try:
                loc = page.locator(sel)
                n = await loc.count()
                if n == 0:
                    continue
                for i in range(n):
                    el = loc.nth(i)
                    try:
                        if not await el.is_visible(timeout=500):
                            continue
                        if (await el.get_attribute("disabled")) is not None:
                            continue
                        await el.click(timeout=3000)
                        _log(status, f"✅ Clicked: {sel}")
                        return True
                    except Exception:
                        continue
            except Exception:
                continue
        await asyncio.sleep(1)
    _log(status, "❌ Could not click 'Start Transcription'")
    return False


async def _extract_from_timestamps_async(page) -> str | None:
    try:
        ts_locator = page.locator(r"text=/^\d{1,2}:\d{2}(?::\d{2})?$/")
        count = await ts_locator.count()
        if count == 0:
            return None
    except Exception:
        return None

    collected = []
    for i in range(count):
        try:
            ts_el = ts_locator.nth(i)
            if not await ts_el.is_visible(timeout=300):
                continue
            text_after = await ts_el.evaluate("""
                (node) => {
                    let el = node;
                    for (let depth = 0; depth < 5 && el; depth++) {
                        let sib = el.nextSibling;
                        while (sib) {
                            if (sib.nodeType === Node.TEXT_NODE) {
                                const t = (sib.textContent || '').trim();
                                if (t) return t;
                            }
                            if (sib.nodeType === Node.ELEMENT_NODE) {
                                const t = (sib.innerText || sib.textContent || '').trim();
                                if (t) return t;
                            }
                            sib = sib.nextSibling;
                        }
                        el = el.parentElement;
                    }
                    return '';
                }
            """)
            if text_after:
                collected.append(text_after.strip())
        except Exception as e:
            logging.debug(f"timestamp extract {i} failed: {e}")
            continue

    if not collected:
        return None
    joined = "\n".join(collected)
    cleaned = _clean_transcript(joined)
    return cleaned if cleaned and len(cleaned) >= 2 else None


async def _extract_from_dom_walk_async(page) -> str | None:
    try:
        result = await page.evaluate("""
            () => {
                const tsRe = /^\\d{1,2}:\\d{2}(?::\\d{2})?$/;
                const spkRe = /^SPK[_\\s-]?\\d+$/i;
                const lines = [];
                const all = document.querySelectorAll('*');
                for (const el of all) {
                    const t = (el.textContent || '').trim();
                    if (!tsRe.test(t)) continue;
                    if (el.children.length > 0) continue;
                    const container = el.parentElement;
                    if (!container) continue;
                    const full = (container.innerText || '').trim();
                    const parts = full.split('\\n')
                        .map(s => s.trim())
                        .filter(s => s && !tsRe.test(s) && !spkRe.test(s));
                    for (const p of parts) {
                        if (!lines.includes(p)) lines.push(p);
                    }
                }
                return lines.join('\\n');
            }
        """)
        if result:
            cleaned = _clean_transcript(result)
            if cleaned and len(cleaned) >= 2:
                return cleaned
    except Exception as e:
        logging.debug(f"DOM walk failed: {e}")
    return None


async def _try_find_transcript_async(page) -> str | None:
    t = await _extract_from_timestamps_async(page)
    if t:
        return t
    t = await _extract_from_dom_walk_async(page)
    if t:
        return t
    for sel in (
        '[class*="transcript" i]',
        '[class*="result" i]',
        '[data-testid*="transcript" i]',
    ):
        try:
            loc = page.locator(sel)
            n = await loc.count()
            for i in range(n):
                el = loc.nth(i)
                try:
                    if not await el.is_visible(timeout=300):
                        continue
                    raw = await el.inner_text(timeout=2000)
                    cleaned = _clean_transcript(raw)
                    if cleaned and 2 <= len(cleaned) < 2000:
                        return cleaned
                except Exception:
                    continue
        except Exception:
            continue
    return None


# ============================================================ #
# SCRIPT MODE                                                    #
# ============================================================ #
def main():
    print()
    print("=" * 60)
    print("        Audio Transcriber — audiototext.com")
    print("=" * 60)
    print(f"Folder: {CAPTCHA_AUDIO_DIR}")
    print(f"Headless: {HEADLESS}")
    print()

    target = find_latest_mp3()
    if not target:
        print(f"❌ No .mp3 files found in {CAPTCHA_AUDIO_DIR}")
        return

    print(f"📁 Latest file: {os.path.basename(target)}")
    print(f"   Size: {os.path.getsize(target)} bytes")
    print()

    transcript = transcribe_audio_file(target)
    if transcript:
        print()
        print("-" * 60)
        print("📝 TRANSCRIPT")
        print("-" * 60)
        print(transcript)
        print("-" * 60)
    else:
        print("\n❌ No transcript produced.")

    print()
    print("=" * 60)
    print("Done.")
    print("=" * 60)


if __name__ == "__main__":
    main()