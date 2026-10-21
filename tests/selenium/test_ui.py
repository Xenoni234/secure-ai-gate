"""End-to-end browser tests on the deployed container via Selenium Grid."""
import re

import pytest
import requests
from selenium.common.exceptions import NoAlertPresentException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from tests.selenium.conftest import APP_URL

ANALYSIS_WAIT = 90   # seconds: Stockfish runs on every submission
XSS = "<script>alert('xss')</script>"


def load_sample(driver, name):
    driver.get(APP_URL)
    driver.find_element(By.ID, f"load-{name}").click()
    WebDriverWait(driver, 10).until(
        lambda d: d.find_element(By.ID, "pgn").get_attribute("value").strip()
    )


def analyse(driver, wait_for="verdict"):
    driver.find_element(By.ID, "analyze-btn").click()
    return WebDriverWait(driver, ANALYSIS_WAIT).until(
        EC.visibility_of_element_located((By.ID, wait_for))
    )


def test_homepage_loads(driver):
    driver.get(APP_URL)
    assert "Fair-Play Gate" in driver.title
    assert "not an automatic ban" in driver.page_source


def test_fair_sample_is_fair(driver):
    load_sample(driver, "fair")
    verdict = analyse(driver)
    assert "fair play" in verdict.text.lower()
    assert "suspicious" not in verdict.text.lower()


def test_suspicious_sample_is_flagged(driver):
    load_sample(driver, "suspicious")
    verdict = analyse(driver)
    assert "suspicious" in verdict.text.lower()


def test_xss_in_pgn_header_is_escaped(driver):
    load_sample(driver, "fair")
    box = driver.find_element(By.ID, "pgn")
    pgn = re.sub(r'^\[White ".*"\]$', lambda _: f'[White "{XSS}"]', box.get_attribute("value"),
                 count=1, flags=re.M)
    box.clear()
    box.send_keys(pgn)
    analyse(driver)
    try:
        driver.switch_to.alert
        pytest.fail("XSS executed: JavaScript alert appeared")
    except NoAlertPresentException:
        pass
    assert XSS in driver.find_element(By.ID, "player-white").text   # shown as plain text


def test_invalid_pgn_shows_error(driver):
    driver.get(APP_URL)
    driver.find_element(By.ID, "pgn").send_keys("1. e4 e5 2. Qh8 Nc6")
    error = analyse(driver, wait_for="error")
    assert "illegal" in error.text.lower()


def test_live_security_headers():
    r = requests.get(APP_URL, timeout=10)
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in r.headers
