#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONPATH="$PWD/.vendor${PYTHONPATH:+:$PYTHONPATH}"
if [ ! -d .vendor/fastapi ]; then
  echo "Dependências não encontradas. Rode: python3 get-pip.py --target .vendor && PYTHONPATH=.vendor python3 -m pip install --target .vendor -r requirements.txt"
  exit 1
fi
python3 -m uvicorn app.main:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8765}" --reload
