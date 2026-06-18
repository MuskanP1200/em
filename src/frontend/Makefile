
.PHONY: install
install:
	pip install pip==25.0.1 &&\
	{ pip freeze | grep pip-tools > /dev/null || pip install pip-tools[stable] ; } &&\
	pip-compile --no-emit-index-url requirements/requirements.in &&\
	pip-sync requirements/requirements.txt

.PHONY: format
format:
	find ui -type f -name '*.py' -exec black {} +

.PHONY: format-check
format-check:
	find ui -type f -name '*.py' -exec black --check {} +

.PHONY: lint
lint:
	find ui -type f -name "*.py" -exec ruff check {} +

.PHONY: python-sast
python-sast:
	find ui -type f -name "*.py" -exec bandit {} +

.PHONY: refactor
refactor: lint format