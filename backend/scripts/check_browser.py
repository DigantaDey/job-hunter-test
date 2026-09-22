"""Offline deployment smoke test: verify the installed browser actually launches.

Run with the backend's Python environment (and runtime user):
    python backend/scripts/check_browser.py

No application settings, database, credentials, or external network are needed.
A missing Python package, browser download, or OS library fails the process.
"""
from playwright.sync_api import sync_playwright


def main() -> None:
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.set_content("<title>Browser smoke test</title><input name='candidate'>")
            page.locator("input").fill("Smoke test")
            assert page.title() == "Browser smoke test"
            assert page.locator("input").input_value() == "Smoke test"
            assert page.screenshot(), "Chromium must support screenshot capture"
        finally:
            browser.close()
    print("Python Playwright and Chromium are ready.")


if __name__ == "__main__":
    main()
