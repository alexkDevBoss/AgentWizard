# Personal AI Adventure Agent -- task runner.
#
# Every target routes through the project-local .venv, so you never have to
# remember whether it is activated. Works on Windows (Git Bash or cmd) and
# on macOS/Linux.
#
#   make setup            create .venv (py3.12) and install all requirements
#   make test             pytest
#   make lint             ruff check + format check
#   make fmt              ruff format (writes)
#   make synth            cdk synth        [ENV=dev|prod]
#   make diff             cdk diff         [ENV=dev|prod]
#   make deploy           cdk deploy       [ENV=dev|prod]  -- ASKS FIRST
#   make destroy          cdk destroy      [ENV=dev|prod]
#   make bootstrap        cdk bootstrap (once per account/region)
#   make clean            remove build artefacts (keeps .venv)

ENV     ?= dev
REGION  ?= us-east-1

ifeq ($(OS),Windows_NT)
  VENV_BIN  := .venv/Scripts
  PY        := $(VENV_BIN)/python.exe
  MKVENV    := py -3.12 -m venv .venv
  # The CDK CLI spawns the app through cmd.exe, which rejects forward
  # slashes in the executable position. Backslashes only, here.
  PY_APP    := .venv\Scripts\python.exe
else
  VENV_BIN  := .venv/bin
  PY        := $(VENV_BIN)/python
  MKVENV    := python3.12 -m venv .venv
  PY_APP    := $(PY)
endif

# --app overrides cdk.json so the venv interpreter is always the one used.
CDK_APP := --app "$(PY_APP) infra/app.py"
STACK   := AdventureAgent$(if $(filter prod,$(ENV)),Prod,Dev)
CDK     := cdk $(CDK_APP) --context env=$(ENV)

.PHONY: help setup test lint fmt synth diff deploy destroy bootstrap clean venv-check

help:
	@echo "targets: setup test lint fmt synth diff deploy destroy bootstrap clean   (ENV=dev|prod)"

setup:
	$(MKVENV)
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt -r requirements-infra.txt -r requirements-dev.txt
	@echo ""
	@echo "venv ready: $(PY)"

venv-check:
	@$(PY) -c "import sys; assert sys.version_info[:2]==(3,12), 'expected python 3.12, got %s' % sys.version; print('venv ok:', sys.version.split()[0])"

test: venv-check
	$(PY) -m pytest -q

lint: venv-check
	$(PY) -m ruff check .
	$(PY) -m ruff format --check .

fmt: venv-check
	$(PY) -m ruff format .
	$(PY) -m ruff check --fix .

synth: venv-check
	$(CDK) synth $(STACK)

diff: venv-check
	$(CDK) diff $(STACK)

# Deploy is never silent: it prints the target first and requires confirmation.
deploy: venv-check
	@echo "About to deploy $(STACK) to $(REGION). Ctrl-C to abort."
	$(CDK) deploy $(STACK)

destroy: venv-check
	$(CDK) destroy $(STACK)

bootstrap: venv-check
	$(CDK) bootstrap

clean:
	@$(PY) -c "import shutil;[shutil.rmtree(d,ignore_errors=True) for d in ('cdk.out','.pytest_cache','.ruff_cache')];print('cleaned')"
