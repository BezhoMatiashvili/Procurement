#!/usr/bin/env bash
# One-time, ONLINE step: download every model the system needs, so that production can run
# fully offline (HF_HUB_OFFLINE=1). Run this on a machine with internet access and copy the
# resulting ./models directory (and the Ollama volume) to the air-gapped server.
set -euo pipefail
cd "$(dirname "$0")/.."

MODELS_DIR="${PA_DOCLING_ARTIFACTS_PATH:-./models/docling}"
mkdir -p "$MODELS_DIR"

echo ">> Docling models (layout, TableFormer) -> $MODELS_DIR"
HF_HUB_OFFLINE=0 uv run docling-tools models download --output-dir "$MODELS_DIR" layout tableformer

echo ">> Spelling dictionary (hunspell ka_GE, pinned and hash-checked) -> ${PA_SPELLING_DICTIONARY_PATH:-./models/hunspell}"
uv run python scripts/fetch_spelling_dictionary.py --dest "${PA_SPELLING_DICTIONARY_PATH:-./models/hunspell}"

echo ">> Ollama models"
LLM="${PA_LLM_MODEL:-gemma3:12b}"
FAST="${PA_LLM_FAST_MODEL:-gemma3:4b}"
if command -v ollama >/dev/null 2>&1; then
  ollama pull "$LLM"; ollama pull "$FAST"
elif docker ps --format '{{.Names}}' | grep -qx ollama; then
  docker exec ollama ollama pull "$LLM"; docker exec ollama ollama pull "$FAST"
else
  echo "   (no ollama found; pull $LLM and $FAST on the Ollama host manually)"
fi
echo ">> done"
