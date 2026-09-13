import cv2
import numpy as np
import os
from playwright.sync_api import Page, Locator
# BoundingBox is not directly importable, it's a dict type returned by .bounding_box()

def solve_slide_captcha(
    page: Page,
    captcha_container_selector: str,
    slider_handle_selector: str,
    puzzle_piece_selector: str,
    output_dir: str = "captcha_screenshots"
) -> bool:
    """
    Solves a slide CAPTCHA by identifying the puzzle piece and its target slot
    using computer vision and simulating a drag action.

    Args:
        page: The Playwright Page object.
        captcha_container_selector: CSS selector for the main CAPTCHA container.
        slider_handle_selector: CSS selector for the draggable slider handle.
        puzzle_piece_selector: CSS selector for the visual puzzle piece that needs to move.
                               This is often a child or sibling of the slider handle.
        output_dir: Directory to save intermediate screenshots for debugging.

    Returns:
        True if the CAPTCHA was solved, False otherwise.
    """
    os.makedirs(output_dir, exist_ok=True)

    try:
        captcha_container = page.locator(captcha_container_selector)
        slider_handle = page.locator(slider_handle_selector)
        puzzle_piece = page.locator(puzzle_piece_selector)

        # Ensure elements are visible before attempting to interact or screenshot
        print(f"Waiting for CAPTCHA container '{captcha_container_selector}' to be visible...")
        captcha_container.wait_for(state='visible', timeout=5000)
        print(f"Waiting for slider handle '{slider_handle_selector}' to be visible...")
        slider_handle.wait_for(state='visible', timeout=5000)
        print(f"Waiting for puzzle piece '{puzzle_piece_selector}' to be visible...")
        puzzle_piece.wait_for(state='visible', timeout=5000)

        # 1. Capture screenshots of relevant elements
        captcha_container_path = os.path.join(output_dir, "captcha_container.png")
        puzzle_piece_path = os.path.join(output_dir, "puzzle_piece.png")

        print(f"Capturing screenshot of CAPTCHA container to {captcha_container_path}")
        captcha_container.screenshot(path=captcha_container_path)
        print(f"Capturing screenshot of puzzle piece to {puzzle_piece_path}")
        puzzle_piece.screenshot(path=puzzle_piece_path)

        # Get initial position of the slider handle
        initial_slider_bbox: dict = slider_handle.bounding_box() # Corrected type hint
        if not initial_slider_bbox:
            print("Error: Could not get bounding box for slider handle.")
            return False

        # Get bounding box of the CAPTCHA container for coordinate transformation
        captcha_container_bbox: dict = captcha_container.bounding_box() # Corrected type hint
        if not captcha_container_bbox:
            print("Error: Could not get bounding box for CAPTCHA container.")
            return False

        # 2. Load images with OpenCV
        full_captcha_img = cv2.imread(captcha_container_path)
        template_img = cv2.imread(puzzle_piece_path)

        if full_captcha_img is None:
            print(f"Error: Could not load full captcha image from {captcha_container_path}")
            return False
        if template_img is None:
            print(f"Error: Could not load puzzle piece image from {puzzle_piece_path}")
            return False

        # Convert to grayscale
        full_captcha_gray = cv2.cvtColor(full_captcha_img, cv2.COLOR_BGR2GRAY)
        template_gray = cv2.cvtColor(template_img, cv2.COLOR_BGR2GRAY)

        # Apply Canny edge detection for robustness
        full_captcha_edges = cv2.Canny(full_captcha_gray, 50, 150)
        template_edges = cv2.Canny(template_gray, 50, 150)

        # Save edges for debugging
        cv2.imwrite(os.path.join(output_dir, "captcha_edges.png"), full_captcha_edges)
        cv2.imwrite(os.path.join(output_dir, "template_edges.png"), template_edges)

        # 3. Perform template matching to find the target slot
        # We are looking for the best match of the puzzle piece's edges within the CAPTCHA container's edges
        print("Performing template matching...")
        result = cv2.matchTemplate(full_captcha_edges, template_edges, cv2.TM_CCOEFF_NORMED)
        min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(result)

        # The max_loc gives the top-left (x,y) of the best match of the template within the full image
        target_x_in_captcha_container = max_loc[0]

        # 4. Calculate the horizontal drag distance
        # Initial X of the slider handle relative to the CAPTCHA container
        initial_slider_x_relative_to_captcha = initial_slider_bbox['x'] - captcha_container_bbox['x']

        # The drag distance is the difference between the target X (where the puzzle piece should go)
        # and the initial X of the slider handle (which moves the puzzle piece).
        # We also need to add half the width of the template to align its center,
        # or adjust based on which edge we're aligning. For simplicity, let's align the top-left.
        drag_distance = target_x_in_captcha_container - initial_slider_x_relative_to_captcha

        # Add a small buffer/correction, common in real-world scenarios
        # This might need tuning based on the specific CAPTCHA
        drag_distance += 5 # Example: slight adjustment to ensure full fit

        print(f"Calculated drag distance: {drag_distance} pixels")

        # 5. Simulate the drag action with Playwright
        # Move mouse to the center of the slider handle
        print("Simulating mouse drag action...")
        page.mouse.move(
            initial_slider_bbox['x'] + initial_slider_bbox['width'] / 2,
            initial_slider_bbox['y'] + initial_slider_bbox['height'] / 2
        )
        page.mouse.down()
        # Drag horizontally by the calculated distance
        page.mouse.move(
            initial_slider_bbox['x'] + initial_slider_bbox['width'] / 2 + drag_distance,
            initial_slider_bbox['y'] + initial_slider_bbox['height'] / 2,
            steps=10 # Smooth the drag
        )
        page.mouse.up()

        print("Drag action completed. Waiting for verification...")
        page.wait_for_timeout(2000) # Give some time for the CAPTCHA to process

        # The calling function (app.py) should re-check verification status
        return True

    except Exception as e:
        print(f"An error occurred during CAPTCHA solving: {e}")
        return False