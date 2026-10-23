"""Plain Selenium WebDriver (no Selenium Grid): the browser runs on the same machine as the tests.

- In Jenkins: headless Chromium installed in the Jenkins container (CHROME_BIN / CHROMEDRIVER env vars).
- On a laptop: your installed Google Chrome; Selenium Manager finds the matching driver automatically.
  Run with HEADLESS=false to watch the browser click through the site.
"""
import os
import time
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service

APP_URL = os.environ.get("APP_URL", "http://localhost:5050")
SHOTS = Path("reports/screenshots")
DEMO_PAUSE = float(os.environ.get("DEMO_PAUSE", "0"))   # seconds to hold each result on screen (live demo)


@pytest.fixture(scope="session")
def driver():
    opts = Options()
    if os.environ.get("HEADLESS", "true").lower() == "true":
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")   # small /dev/shm inside containers
    opts.add_argument("--window-size=1366,900")
    if os.environ.get("CHROME_BIN"):
        opts.binary_location = os.environ["CHROME_BIN"]
    service = Service(executable_path=os.environ["CHROMEDRIVER"]) if os.environ.get("CHROMEDRIVER") else Service()
    d = webdriver.Chrome(options=opts, service=service)
    d.implicitly_wait(5)
    yield d
    d.quit()


@pytest.fixture(autouse=True)
def screenshot(request, driver):
    yield
    if DEMO_PAUSE:
        time.sleep(DEMO_PAUSE)
    SHOTS.mkdir(parents=True, exist_ok=True)
    driver.save_screenshot(str(SHOTS / f"{request.node.name}.png"))
