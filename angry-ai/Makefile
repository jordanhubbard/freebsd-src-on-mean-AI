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
		git lfs install || true; \
		git clone https://huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct "$(MODEL)"; \
	else \
		echo "==> Model directory $(MODEL) already exists; skipping download."; \
	fi

show-config:
	@echo "=================================================="
	@echo " Angry FreeBSD AI Configuration:"
	@echo "--------------------------------------------------"
	@echo "  Repo       = $(REPO)"
	@echo "  Bootstrap  = $(BOOTSTRAP)"
	@echo "  Model      = $(MODEL)"
	@echo "  Tokens     = $(MAX_NEW_TOKENS)"
	@echo "  Temp       = $(TEMPERATURE)"
	@echo "  Max steps  = $(MAX_STEPS)"
	@echo "=================================================="

run: deps model show-config
	@echo "==> LETTING THE ANGRY AI LOOSE..."
	$(PYTHON_VENV) angry_ai.py \
		--repo "$(REPO)" \
		--bootstrap "$(BOOTSTRAP)" \
		--model "$(MODEL)" \
		--max-new-tokens $(MAX_NEW_TOKENS) \
		--temperature $(TEMPERATURE) \
		--max-steps $(MAX_STEPS)

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
