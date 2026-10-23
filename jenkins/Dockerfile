FROM jenkins/jenkins:lts-jdk17
USER root
RUN apt-get update && apt-get install -y --no-install-recommends \
        python3 python3-venv python3-pip curl ca-certificates gnupg git stockfish \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && chmod a+r /etc/apt/keyrings/docker.asc \
 && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*
# Browser + driver for the Selenium UI tests (plain WebDriver, no Selenium Grid)
RUN apt-get update && apt-get install -y --no-install-recommends chromium chromium-driver fonts-liberation \
 && rm -rf /var/lib/apt/lists/*
# Lets the engine-marked pytest tests and the Selenium tests run inside Jenkins
ENV STOCKFISH_PATH=/usr/games/stockfish \
    CHROME_BIN=/usr/bin/chromium \
    CHROMEDRIVER=/usr/bin/chromedriver
USER jenkins
