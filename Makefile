.PHONY: init up doctor smoke backup setup-dev dev test docs-check

init:
	./scripts/bmas init

up:
	./scripts/bmas up

doctor:
	./scripts/bmas doctor

smoke:
	./scripts/bmas smoke

backup:
	./scripts/bmas backup

setup-dev:
	./scripts/bmas setup-dev

dev:
	./scripts/bmas dev

test:
	./scripts/bmas test

docs-check:
	./scripts/bmas docs-check
