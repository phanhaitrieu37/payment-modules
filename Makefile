MAKEFLAGS += --no-print-directory

.PHONY: sync lint format test-fast test acceptance build ci

sync:
	uv sync --all-extras --group dev

lint:
	uv run ruff check .
	uv run ruff format --check .

format:
	uv run ruff check --fix .
	uv run ruff format .

test-fast:
	uv run pytest -q tests/unit

test:
	uv run pytest -q -m "not acceptance"

# Exit code 5 means "no tests collected": tolerated until acceptance tests exist.
acceptance:
	uv run pytest -q -m acceptance || test $$? -eq 5

build:
	rm -rf dist
	uv build
	cd dist && shasum -a 256 *.whl *.tar.gz > SHA256SUMS

ci:
	$(MAKE) lint
	$(MAKE) test
	$(MAKE) acceptance
