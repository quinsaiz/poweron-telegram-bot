#!/bin/sh

set -eu

exec uvicorn src.main:app \
  --host 0.0.0.0 \
  --port "${PORT:-9999}"
