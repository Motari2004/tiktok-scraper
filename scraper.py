import time
import requests
import os
import logging
import json
import re
import wave
from urllib.parse import urlparse
from playwright.sync_api import sync_playwright, Page

# ============================================================ #
# CONFIGURATION                                                 #
# ============================================================ #
CAPTCHA_AUDIO_DIR = "captcha_audio"
os.makedirs(CAPTCHA_AUDIO_DIR, exist_ok=True)

_manual_clicked_element_data = {"selector": None, "outer_html": None, "timestamp": None}

PRE_PLAY_DELAY = 5
AUDIO_CAPTURE_TIMEOUT = 60
MANUAL_PLAY_GRACE = 15
MIN_AUDIO_BYTES = 5000
CODE_INPUT_TIMEOUT = 300
MANUAL_SOLVE_TIMEOUT = 300


# ============================================================ #
# STATUS                                                        #
# ============================================================ #
def set_status(status, message, found=None):
    if status is not None:
        status["message"] = message
        if found is not None:
            status["found"] = found
    print(message)


# ============================================================ #
# PLAY THE SAVED MP3 LOCALLY                                    #
# ============================================================ #
def play_audio_file(path):
    try:
        if not path or not os.path.exists(path):
            print(f"[Play] File not found: {path}")
            return
        print(f"[Play] Playing: {path}")
        import platform
        import subprocess
        system = platform.system()
        if system == "Windows":
            os.startfile(path)
        elif system == "Darwin":
            subprocess.Popen(["afplay", path])
        else:
            for player in ("ffplay", "mpg123", "aplay"):
                try:
                    cmd = [player, "-nodisp", "-autoexit", path] if player == "ffplay" else [player, path]
                    subprocess.Popen(cmd)
                    return
                except FileNotFoundError:
                    continue
            print("[Play] No audio player found on Linux.")
    except Exception as e:
        print(f"[Play] Failed: {e}")


# ============================================================ #
# ASK THE USER FOR THE CODE (fallback only)                     #
# ============================================================ #
def prompt_for_code(timeout=CODE_INPUT_TIMEOUT):
    print()
    print("=" * 60)
    print("🎧 AUDIO SAVED — LISTEN AND ENTER THE CODE")
    print("=" * 60)
    print(f"File: {_audio_listener.get('saved_path')}")
    print()
    print("Type the digits you hear, then press ENTER.")
    print("  'skip'  → don't auto-fill, I'll solve it manually")
    print("  'q'     → abort")
    print("=" * 60)

    try:
        user_input = input("CODE > ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\n[Code] Cancelled.")
        return None

    if not user_input:
        return None
    if user_input.lower() in ("q", "quit", "abort"):
        return None
    if user_input.lower() == "skip":
        return "__SKIP__"
    code = re.sub(r"\D", "", user_input)
    return code or None


# ============================================================ #
# AUTO-FILL THE CODE INTO TIKTOK                                #
# ============================================================ #
def fill_code_into_page(page: Page, code: str, status=None):
    if not code:
        return False

    set_status(status, f"⌨️  Typing code: {code}")

    candidate_selectors = [
        'input[type="text"][maxlength="5"]',
        'input[type="text"][maxlength="6"]',
        'input[type="tel"]',
        'input[aria-label*="code" i]',
        'input[placeholder*="code" i]',
        'input[placeholder*="Enter" i]',
        'input[inputmode="numeric"]',
        'input[type="text"]',
        'input:not([type="hidden"])',
    ]

    filled = False
    for sel in candidate_selectors:
        try:
            loc = page.locator(sel)
            count = loc.count()
            if count == 0:
                continue
            for i in range(count):
                el = loc.nth(i)
                try:
                    if not el.is_visible(timeout=500):
                        continue
                    el.click(timeout=1500)
                    el.fill("")
                    el.type(code, delay=80)
                    set_status(status, f"✅ Code typed into: {sel}")
                    filled = True
                    break
                except Exception as e:
                    logging.debug(f"Fill failed for {sel}[{i}]: {e}")
                    continue
            if filled:
                break
        except Exception:
            continue

    if not filled:
        set_status(status, "⚠️ Could not find the input field — type it in the browser.")
        return False

    submit_selectors = [
        'button:has-text("Verify")',
        'button:has-text("Submit")',
        'button:has-text("Confirm")',
        'button:has-text("Continue")',
        'button[type="submit"]',
        '[aria-label*="verify" i]',
    ]
    for sel in submit_selectors:
        try:
            loc = page.locator(sel)
            if loc.count() > 0 and loc.first.is_visible(timeout=800):
                set_status(status, f"↩ Submitting via: {sel}")
                loc.first.click(timeout=2000)
                return True
        except Exception:
            continue

    set_status(status, "⚠️ No submit button found — press Enter in the browser.")
    return True


