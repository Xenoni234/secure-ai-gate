import os
from pathlib import Path

import pytest
from selenium import webdriver
from selenium.webdriver.chrome.options import Options

APP_URL = os.environ.get("APP_URL", "http://localhost:5000")
SELENIUM_URL = os.environ.get("SELENIUM_URL", "http://localhost:4444")
SHOTS = Path("reports/screenshots")


@pytest.fixture(scope="session")
def driver():
    opts = Options()
    if os.environ.get("HEADLESS", "true").lower() == "true":
        opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--window-size=1366,900")
    d = webdriver.Remote(command_executor=SELENIUM_URL, options=opts)
    d.implicitly_wait(5)
    yield d
    d.quit()


@pytest.fixture(autouse=True)
def screenshot(request, driver):
    yield
    SHOTS.mkdir(parents=True, exist_ok=True)
    driver.save_screenshot(str(SHOTS / f"{request.node.name}.png"))