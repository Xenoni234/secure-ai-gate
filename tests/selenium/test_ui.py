"""End-to-end browser tests on the deployed container via Selenium Grid."""
import pytest
import requests
from selenium.common.exceptions import NoAlertPresentException
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from tests.selenium.conftest import APP_URL


def load_sample(driver):
    driver.get(APP_URL)
    driver.find_element(By.ID, "load-sample").click()
    WebDriverWait(driver, 10).until(
        lambda d: d.find_element(By.ID, "features").get_attribute("value").strip()
    )


def test_homepage_loads(driver):
    driver.get(APP_URL)
    assert "Secure AI Gate" in driver.title


def test_prediction_flow(driver):
    load_sample(driver)
    driver.find_element(By.ID, "patient_ref").send_keys("PT-001")
    driver.find_element(By.ID, "predict-btn").click()
    result = WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.ID, "result")))
    assert "malignant" in result.text.lower() or "benign" in result.text.lower()


def test_xss_payload_is_escaped(driver):
    payload = "<script>alert('xss')</script>"
    load_sample(driver)
    driver.find_element(By.ID, "patient_ref").send_keys(payload)
    driver.find_element(By.ID, "predict-btn").click()
    WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.ID, "result")))
    try:
        driver.switch_to.alert
        pytest.fail("XSS executed: JavaScript alert appeared")
    except NoAlertPresentException:
        pass
    assert payload in driver.find_element(By.ID, "echo-ref").text   # shown as plain text


def test_invalid_input_shows_error(driver):
    driver.get(APP_URL)
    driver.find_element(By.ID, "features").send_keys("abc, def")
    driver.find_element(By.ID, "predict-btn").click()
    error = WebDriverWait(driver, 10).until(EC.visibility_of_element_located((By.ID, "error")))
    assert "number" in error.text.lower()


def test_live_security_headers():
    r = requests.get(APP_URL, timeout=10)
    assert r.headers.get("X-Frame-Options") == "DENY"
    assert "Content-Security-Policy" in r.headers