# ============================================================ #
# DETECT TIKTOK VERIFICATION                                    #
# ============================================================ #
def detect_verification(page: Page):
    result = {"detected": False, "slider_text": False, "audio_present": False, "details": []}
    try:
        try:
            url = page.url.lower()
            if any(w in url for w in ["captcha", "challenge", "verification", "verify"]):
                result["detected"] = True
                result["details"].append("Verification URL detected")
        except Exception:
            pass

        body_text = ""
        try:
            body_text = page.locator("body").inner_text(timeout=3000).lower()
        except Exception:
            pass

        if "drag the slider to fit the puzzle" in body_text:
            result["detected"] = True
            result["slider_text"] = True
            result["details"].append('Found: "Drag the slider to fit the puzzle"')

        for text in ["drag the slider", "fit the puzzle", "verify you are human",
                     "security verification", "verification required",
                     "complete the puzzle", "slide to verify", "human verification"]:
            if text in body_text:
                result["detected"] = True
                detail = f'Found verification text: "{text}"'
                if detail not in result["details"]:
                    result["details"].append(detail)

        audio_selectors = [
            "audio", "[aria-label='Play']",
            "[aria-label*='audio' i]", "[aria-label*='listen' i]",
            "[aria-label*='sound' i]", "[aria-label*='speaker' i]",
            "[title*='audio' i]", "[title*='listen' i]",
            "[title*='sound' i]", "[title*='speaker' i]",
            "button:has-text('Audio')", "button:has-text('Listen')",
            "button:has-text('Sound')",
            "button[aria-label*='play' i]",
            "div[role='button'][aria-label*='play' i]"
        ]
        for selector in audio_selectors:
            try:
                loc = page.locator(selector)
                if loc.count() > 0 and loc.first.is_visible(timeout=1000):
                    result["audio_present"] = True
                    result["details"].append(f"Audio control: {selector}")
                    break
            except Exception:
                continue

        if result["detected"]:
            print()
            print("=" * 60)
            print("🔐 VERIFICATION DETECTED")
            print("=" * 60)
            for detail in result["details"]:
                print("   •", detail)
            print("=" * 60)
            print()
        return result
    except Exception as error:
        print(f"Verification detection error: {error}")
        return result


def is_verification_page(page: Page):
    return detect_verification(page)["detected"]


# ============================================================ #
# AUDIO LISTENER                                                #
# ============================================================ #
_audio_listener = {
    "captured_url": None,
    "captured_content_type": None,
    "captured_body": None,
    "saved_path": None,
    "armed": False,
    "all_urls": [],
    "context": None,
}


def _on_route(route, request):
    try:
        if not _audio_listener["armed"]:
            route.continue_()
            return

        url = request.url
        url_l = url.lower()
        path = urlparse(url).path.lower()

        if any(k in url_l for k in ("captcha", "voice", "verify", "audio", "sound")):
            _audio_listener["all_urls"].append((request.method, request.resource_type, url[:160]))
            print(f"[ROUTE] {request.method} {request.resource_type} {url[:140]}")

        if _audio_listener["captured_url"]:
            route.continue_()
            return

        is_candidate = (
            any(path.endswith(ext) for ext in
                (".mp3", ".wav", ".ogg", ".m4a", ".aac", ".opus"))
            and "/captcha/get" not in url_l
            and "/captcha/verify" not in url_l
        )

        if not is_candidate:
            route.continue_()
            return

        _audio_listener["captured_url"] = url
        print(f"🔊 AUDIO URL CAPTURED: {url[:160]}")
        route.continue_()
    except Exception as e:
        print(f"[ROUTE] Error: {e}")
        try:
            route.continue_()
        except Exception:
            pass


def arm_audio_listener(page: Page, context=None):
    _audio_listener["captured_url"] = None
    _audio_listener["captured_content_type"] = None
    _audio_listener["captured_body"] = None
    _audio_listener["saved_path"] = None
    _audio_listener["all_urls"] = []
    _audio_listener["armed"] = True
    _audio_listener["context"] = context
    page.route("**/*", _on_route)


def disarm_audio_listener(page: Page):
    _audio_listener["armed"] = False
    try:
        page.unroute("**/*", _on_route)
    except Exception:
        pass


