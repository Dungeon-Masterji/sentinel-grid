#!/bin/bash
set -e

TARGET="/home/site/wwwroot/.python_packages/lib/site-packages"

echo "Installing dependencies to $TARGET..."
python -m pip install --target="$TARGET" --upgrade -r requirements.txt

echo "Starting uvicorn..."
export PYTHONPATH="$TARGET:$PYTHONPATH"
exec python -m uvicorn main:app --host 0.0.0.0 --port 8000
