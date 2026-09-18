PYTHON ?= python
DIST_DIR ?= dist

.PHONY: check syntax test release-check release release-verify dev-setup generate-icons generate-brand

check:
	$(PYTHON) tools/check_project.py

syntax:
	$(PYTHON) tools/check_project.py --skip-tests

test:
	$(PYTHON) -m unittest discover -s tests -p 'test_*.py' -v

release-check:
	$(PYTHON) tools/build_release.py --check-only
	$(PYTHON) tools/check_project.py --release

release:
	$(PYTHON) tools/build_release.py --dist "$(DIST_DIR)"

release-verify:
	$(PYTHON) tools/build_release.py --dist "$(DIST_DIR)" --verify-only

dev-setup:
	$(PYTHON) -m pip install -r requirements-dev.txt

generate-icons:
	$(PYTHON) tools/gen_icons.py

generate-brand:
	$(PYTHON) tools/gen_brand_assets.py
