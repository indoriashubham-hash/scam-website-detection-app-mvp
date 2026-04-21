.PHONY: up down logs api worker shell db-shell fmt lint test

up:
	docker compose up --build

down:
	docker compose down -v

logs:
	docker compose logs -f api worker

api:
	docker compose logs -f api

worker:
	docker compose logs -f worker

shell:
	docker compose exec api bash

db-shell:
	docker compose exec postgres psql -U wri -d wri

fmt:
	ruff format app tests
	ruff check --fix app tests

lint:
	ruff check app tests

test:
	docker compose exec api pytest -q
