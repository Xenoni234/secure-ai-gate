# Secure AI Gate – Working Commands (start, check, fix)

Run everything from the project folder in the VS Code terminal:

```bash
cd ~/Desktop/Secure-ai-gate
```

---

## 1. Ports used by this project

| Port | What | URL |
|---|---|---|
| **8080** | Jenkins (web UI) | http://localhost:8080/job/secure-ai-gate/ |
| **50000** | Jenkins agent port (internal, no page) | – |
| **5050** | Website – Docker staging app (`secure-ai-app`) | http://localhost:5050 |
| **8000** | Website – optional local run with gunicorn (no Docker) | http://localhost:8000 |
| **8025** | Mailpit – test inbox that shows every email Jenkins sends | http://localhost:8025 |
| **1025** | Mailpit SMTP (Jenkins sends mail here; no page) | – |

> Ports **4444 / 7900** (old Selenium Grid) are **no longer used** – Selenium now runs Chrome directly.
> Never use port **5000** (macOS AirPlay uses it) or **5060** (Safari blocks it).

---

## 2. Normal start (after restart / when everything is closed)

```bash
open -a Docker                                        # 1. start Docker Desktop, wait for the whale icon
docker compose -f jenkins/docker-compose.yml up -d    # 2. start Jenkins
docker ps                                             # 3. check what is running
open http://localhost:8080/job/secure-ai-gate/ http://localhost:5050   # 4. open Jenkins + website
```

If `secure-ai-app` is **not** in `docker ps`, either click **Build with Parameters → Build** in Jenkins
(the build deploys it), or start it by hand (see section 4).

---

## 3. Quick health checks

```bash
docker ps --format '{{.Names}}  {{.Status}}  {{.Ports}}'   # jenkins + secure-ai-app (healthy)
curl -s http://localhost:5050/health                       # {"engine":true,...,"status":"ok"}
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:8080/login   # 200 = Jenkins is up
docker images secure-ai-gate                               # one tag per build + stable
```

---

## 4. Website is down (http://localhost:5050 does not open)

```bash
docker ps -a --filter name=secure-ai-app        # is it stopped / unhealthy?
docker logs --tail 30 secure-ai-app              # see the error
docker restart secure-ai-app                     # try a simple restart first
```

Still down? Re-create it from the last good image:

```bash
docker rm -f secure-ai-app
docker network inspect ci-net >/dev/null 2>&1 || docker network create ci-net
IMAGE_TAG=stable API_KEY=demo-key docker compose up -d app
curl -s http://localhost:5050/health
```

Error *"container name /secure-ai-app is already in use"* → it is already running; just open the site.
If you really want a fresh one: `docker rm -f secure-ai-app` and run the `docker compose up` line again.

### Run the website without Docker (backup for the demo, port 8000)

```bash
source .venv/bin/activate
API_KEY=demo-key .venv/bin/gunicorn --bind 127.0.0.1:8000 --threads 4 --timeout 120 app.wsgi:app
# open http://localhost:8000   –   stop with Ctrl + C
```

---

## 5. Jenkins is down / stuck (http://localhost:8080)

```bash
docker logs --tail 30 jenkins
docker restart jenkins                                  # wait ~30 s, then reload the page
docker compose -f jenkins/docker-compose.yml up -d      # if the container is missing
```

Jenkins image changed (e.g. `jenkins/Dockerfile` edited) → rebuild:

```bash
docker compose -f jenkins/docker-compose.yml up -d --build
```

### A build is stuck

| Stuck at | Fix |
|---|---|
| **OWASP ZAP** | it auto-stops after 5 min; or now: `docker rm -f zap` |
| **Trivy** | `docker ps` → `docker rm -f <trivy container name>`; build continues as UNSTABLE |
| **Checkout fails** in a few seconds (`Could not resolve host: github.com`) | internet dropped – wait 10 s and click **Build** again |
| Build waiting forever | Jenkins → build → red **X** (abort), then **Build with Parameters** again |

Refresh the Trivy vulnerability database (when the network is good):

```bash
docker run --rm -v trivy-cache:/root/.cache/ aquasec/trivy:latest image --download-db-only --db-repository ghcr.io/aquasecurity/trivy-db:2
```

---

