.PHONY: test security core tier1 enrich lint clean init

test: security core tier1 enrich

security:
	@python3 -I tests/test_security.py

core:
	@python3 -I tests/test_core.py

tier1:
	@python3 -I tests/test_tier1.py

enrich:
	@python3 -I tests/test_enrich.py

init:
	@test -f .env || (cp .env.example .env && chmod 600 .env && echo "created .env (chmod 600)")
	@PYTHONPATH=. python3 -c "from core.config import load; print(load())"

clean:
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned caches (data/ left alone — use the purge command for records)"
