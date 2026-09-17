#!/bin/sh
set -eu

# Railway's custom Start Command may invoke /run.sh directly.
# Keep this wrapper intentionally tiny so both Docker and Railpack deployments
# use the same application entrypoint.
exec uvicorn main:app --host 0.0.0.0 --port "${PORT:-8080}"