# ============================================================ #
# SAVE CAPTURED AUDIO                                           #
# ============================================================ #
def save_captured_audio(page: Page, status=None):
    url = _audio_listener.get("captured_url")
    if not url:
        return None

    context = _audio_listener.get("context")
    body = None

    if context:
        try:
            print("[Save] Fetching via context.request...")
            resp = context.request.get(
                url,
                headers={"Referer": page.url, "Accept": "*/*"},
                timeout=20000,
            )
            if resp.status in (200, 206):
                body = resp.body()
                print(f"[Save] context.request got {len(body)} bytes (status {resp.status})")
            else:
                print(f"[Save] context.request status {resp.status}")
        except Exception as e:
            print(f"[Save] context.request failed: {e}")

    if not body or len(body) < MIN_AUDIO_BYTES:
        try:
            print("[Save] Trying requests fallback...")
            headers = {
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/143.0.0.0 Safari/537.36"),
                "Referer": page.url,
                "Origin": "https://www.tiktok.com",
                "Accept": "*/*",
            }
            r = requests.get(url, headers=headers, timeout=20)
            print(f"[Save] requests status {r.status_code}, "
                  f"ct {r.headers.get('content-type')}, len {len(r.content)}")
            if r.status_code in (200, 206) and len(r.content) > len(body or b""):
                body = r.content
        except Exception as e:
            print(f"[Save] requests failed: {e}")

    if not body or len(body) < MIN_AUDIO_BYTES:
        set_status(status, f"❌ Download failed or too small "
                           f"({len(body) if body else 0} bytes)")
        _audio_listener["captured_url"] = None
        return None

    ext = ".mp3"
    if body[:3] == b"ID3" or body[:2] in (b"\xff\xfb", b"\xff\xf3"):
        ext = ".mp3"
    elif body[:4] == b"OggS":
        ext = ".ogg"
    elif body[:4] == b"RIFF":
        ext = ".wav"
    elif ".wav" in url.lower():
        ext = ".wav"

    path = os.path.join(CAPTCHA_AUDIO_DIR, f"captcha_{int(time.time())}_voice{ext}")
    with open(path, "wb") as f:
        f.write(body)

    _audio_listener["saved_path"] = path
    set_status(status, f"✅ Audio saved ({len(body)} bytes): {path}")
    return path


def wait_for_audio_and_save(page: Page, status=None, timeout=AUDIO_CAPTURE_TIMEOUT):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _audio_listener["captured_url"]:
            time.sleep(0.5)
            saved = save_captured_audio(page, status)
            if saved:
                return saved
        time.sleep(0.3)
    set_status(status, f"⏱ No audio captured within {timeout}s.")
    return None


