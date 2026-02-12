#!/bin/bash

set -e

mkdir -p /app/data

exec uvicorn src.main:app --host 0.0.0.0 --port 8000