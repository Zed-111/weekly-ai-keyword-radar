PYTHON ?= python3
VENV ?= .venv
BIN := $(VENV)/bin

.PHONY: setup fetch refine weekly weekly-no-llm validate test dry-run clean

setup:
	$(PYTHON) -m venv $(VENV)
	$(BIN)/python -m pip install --upgrade pip
	$(BIN)/pip install -r requirements.txt
	@echo "Setup done. Copy .env.example to .env and add your LLM key."

fetch:
	$(BIN)/python scripts/fetch.py --days 7 --verbose

refine:
	$(BIN)/python scripts/refine.py --input output/raw.json --feedback config/feedback.yaml --verbose

weekly:
	$(BIN)/python scripts/run_weekly.py --days 7 --archive --fallback-on-error

weekly-no-llm:
	$(BIN)/python scripts/run_weekly.py --days 7 --archive --no-llm

validate:
	$(BIN)/python scripts/validate.py --input output/weekly-keywords.json --schema references/output-schema.json

test:
	$(BIN)/python -m unittest discover -s tests -v

dry-run:
	$(BIN)/python scripts/refine.py --dry-run --input output/raw.json --feedback config/feedback.yaml

clean:
	rm -rf __pycache__ scripts/__pycache__ tests/__pycache__
