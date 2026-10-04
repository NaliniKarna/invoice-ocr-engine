.PHONY: install test test-real up down migrate
install: ; pip install -r requirements-dev.txt
test: ; OCR_API_KEY_PEPPER=$$(python -c "print('x'*40)") OCR_ADMIN_TOKEN=$$(python -c "print('y'*40)") pytest
up: ; docker compose up --build -d
down: ; docker compose down
migrate: ; alembic upgrade head