## 6. "Port already in use" – find and kill what is using a port

```bash
lsof -nP -iTCP:5050 -sTCP:LISTEN      # replace 5050 with 8080 / 8000 / 50000
```

- If the **COMMAND** column says `com.docke` / `docker` → it is a container; stop the container instead:
  ```bash
  docker ps --filter publish=5050       # find which container
  docker rm -f secure-ai-app            # or: docker restart jenkins (for 8080 / 50000)
  ```
- If it is another program (e.g. `Python`, `gunicorn`, `node`) → kill it by PID:
  ```bash
  kill <PID>          # polite stop
  kill -9 <PID>       # force stop if it does not quit
  ```
- Kill whatever is on a port in one line (only for non-Docker programs):
  ```bash
  lsof -ti tcp:8000 | xargs kill -9
  ```

| Port busy | Usually caused by | Kill / fix |
|---|---|---|
| 5050 | old `secure-ai-app` container | `docker rm -f secure-ai-app` |
| 8080 / 50000 | old Jenkins container | `docker restart jenkins` |
| 8000 | local gunicorn left running | `lsof -ti tcp:8000 \| xargs kill -9` |
| 5000 | macOS AirPlay Receiver | don't use 5000 (System Settings → AirDrop & Handoff → AirPlay Receiver off) |

---

## 7. Selenium UI tests (normal WebDriver, no Grid)

Watch Chrome test the website live on your Mac (site must be running on 5050):

```bash
HEADLESS=false DEMO_PAUSE=2 .venv/bin/python -m pytest tests/selenium -v
```

In Jenkins the same tests run headless in Chromium inside the Jenkins container (step 9).

All other tests:

```bash
.venv/bin/python -m pytest tests --ignore=tests/selenium -q      # 52 tests
```

---

## 8. Live demo: code change → GitHub → Jenkins → website

1. Edit something visible, e.g. `app/templates/index.html` line 39 (`<h1>Chess Fair-Play Gate …</h1>`), save.
2. Push it:
   ```bash
   git add app/templates/index.html && git commit -m "Live demo change" && git push origin main
   ```
   *"nothing to commit"* = the file was not changed/saved.
3. Jenkins starts a build within ~1 minute (or click **Build with Parameters → Build**).
4. Watch **Console Output**; after step 8 the website on http://localhost:5050 shows the change.
5. Show `docker images secure-ai-gate` – new tag + `stable`.

---

## 9. Email report after every build

After **every** build (pass, unstable or fail) Jenkins emails the person who pushed (taken from the git
commit) and whoever clicked **Build**. The email lists the pushed commits + changed files, the test table
(total / passed / failed / skipped), the names of failed tests, each security gate's result and the final
status. Configured in the job under **Post-build Actions → Editable Email Notification** (trigger: *Always*;
recipients: *Developers, Culprits, Requester*).

**Test inbox (works offline):** Jenkins sends to Mailpit → open http://localhost:8025

```bash
docker start mailpit       # if it is not running
docker run -d --name mailpit --network ci-net -p 8025:8025 -p 1025:1025 --restart unless-stopped axllent/mailpit   # first time only
```

**Manual "Build" emails you too:** Jenkins → your name (top right) → **Account** → *E-mail address* → Save.

**Switch to real Gmail** (so the email lands in the real inbox):
1. Google Account → Security → turn on **2-Step Verification** → **App passwords** → create one named `Jenkins` (16 letters).
2. Jenkins → **Manage Jenkins → System**:
   - *Jenkins Location → System Admin e-mail address*: your Gmail address
   - *Extended E-mail Notification*: SMTP server `smtp.gmail.com`, port `465`, **Advanced** →
     *Credentials → Add*: username = your Gmail, password = the app password; tick **Use SSL** → **Save**.
3. Click **Build with Parameters → Build** and check your Gmail.

To go back to the test inbox: SMTP server `mailpit`, port `1025`, no credentials, SSL off.

---

## 10. Clean up disk space (optional, when Docker gets slow)

```bash
docker image prune -f        # remove dangling <none> images
docker container prune -f    # remove stopped containers
```

> Do **not** run `docker volume prune` – it would delete Jenkins data (`jenkins_jenkins_home`) and the Trivy cache.