# ============================================================ #
# OPEN AUDIO OPTION                                             #
# ============================================================ #
def open_audio_option(page: Page, status=None):
    global _manual_clicked_element_data

    initial_audio_control_selectors = [
        "[aria-label*='audio' i]", "[aria-label*='listen' i]",
        "[aria-label*='sound' i]", "[aria-label*='speaker' i]",
        "[title*='audio' i]", "[title*='listen' i]",
        "[title*='sound' i]", "[title*='speaker' i]",
        "button:has-text('Audio')", "button:has-text('Listen')",
        "button:has-text('Sound')",
    ]

    initial_control_clicked = False
    for selector in initial_audio_control_selectors:
        try:
            locator = page.locator(selector)
            count = locator.count()
            if count == 0:
                continue
            for index in range(count):
                element = locator.nth(index)
                if element.is_visible(timeout=1000):
                    set_status(status, f"🔊 Audio control detected: {selector}. Clicking...")
                    element.click(timeout=3000)
                    initial_control_clicked = True
                    set_status(status, "🔊 Audio challenge opened.")
                    break
            if initial_control_clicked:
                break
        except Exception as error:
            logging.debug(f"Could not click '{selector}': {error}")
            continue

    if not initial_control_clicked:
        set_status(status, "🔊 No clickable audio control detected.")
        return False

    arm_audio_listener(page, context=page.context)

    set_status(status, f"⏳ Waiting {PRE_PLAY_DELAY}s for audio modal to fully load...")
    time.sleep(PRE_PLAY_DELAY)

    play_icon_selector = "button[aria-label='Play']"
    try:
        play_locator = page.locator(play_icon_selector).first
        play_locator.wait_for(state="visible", timeout=8000)

        try:
            page.wait_for_function(
                """() => {
                    const b = document.querySelector("button[aria-label='Play']");
                    return b && !b.disabled && b.offsetParent !== null;
                }""",
                timeout=8000
            )
        except Exception as wf_err:
            logging.debug(f"wait_for_function(Play enabled) failed: {wf_err}")

        time.sleep(1.0)

        set_status(status, "▶ Clicking Play programmatically...")
        play_locator.click(timeout=5000)
        set_status(status, "▶ Play clicked — listening for the MP3 URL...")
    except Exception as e:
        logging.debug(f"Programmatic Play click failed: {e}")
        set_status(status, "▶ Programmatic Play click failed — click Play manually.")

    set_status(status, "🎧 Listening for audio network request...")
    audio_file = wait_for_audio_and_save(page, status, timeout=AUDIO_CAPTURE_TIMEOUT)

    if not audio_file:
        set_status(
            status,
            f"⚠️ No audio after programmatic click. "
            f"Waiting {MANUAL_PLAY_GRACE}s for manual Play..."
        )
        deadline = time.time() + MANUAL_PLAY_GRACE
        while time.time() < deadline:
            if _audio_listener["captured_url"]:
                break
            time.sleep(0.5)
        audio_file = save_captured_audio(page, status)

    if not audio_file and _audio_listener["all_urls"]:
        print("\n[DEBUG] Requests seen while listening:")
        for method, rtype, u in _audio_listener["all_urls"][-25:]:
            print(f"   {method} {rtype} {u}")

    disarm_audio_listener(page)

    if not audio_file:
        set_status(status, "❌ Could not capture audio.")
        return False

    # Play the audio locally
    play_audio_file(audio_file)

    # Auto-transcribe
    transcript = None
    try:
        from transcribe import transcribe_audio_file
        set_status(status, "🎙️  Sending audio to audiototext.com...")
        transcript = transcribe_audio_file(audio_file, status=status)
    except Exception as e:
        set_status(status, f"❌ Transcriber failed: {e}")

    code = None
    if transcript:
        digits = re.sub(r"\D", "", transcript)
        if digits:
            code = digits
        else:
            code = re.sub(r"\s+", "", transcript)
        set_status(status, f"🔤 Transcript: {transcript!r}")
        set_status(status, f"⌨️  Using code: {code!r}")

    if not code:
        set_status(status, "⚠️ Transcription failed — falling back to manual prompt.")
        user_code = prompt_for_code()
        if user_code is None:
            set_status(status, "User cancelled code entry.")
            return False
        if user_code == "__SKIP__":
            set_status(status, "User chose to skip auto-fill.")
            return _wait_for_verification_cleared(page, status)
        code = user_code

    fill_code_into_page(page, code, status)
    return _wait_for_verification_cleared(page, status)


