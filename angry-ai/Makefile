# Angry FreeBSD AI Makefile
#
# Intended usage:
#   git clone https://github.com/jordanhubbard/freebsd-src-on-angry-AI.git
#   cd freebsd-src-on-angry-AI/angry-ai
#   make deps
#   make run
#
# This will:
#   - create a Python virtualenv in .venv
#   - install dependencies
#   - ensure the model exists locally (clones it if needed)
#   - run angry_ai.py against the parent repo (..),
#     using ../AI_START_HERE.md as the bootstrap prompt.

PYTHON      ?= python3
VENV_DIR    ?= .venv
PYTHON_VENV := $(VENV_DIR)/bin/python
PIP         := $(VENV_DIR)/bin/pip

# Defaults: repo root is parent directory, bootstrap is AI_START_HERE.md there.
REPO        ?= ..
BOOTSTRAP   ?= ../AI_START_HERE.md

# By default, we expect the model to live in this directory inside angry-ai/.
# If it does not exist, `make model` (or `make run`) will clone it from Hugging Face.
MODEL       ?= ./Qwen2.5-Coder-32B-Instruct

MAX_NEW_TOKENS ?= 512
TEMPERATURE    ?= 0.1
MAX_STEPS      ?= 100

# BUILD_VALIDATE - controls whether SSH validation is enabled
# Default: false (validation disabled by default since SSH command won't work on all hosts)
# Set to true to enable validation: make run BUILD_VALIDATE=true
BUILD_VALIDATE ?= false

# SSH validation command - runs after each mutagenic change to verify builds
# Only used if BUILD_VALIDATE=true. Empty string disables validation. Timeout is 5 minutes.
# The command is run with shell=True in Python, so shell operators work
ifeq ($(BUILD_VALIDATE),true)
SSH_VALIDATION_CMD ?= ssh freebsd.local 'cd Src/freebsd-src-on-angry-AI && git pull && make buildworld'
else
SSH_VALIDATION_CMD :=
endif

# Dependencies (non-torch)
DEPS = \
	transformers \
	accelerate \
	safetensors \
	sentencepiece \


# ----------------------------------------------------------------------
# Core Targets
# ----------------------------------------------------------------------

all: run

$(VENV_DIR):
	@echo "==> Creating virtualenv in $(VENV_DIR)"
	$(PYTHON) -m venv $(VENV_DIR)

venv: $(VENV_DIR)

deps: venv
	@echo "==> Upgrading pip"
	$(PIP) install --upgrade pip
	@echo "==> Detecting NVIDIA GPU..."
	@if nvidia-smi --query-gpu=name --format=csv,noheader >/dev/null 2>&1; then \
		echo "==> NVIDIA GPU detected! Installing CUDA-enabled PyTorch (nightly cu130)"; \
		$(PIP) install --pre torch torchvision --index-url https://download.pytorch.org/whl/nightly/cu130; \
	else \
		echo "==> No NVIDIA GPU detected, installing CPU-only PyTorch"; \
		$(PIP) install torch; \
	fi
	@echo "==> Installing remaining dependencies: $(DEPS)"
	$(PIP) install $(DEPS)

model:
	@if [ ! -d "$(MODEL)" ]; then \
		echo "==> Model directory $(MODEL) not found; cloning Qwen2.5-Coder-32B-Instruct"; \
		if ! command -v git-lfs >/dev/null 2>&1; then \
			echo "ERROR: git-lfs is not installed. Install it first:"; \
			echo "  macOS: brew install git-lfs"; \
			echo "  Ubuntu/Debian: apt-get install git-lfs"; \
			exit 1; \
		fi; \
		git lfs install || true; \
		git clone https://huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct "$(MODEL)"; \
	else \
		echo "==> Model directory $(MODEL) already exists; checking for incomplete download..."; \
		FIRST_MODEL="$$(find "$(MODEL)" -name 'model-*.safetensors' | head -1)"; \
		if [ -n "$$FIRST_MODEL" ] && [ "$$(stat -f %z "$$FIRST_MODEL" 2>/dev/null || stat -c %s "$$FIRST_MODEL" 2>/dev/null)" -lt 1000 ]; then \
			echo "ERROR: Model files appear to be Git LFS pointer files (too small)."; \
			echo "This means the actual model weights were not downloaded."; \
			echo ""; \
			echo "To fix this:"; \
			echo "  1. Install git-lfs if not already installed:"; \
			echo "       macOS: brew install git-lfs"; \
			echo "       Ubuntu/Debian: apt-get install git-lfs"; \
			echo "  2. Initialize git-lfs: git lfs install"; \
			echo "  3. Pull the actual files: cd $(MODEL) && git lfs pull"; \
			echo ""; \
			echo "Or simply remove $(MODEL) and run 'make model' again."; \
			exit 1; \
		fi; \
	fi

show-config:
	@echo "=================================================="
	@echo " Angry FreeBSD AI Configuration:"
	@echo "--------------------------------------------------"
	@echo "  Repo          = $(REPO)"
	@echo "  Bootstrap     = $(BOOTSTRAP)"
	@echo "  Model         = $(MODEL)"
	@echo "  Tokens        = $(MAX_NEW_TOKENS)"
	@echo "  Temp          = $(TEMPERATURE)"
	@echo "  Max steps     = $(MAX_STEPS)"
	@echo "  Build Validate= $(BUILD_VALIDATE)"
	@echo "  Validation    = $(if $(SSH_VALIDATION_CMD),$(SSH_VALIDATION_CMD),(disabled))"
	@echo "=================================================="

run: deps model show-config
	@echo "==> LETTING THE ANGRY AI LOOSE..."
	@$(PYTHON_VENV) angry_ai.py \
		--repo "$(REPO)" \
		--bootstrap "$(BOOTSTRAP)" \
		--model "$(MODEL)" \
		--max-new-tokens $(MAX_NEW_TOKENS) \
		--temperature $(TEMPERATURE) \
		--max-steps $(MAX_STEPS) \
		--ssh-validation-cmd "$(SSH_VALIDATION_CMD)"

# ----------------------------------------------------------------------
# Utility / Cleanup
# ----------------------------------------------------------------------

debug:
	@echo "$(PYTHON_VENV) angry_ai.py --repo $(REPO) --bootstrap $(BOOTSTRAP) --model $(MODEL)"

clean-venv:
	@echo "==> Removing virtualenv"
	rm -rf $(VENV_DIR)

clean-logs:
	rm -rf $(REPO)/.angry-ai/logs

.PHONY: all venv deps run debug clean-venv clean-logs show-config model
