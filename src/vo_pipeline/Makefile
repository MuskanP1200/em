
.PHONY: install
install:
	pip install pip==25.0.1 &&\
	{ pip freeze | grep pip-tools > /dev/null || pip install pip-tools[stable] ; } &&\
	pip-compile --no-emit-index-url requirements/requirements.in &&\
	pip-sync requirements/requirements.txt

.PHONY: format
format:
	@echo "No python file to lint yet" 
# 	find api_data -type f -name '*.py' -exec black {} +
# 	find discount_matching -type f -name '*.py' -exec black {} +
# 	find sample_data -type f -name '*.py' -exec black {} +
# 	find veh_ind_pipeline -type f -name '*.py' -exec black {} +

.PHONY: format-check
format-check:
	@echo "No python file to lint yet" 
# 	find api_data -type f -name '*.py' -exec black --check {} +
# 	find discount_matching -type f -name '*.py' -exec black --check {} +
# 	find sample_data -type f -name '*.py' -exec black --check {} +
# 	find veh_ind_pipeline -type f -name '*.py' -exec black --check {} +

.PHONY: lint
lint:
	@echo "No python file to lint yet" 
# 	find api_data -type f -name "*.py" -exec ruff check {} +
# 	find discount_matching -type f -name "*.py" -exec ruff check {} +
# 	find sample_data -type f -name "*.py" -exec ruff check {} +
# 	find veh_ind_pipeline -type f -name "*.py" -exec ruff check {} +

.PHONY: python-sast
python-sast:
	@echo "No python file to lint yet" 
# 	find api_data -type f -name "*.py" -exec bandit {} +
# 	find discount_matching -type f -name "*.py" -exec bandit {} +
# 	find sample_data -type f -name "*.py" -exec bandit {} +
# 	find veh_ind_pipeline -type f -name "*.py" -exec bandit {} +

.PHONY: refactor
refactor: lint format