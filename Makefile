.PHONY: setup dev check doctor services stop
setup:
	uv sync --python 3.12
	cd web && npm install
services:
	docker compose up -d --wait db search
dev: services
	python3 scripts/dev.py
check:
	uv run pytest backend/tests -q
	cd web && npm run build
doctor:
	python3 scripts/doctor.py
stop:
	docker compose stop
