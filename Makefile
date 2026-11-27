PYTHON ?= python3
REPOSITORY_WORKERS ?= 4
PUBLIC_TESTS := test_repository test_harness_tasks test_agent_review test_review_revision test_harness_decisions test_env_run test_setup_local

.PHONY: check inventory test skills

check:
	$(PYTHON) bin/repository.py check --workers $(REPOSITORY_WORKERS)
	$(MAKE) skills test PYTHON="$(PYTHON)"

inventory:
	$(PYTHON) bin/repository.py files --workers $(REPOSITORY_WORKERS)

skills:
	$(PYTHON) bin/check_skill_lock.py

test:
	PYTHONPATH=tests $(PYTHON) -m unittest $(PUBLIC_TESTS)
