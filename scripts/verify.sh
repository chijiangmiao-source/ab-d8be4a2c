#!/bin/sh
# verify container entry point:
#   1) rule tests (legal overlap window / cooling gap / retransmission)
#   2) HTTP smoke against the running web service
# Exits non-zero (reported by `docker compose ps` / exit code) on any failure.
set -eu

echo "== [verify] stage 1: rule tests =="
python3 -m unittest tests.test_rules -v

echo "== [verify] stage 2: HTTP smoke against http://${WEB_HOST:-web}:${WEB_PORT:-8000} =="
SMOKE_BASE="http://${WEB_HOST:-web}:${WEB_PORT:-8000}" python3 tests/http_smoke.py

echo "== [verify] ALL STAGES PASSED =="
