.PHONY: test build integration
test:
	python3 -m unittest discover -s tests_v6 -p 'test_*.py' -v

build:
	python3 tools/build_v6.py

# The supervisor creates a separate Unix UID; it always cleans it in finally.
integration:
	sudo -n python3 tools/test_v6.py
