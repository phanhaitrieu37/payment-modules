MAKEFLAGS += --no-print-directory

.PHONY: sync lint format test-fast test-integration test acceptance build ci

sync:
	uv sync --all-extras --group dev

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

test-fast:
	uv run pytest -q tests/unit tests/contract

test-integration:
	uv run pytest -q -m "integration and not acceptance"

test:
	uv run pytest -q -m "not acceptance"

acceptance:
	uv run pytest -q -m acceptance

build:
	rm -rf dist
	uv build
	cd dist && shasum -a 256 *.whl *.tar.gz > SHA256SUMS

ci:
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) acceptance
