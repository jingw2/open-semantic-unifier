.PHONY: up down logs test dev-backend dev-frontend build-frontend

up:
	docker compose up -d --build

down:
	docker compose down

logs:
	docker compose logs -f backend worker

test:
	cd backend && ../../.kb03/bin/pytest -v tests/

dev-backend:
	cd backend && ../../.kb03/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload

dev-frontend:
	cd frontend && npm run dev

build-frontend:
	cd frontend && npm install && npx tsc --noEmit && npm run build
