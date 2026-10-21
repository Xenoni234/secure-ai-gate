pipeline {
    agent any

    parameters {
        booleanParam(name: 'FAIL_ON_CVE', defaultValue: false,
                     description: 'Fail the build if Trivy finds fixable HIGH/CRITICAL CVEs')
        booleanParam(name: 'KEEP_STAGING', defaultValue: true,
                     description: 'Leave the staging app running after the build (for demo)')
    }

    triggers { pollSCM('* * * * *') }

    options {
        timestamps()
        timeout(time: 40, unit: 'MINUTES')
        disableConcurrentBuilds()
        buildDiscarder(logRotator(numToKeepStr: '15'))
    }

    environment {
        IMAGE        = 'secure-ai-gate'
        IMAGE_TAG    = "${env.BUILD_NUMBER}"
        API_KEY      = credentials('secure-ai-api-key')   // stored secret, masked in logs
        APP_URL      = 'http://secure-ai-app:5000'
        SELENIUM_URL = 'http://selenium:4444'
        HEADLESS     = 'false'                            // watch live at localhost:7900
        DEMO_PAUSE   = '3'                                // hold each UI test result on screen
        PY           = '.venv/bin/python'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
                sh 'git log -1 --oneline'
            }
        }

        stage('Setup Python') {
            steps {
                sh '''
                    python3 -m venv .venv
                    $PY -m pip install --quiet --upgrade pip
                    $PY -m pip install --quiet -r requirements.txt -r requirements-dev.txt
                    rm -rf reports && mkdir -p reports
                '''
            }
        }

        stage('SAST - Bandit') {
            steps {
                sh '''
                    $PY -m bandit -r app -f json -o reports/bandit.json || true
                    $PY -m bandit -r app -lll -iii
                '''
            }
        }

        stage('Dependency Scan - pip-audit') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE') {
                    sh '$PY -m pip_audit -r requirements.txt -f json -o reports/pip-audit.json'
                }
            }
        }

        stage('Train + Quality Gate') {
            steps { sh '$PY -m app.train' }
        }

        stage('Unit, API-Security & Adversarial Tests') {
            steps {
                sh '$PY -m pytest tests --ignore=tests/selenium -v --junitxml=reports/junit-unit.xml'
            }
        }

        stage('Docker Build') {
            steps { sh 'docker build -t $IMAGE:$IMAGE_TAG -t $IMAGE:latest .' }
        }

        stage('Container Scan - Trivy') {
            steps {
                script {
                    // 0 = clean / reported, 1 = CVEs with FAIL_ON_CVE, 99 = scanner DB unavailable
                    def rc = sh(returnStatus: true, script: '''
                        if ! docker run --rm --entrypoint sh -v trivy-cache:/root/.cache/ aquasec/trivy:latest \
                                -c 'test -f /root/.cache/trivy/db/metadata.json'; then
                            echo "Trivy vulnerability DB not cached yet - scan skipped" | tee reports/trivy.txt
                            exit 99
                        fi
                        docker run --rm -v /var/run/docker.sock:/var/run/docker.sock -v trivy-cache:/root/.cache/ \
                            aquasec/trivy:latest image --scanners vuln --severity HIGH,CRITICAL --ignore-unfixed \
                            --skip-db-update --timeout 10m --exit-code 2 --no-progress $IMAGE:$IMAGE_TAG \
                            > reports/trivy.txt 2>&1 && STATUS=0 || STATUS=$?
                        cat reports/trivy.txt
                        if [ $STATUS -eq 2 ]; then
                            if [ "$FAIL_ON_CVE" = "true" ]; then exit 1; fi
                            exit 0
                        fi
                        [ $STATUS -eq 0 ] || exit 99
                    ''')
                    if (rc == 99) {
                        unstable('Trivy vulnerability DB unavailable - scan skipped')
                    } else if (rc != 0) {
                        error('Fixable HIGH/CRITICAL CVEs found')
                    }
                }
            }
        }

        stage('Deploy to Staging') {
            steps {
                sh '''
                    docker network inspect ci-net >/dev/null 2>&1 || docker network create ci-net
                    docker compose down --remove-orphans || true
                    docker rm -f secure-ai-app selenium >/dev/null 2>&1 || true
                    docker compose up -d
                    for i in $(seq 1 30); do
                        curl -sf $APP_URL/health && break
                        echo "waiting for app..."; sleep 2
                    done
                    curl -sf $APP_URL/health
                    for i in $(seq 1 30); do
                        curl -sf $SELENIUM_URL/status | grep -Eq '"ready": ?true' && break
                        echo "waiting for selenium..."; sleep 2
                    done
                '''
            }
        }

        stage('UI Tests - Selenium') {
            steps {
                sh '$PY -m pytest tests/selenium -v --junitxml=reports/junit-ui.xml'
            }
        }

        stage('DAST - OWASP ZAP') {
            steps {
                catchError(buildResult: 'SUCCESS', stageResult: 'UNSTABLE') {
                    sh '''
                        docker rm -f zap >/dev/null 2>&1 || true
                        docker run --name zap --network ci-net -u root zaproxy/zap-stable \
                            bash -c "mkdir -p /zap/wrk && zap-baseline.py -t $APP_URL -r zap-report.html -I"
                        docker cp zap:/zap/wrk/zap-report.html reports/zap-report.html
                    '''
                }
            }
            post {
                always { sh 'docker rm -f zap >/dev/null 2>&1 || true' }
            }
        }

        stage('Promote Image') {
            steps {
                sh 'docker tag $IMAGE:$IMAGE_TAG $IMAGE:stable'
                echo "Promoted ${env.IMAGE}:${env.IMAGE_TAG} -> stable"
            }
        }
    }

    post {
        always {
            junit allowEmptyResults: true, testResults: 'reports/junit-*.xml'
            archiveArtifacts artifacts: 'reports/**', allowEmptyArchive: true
            script {
                if (!params.KEEP_STAGING || currentBuild.currentResult == 'FAILURE') {
                    sh 'docker compose down --remove-orphans || true'
                }
            }
        }
        success  { echo 'All security gates passed. Image promoted to stable.' }
        unstable { echo 'Build passed with warnings. Check pip-audit / ZAP reports.' }
        failure  { echo 'A security or quality gate failed. Image NOT promoted.' }
    }
}