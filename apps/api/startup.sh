#!/bin/bash
set -e

echo "Installing dependencies..."
pip install --target=".python_packages/lib/site-packages" -r requirements.txt

echo "Starting uvicorn..."
export PYTHONPATH="/home/site/wwwroot/.python_packages/lib/site-packages:$PYTHONPATH"
uvicorn main:app --host 0.0.0.0 --port 8000