# ============================================================ #
# WAIT FOR VERIFICATION TO CLEAR                                #
# ============================================================ #
def _wait_for_verification_cleared(page: Page, status=None, timeout=60):
    set_status(status, "⏳ Waiting for verification to clear...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not detect_verification(page)["detected"]:
            time.sleep(2)
            if not detect_verification(page)["detected"]:
                set_status(status, "✅ Verification cleared.")
                return True
        time.sleep(1)
    set_status(status, "⚠️ Verification still present after submission.")
    return False


# ============================================================ #
# WAIT FOR VERIFICATION                                         #
# ============================================================ #
def wait_for_verification(page: Page, status=None):
    set_status(status, "⏸ VERIFICATION REQUIRED")
    detection = detect_verification(page)

    if detection["audio_present"]:
        set_status(status, "🔊 Audio option detected. Processing...")
        return open_audio_option(page, status)
    else:
        set_status(status, "🔊 No audio option detected.")
        return False


# ============================================================ #
# ENSURE VERIFICATION IS CLEAR                                  #
# ============================================================ #
def ensure_verification_clear(page: Page, status=None):
    while True:
        detection = detect_verification(page)
        if not detection["detected"]:
            return True
        set_status(status, "🔐 Verification detected — handling.")
        wait_for_verification(page, status)
        time.sleep(2)


# ============================================================ #
# COLLECT VIDEO URLS                                            #
# ============================================================ #
def collect_video_urls(page: Page, max_videos, status=None):
    """Robust TikTok profile video URL collector (4 strategies)."""
    found = set()

    # Strategy 1: standard anchor href
    try:
        for a in page.locator('a[href*="/video/"]').all():
            try:
                href = a.get_attribute("href")
                if not href:
                    continue
                if href.startswith("/"):
                    href = "https://www.tiktok.com" + href
                found.add(href.split("?")[0])
                if len(found) >= max_videos:
                    break
            except Exception:
                continue
    except Exception:
        pass

    # Strategy 2: data-e2e cards
    if len(found) < max_videos:
        try:
            for card in page.locator('[data-e2e="user-post-item"]').all():
                try:
                    inner = card.locator('a[href*="/video/"]').first
                    href = inner.get_attribute("href", timeout=500)
                    if href:
                        if href.startswith("/"):
                            href = "https://www.tiktok.com" + href
                        found.add(href.split("?")[0])
                        if len(found) >= max_videos:
                            break
                except Exception:
                    continue
        except Exception:
            pass

    # Strategy 3: raw JS scan of all hrefs
    if len(found) < max_videos:
        try:
            hrefs = page.evaluate("""
                () => Array.from(document.querySelectorAll('a'))
                    .map(a => a.href)
                    .filter(h => h && h.includes('/video/'))
            """)
            for href in hrefs:
                if href.startswith("/"):
                    href = "https://www.tiktok.com" + href
                found.add(href.split("?")[0])
                if len(found) >= max_videos:
                    break
        except Exception as e:
            logging.debug(f"JS href scan failed: {e}")

    # Strategy 4: data-* attribute extraction
    if len(found) < max_videos:
        try:
            video_ids = page.evaluate("""
                () => {
                    const ids = new Set();
                    document.querySelectorAll('[data-e2e]').forEach(el => {
                        for (const attr of el.attributes) {
                            const m = attr.value && attr.value.match(/\\/video\\/(\\d{15,25})/);
                            if (m) ids.add(m[1]);
                        }
                    });
                    return Array.from(ids);
                }
            """)
            user_match = re.search(r"tiktok\.com/@([^/?#]+)", page.url)
            if user_match and video_ids:
                username = user_match.group(1)
                for vid in video_ids:
                    found.add(f"https://www.tiktok.com/@{username}/video/{vid}")
                    if len(found) >= max_videos:
                        break
        except Exception as e:
            logging.debug(f"data-attr scan failed: {e}")

    if status is not None:
        status["found"] = len(found)
    return found


# ============================================================ #
# MAIN WORKER                                                   #
# ============================================================ #
def run_for_url(profile_url, max_videos=50, scrolls=15, status=None):
    found = set()
    with sync_playwright() as p:
        set_status(status, "🌐 Launching Chromium...")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        try:
            set_status(status, f"🌐 Opening profile: {profile_url}")
            page.goto(profile_url, wait_until="domcontentloaded", timeout=60000)

            # Wait for the profile grid to hydrate
            try:
                page.wait_for_selector(
                    '[data-e2e="user-post-item"], a[href*="/video/"]',
                    timeout=15000
                )
                print("[INFO] Video grid detected.")
            except Exception as e:
                print(f"[WARN] Video grid not detected within 15s: {e}")

            time.sleep(5)

            # --- Check for verification ---
            set_status(status, "🔎 Checking for verification...")
            for _ in range(10):
                detection = detect_verification(page)
                if detection["detected"]:
                    set_status(status, "🔐 TikTok verification detected.")
                    completed = wait_for_verification(page, status)
                    if not completed:
                        set_status(status, "❌ Verification not cleared.")
                        return []
                    break
                time.sleep(1)

            # --- Post-CAPTCHA verification is clear: now scrape ---
            ensure_verification_clear(page, status)
            time.sleep(3)
            ensure_verification_clear(page, status)

            set_status(status, "🚀 Verification complete — starting collection.")

            for scroll_number in range(scrolls):
                ensure_verification_clear(page, status)
                current = scroll_number + 1
                set_status(status, f"📜 {current}/{scrolls}")

                new_urls = collect_video_urls(page, max_videos, status)
                print(f"[DEBUG] scroll {current}: +{len(new_urls)} URLs, "
                      f"total {len(found) + len(new_urls)}")
                found.update(new_urls)
                if status is not None:
                    status["found"] = len(found)

                if len(found) >= max_videos:
                    set_status(status, f"🎯 Reached {max_videos} videos.")
                    break

                ensure_verification_clear(page, status)

                # Scroll a bit at a time so lazy-loaded cards render
                for _ in range(5):
                    page.mouse.wheel(0, 500)
                    time.sleep(0.5)
                time.sleep(3)

            results = list(found)[:max_videos]
            set_status(status, f"✅ Done — found {len(results)} video URLs.", len(results))
            return results
        except Exception as error:
            set_status(status, f"❌ Error: {error}")
            return []
        finally:
            browser.close()


# ============================================================ #
# COMPATIBILITY WRAPPER for app.py                              #
# ============================================================ #
def scrape_profile(profile_url, max_videos=50, scrolls=15, status=None):
    return run_for_url(profile_url, max_videos=max_videos, scrolls=scrolls, status=status)