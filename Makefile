.PHONY: lint format format-check test check run-gateway run-prefect

lint:
	uv run ruff check src/ gateway/ flows/ tests/

format:
	uv run ruff format src/ gateway/ flows/ tests/

format-check:
	uv run ruff format --check src/ gateway/ flows/ tests/

test:
	uv run pytest

check: lint format-check test

run-gateway:
	uv run python -m gateway.server

run-prefect:
	uv run prefect server start
