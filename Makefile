# HiveSync developer tasks.
#
# On Windows, run these from Git Bash or WSL. The recipes are POSIX sh and use
# inline environment assignment, which cmd.exe and PowerShell do not understand.
# Every recipe is a single command you can also paste directly into a shell.

SHELL := /bin/sh

include versions.env
export

PYTHON  ?= python
VERSION ?= 0.2.4

IMAGE_NAME ?= hivesync
LOCAL_IMAGE := $(IMAGE_NAME):$(VERSION)

# Docker Hub target. Override on the command line to publish elsewhere.
DOCKERHUB_NAMESPACE ?= geaves006
REGISTRY_IMAGE := docker.io/$(DOCKERHUB_NAMESPACE)/$(IMAGE_NAME)
PLATFORMS ?= linux/amd64,linux/arm64

# Local runs keep their state in ./config instead of /config.
DEV_ENV := HIVESYNC_CONFIG_DIR=./config

BUILD_ARGS := \
	--build-arg PYTHON_IMAGE=$(PYTHON_IMAGE) \
	--build-arg DEBIAN_IMAGE=$(DEBIAN_IMAGE) \
	--build-arg RCLONE_VERSION=$(RCLONE_VERSION) \
	--build-arg RCLONE_SHA256_AMD64=$(RCLONE_SHA256_AMD64) \
	--build-arg RCLONE_SHA256_ARM64=$(RCLONE_SHA256_ARM64) \
	--build-arg TAILWIND_VERSION=$(TAILWIND_VERSION) \
	--build-arg HTMX_VERSION=$(HTMX_VERSION) \
	--build-arg ALPINE_VERSION=$(ALPINE_VERSION)

.PHONY: help
help:
	@echo "install            install runtime and dev dependencies locally"
	@echo "dev                run locally with reload on http://localhost:8080"
	@echo "test               unit tests"
	@echo "test-integration   integration tests against the compose fixtures"
	@echo "lint               ruff and mypy"
	@echo "format             apply ruff formatting"
	@echo "assets             build Tailwind CSS and vendor htmx and Alpine"
	@echo "build              build the container image for this host"
	@echo "up / down / logs   docker compose lifecycle"
	@echo "push               build multi arch and push to Docker Hub"
	@echo "secret-key         print a fresh HIVESYNC_SECRET_KEY"
	@echo "pin-versions       report the tool versions inside the built image"
	@echo "pin-deps           print the resolved Python dependency set"
	@echo "migration          create a new Alembic revision, NAME=description"

.PHONY: install
install:
	$(PYTHON) -m pip install -e ".[dev]"

.PHONY: dev
dev:
	$(DEV_ENV) alembic upgrade head
	$(DEV_ENV) $(PYTHON) -m uvicorn app.main:create_app --factory --reload --port 8080

.PHONY: test
test:
	$(PYTHON) -m pytest -m "not integration"

# Runs inside the image, on the fixture network, so the tests use the pinned
# rclone rather than whatever is on the host PATH.
#
# The runner is a compose service now, rather than a `docker run` that had to
# know the network name compose generates. SPEC section 18, M8.
.PHONY: test-integration
test-integration:
	HIVESYNC_TEST_SECRET_KEY=$$($(PYTHON) -c "from cryptography.fernet import Fernet;print(Fernet.generate_key().decode())") \
		docker compose -f docker-compose.test.yml run --rm --build tests; \
		status=$$?; \
		docker compose -f docker-compose.test.yml down -v; \
		exit $$status

.PHONY: test-image
test-image: build
	docker build -f Dockerfile.test --build-arg BASE=$(IMAGE_NAME):latest -t $(IMAGE_NAME):test .

.PHONY: lint
lint:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m ruff format --check .
	$(PYTHON) -m mypy app

.PHONY: format
format:
	$(PYTHON) -m ruff format .
	$(PYTHON) -m ruff check --fix .

# Built inside the image so the pinned Tailwind, htmx and Alpine versions are the
# only ones in play, and so no toolchain is needed on the host.
.PHONY: assets
assets:
	docker build --target assets-export $(BUILD_ARGS) \
		--output type=local,dest=.assets-out .
	cp .assets-out/css/app.css app/web/static/css/app.css
	cp .assets-out/vendor/htmx.min.js app/web/static/vendor/htmx.min.js
	cp .assets-out/vendor/alpine.min.js app/web/static/vendor/alpine.min.js
	rm -rf .assets-out

.PHONY: build
build:
	docker build $(BUILD_ARGS) -t $(LOCAL_IMAGE) -t $(IMAGE_NAME):latest .

.PHONY: up
up:
	docker compose up --build -d

.PHONY: down
down:
	docker compose down

.PHONY: logs
logs:
	docker compose logs -f hivesync

# Requires `docker login` to have been run by you first, and a buildx builder
# that can produce both architectures.
.PHONY: push
push: guard-DOCKERHUB_NAMESPACE
	docker buildx build $(BUILD_ARGS) \
		--platform $(PLATFORMS) \
		-t $(REGISTRY_IMAGE):$(VERSION) \
		-t $(REGISTRY_IMAGE):latest \
		--push .

.PHONY: secret-key
secret-key:
	@$(PYTHON) -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Records what the image actually contains. lftp is not version pinned, so this
# is how its version gets into versions.env and CLAUDE.md.
.PHONY: pin-versions
pin-versions:
	@docker run --rm --entrypoint sh $(LOCAL_IMAGE) -c \
		'rclone version | head -n 1; lftp --version | head -n 1; python --version'

.PHONY: pin-deps
pin-deps:
	@docker run --rm --entrypoint sh $(LOCAL_IMAGE) -c 'pip freeze'

# Regenerate requirements.lock from requirements.txt, with a hash for every
# distribution. The image installs from the lock with --require-hashes, so this
# must be re-run after any dependency change or the build fails.
.PHONY: lock-deps
lock-deps:
	docker run --rm -v "$(CURDIR)":/src -w /src $(PYTHON_IMAGE) sh -c \
		"pip install -q pip-tools && pip-compile --generate-hashes \
		 --output-file=requirements.lock --quiet requirements.txt"

.PHONY: migration
migration: guard-NAME
	$(DEV_ENV) alembic revision --autogenerate -m "$(NAME)"

guard-%:
	@if [ -z "$($*)" ]; then \
		echo "Set $* first. For example: make $(MAKECMDGOALS) $*=value"; \
		exit 1; \
	fi
