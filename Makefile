.PHONY: test security core tier1 enrich tier3 lint clean init

test: security core tier1 enrich tier3

security:
	@python3 -I tests/test_security.py

core:
	@python3 -I tests/test_core.py

tier1:
	@python3 -I tests/test_tier1.py

enrich:
	@python3 -I tests/test_enrich.py

tier3:
	@python3 -I tests/test_tier3.py

init:
	@test -f .env || (cp .env.example .env && chmod 600 .env && echo "created .env (chmod 600)")
	@PYTHONPATH=. python3 -c "from core.config import load; print(load())"

clean:
	@find . -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
	@echo "cleaned caches (data/ left alone — use the purge command for records)"
