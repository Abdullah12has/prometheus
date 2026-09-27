.PHONY: setup dev check doctor services stop
setup:
	python3 scripts/setup.py
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
