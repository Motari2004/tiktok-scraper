
from flask import Flask, render_template, request, jsonify, send_file
from scraper import scrape_profile

import os
import json
import threading


# ============================================================
# FLASK APP
# ============================================================

app = Flask(__name__)

RESULTS_DIR = "results"

os.makedirs(RESULTS_DIR, exist_ok=True)


# ============================================================
# GLOBAL SCRAPER STATUS
# ============================================================

status = {
    "running": False,
    "message": "Ready",
    "found": 0,
    "videos": []
}


# ============================================================
# NORMALIZE TIKTOK PROFILE
# ============================================================

def normalize_profile_url(value):

    value = value.strip()

    if not value:
        return None

    # Already a URL
    if value.startswith("http://") or value.startswith("https://"):

        if "tiktok.com" not in value.lower():
            return None

        return value

    # Username entered with @
    value = value.lstrip("@").strip()

    # Convert username to profile URL
    return f"https://www.tiktok.com/@{value}"


# ============================================================
# RUN SCRAPER IN BACKGROUND
# ============================================================

def run_scraper(profile_url, max_videos):

    global status

    status["running"] = True
    status["message"] = "Starting browser..."
    status["found"] = 0
    status["videos"] = []

    try:

        print()
        print("=" * 60)
        print("Starting TikTok scraper")
        print("=" * 60)
        print(f"Profile: {profile_url}")
        print(f"Maximum videos: {max_videos}")
        print()

        videos = scrape_profile(
            profile_url=profile_url,
            max_videos=max_videos,
            scrolls=15,
            status=status
        )

        # Remove duplicates while preserving order
        videos = list(dict.fromkeys(videos))

        # Limit results
        videos = videos[:max_videos]

        status["videos"] = videos
        status["found"] = len(videos)

        # ====================================================
        # SAVE JSON
        # ====================================================

        json_path = os.path.join(
            RESULTS_DIR,
            "videos.json"
        )

        with open(
            json_path,
            "w",
            encoding="utf-8"
        ) as file:

            json.dump(
                videos,
                file,
                indent=2,
                ensure_ascii=False
            )

        # ====================================================
        # SAVE TXT
        # ====================================================

        txt_path = os.path.join(
            RESULTS_DIR,
            "videos.txt"
        )

        with open(
            txt_path,
            "w",
            encoding="utf-8"
        ) as file:

            for video_url in videos:
                file.write(video_url + "\n")

        status["message"] = (
            f"Done — found {len(videos)} video URLs."
        )

        print()
        print("=" * 60)
        print(f"Done — found {len(videos)} video URLs.")
        print("=" * 60)
        print()

    except Exception as error:

        status["message"] = f"Error: {str(error)}"

        print()
        print("SCRAPER ERROR")
        print(error)
        print()

    finally:

        status["running"] = False


# ============================================================
# HOME PAGE
# ============================================================

@app.route("/")
def index():

    return render_template(
        "index.html"
    )


# ============================================================
# START SCRAPER
# ============================================================

@app.route(
    "/scrape",
    methods=["POST"]
)
def scrape():

    global status

    # Prevent multiple browsers at once
    if status["running"]:

        return jsonify({
            "success": False,
            "message": "A scraper is already running."
        }), 400

    # Read JSON
    data = request.get_json(
        silent=True
    ) or {}

    # Get profile
    profile_input = data.get(
        "profile_url",
        ""
    ).strip()

    # Get maximum videos
    max_videos_input = data.get(
        "max_videos",
        50
    )

    # ========================================================
    # VALIDATE PROFILE
    # ========================================================

    if not profile_input:

        return jsonify({
            "success": False,
            "message": (
                "Enter a TikTok username "
                "or profile URL."
            )
        }), 400

    # Automatically convert username
    profile_url = normalize_profile_url(
        profile_input
    )

    if not profile_url:

        return jsonify({
            "success": False,
            "message": (
                "Invalid TikTok profile. "
                "Enter something like "
                "khaby.lame or "
                "https://www.tiktok.com/@khaby.lame"
            )
        }), 400

    # ========================================================
    # VALIDATE NUMBER
    # ========================================================

    try:

        max_videos = int(
            max_videos_input
        )

    except (
        ValueError,
        TypeError
    ):

        return jsonify({
            "success": False,
            "message": "Maximum videos must be a number."
        }), 400

    if max_videos < 1:

        return jsonify({
            "success": False,
            "message": (
                "Maximum videos must be at least 1."
            )
        }), 400

    # Optional safety limit
    if max_videos > 1000:

        max_videos = 1000

    # ========================================================
    # RESET STATUS
    # ========================================================

    status["running"] = True
    status["message"] = "Starting scraper..."
    status["found"] = 0
    status["videos"] = []

    # ========================================================
    # START BACKGROUND THREAD
    # ========================================================

    thread = threading.Thread(
        target=run_scraper,
        args=(
            profile_url,
            max_videos
        ),
        daemon=True
    )

    thread.start()

    # ========================================================
    # RESPONSE
    # ========================================================

    return jsonify({
        "success": True,
        "message": (
            f"Scraper started for "
            f"{profile_url}"
        ),
        "profile_url": profile_url
    })


# ============================================================
# SCRAPER STATUS
# ============================================================

@app.route("/status")
def get_status():

    return jsonify({
        "running": status["running"],
        "message": status["message"],
        "found": status["found"],
        "videos": status["videos"]
    })


# ============================================================
# DOWNLOAD RESULTS
# ============================================================

@app.route(
    "/download/<filename>"
)
def download(filename):

    # Only allow these files
    allowed_files = {
        "videos.json",
        "videos.txt"
    }

    if filename not in allowed_files:

        return (
            "File not found",
            404
        )

    file_path = os.path.join(
        RESULTS_DIR,
        filename
    )

    if not os.path.exists(file_path):

        return (
            "File not found",
            404
        )

    return send_file(
        file_path,
        as_attachment=True,
        download_name=filename
    )


# ============================================================
# CLEAR RESULTS
# ============================================================

@app.route(
    "/clear",
    methods=["POST"]
)
def clear_results():

    global status

    if status["running"]:

        return jsonify({
            "success": False,
            "message": (
                "Cannot clear results "
                "while scraper is running."
            )
        }), 400

    status["videos"] = []
    status["found"] = 0
    status["message"] = "Ready"

    # Delete JSON
    json_path = os.path.join(
        RESULTS_DIR,
        "videos.json"
    )

    if os.path.exists(json_path):
        os.remove(json_path)

    # Delete TXT
    txt_path = os.path.join(
        RESULTS_DIR,
        "videos.txt"
    )

    if os.path.exists(txt_path):
        os.remove(txt_path)

    return jsonify({
        "success": True,
        "message": "Results cleared."
    })


# ============================================================
# SERVER
# ============================================================

if __name__ == "__main__":

    print()
    print("=" * 60)
    print("           TikTok Profile Scraper")
    print("=" * 60)
    print()
    print("Server: http://127.0.0.1:5000")
    print("Network: http://0.0.0.0:5000")
    print()
    print("Enter a username such as:")
    print("  khaby.lame")
    print()
    print("or:")
    print("  @khaby.lame")
    print()
    print("or a full URL:")
    print("  https://www.tiktok.com/@khaby.lame")
    print()
    print("=" * 60)
    print()

    app.run(
        host="0.0.0.0",
        port=5000,
        debug=False,
        threaded=True
    )

