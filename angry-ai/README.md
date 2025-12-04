# Angry FreeBSD AI

This directory contains a self-contained "angry AI" wrapper that can
run locally against the `freebsd-src-on-angry-AI` repository using a
local Hugging Face model (CPU-only or GPU-accelerated).

## Quick Start

```sh
git clone https://github.com/jordanhubbard/freebsd-src-on-angry-AI.git
cd freebsd-src-on-angry-AI/angry-ai

# Create .venv and install Python dependencies
make deps

# Download the default model (Qwen2.5-Coder-32B-Instruct) if needed
# Note: you will need git lfs installed to download this large file!
make model

# Run the angry AI
make run
```

### VERY IMPORTANT NOTE for GPU users

If you install everything with `make deps` it will pull a CPU torch wheel. To accelerate using your GPU:

```sh
# 1) Enter the venv
. .venv/bin/activate

# 2) Remove the CPU-only torch
pip uninstall -y torch

# 3) Install a CUDA-enabled torch build
#    Pick the cuXXX closest to your CUDA version from nvidia-smi.
#    For a CUDA 13.0 driver, cu124 is the nearest "official" one today.
pip install --index-url https://download.pytorch.org/whl/cu124 torch

# 4) (Optional) re-install other deps if needed, but they should already be there:
pip install transformers accelerate safetensors sentencepiece

# 5) Run the angry AI
make run
```

By default, the script:

- Treats the parent directory (`..`) as the FreeBSD repo root.
- Uses `../AI_START_HERE.md` as the bootstrap instructions.
- Expects the model to live in `angry-ai/Qwen2.5-Coder-32B-Instruct`.
- Logs all model replies and tool activity under `.angry-ai/logs` in the repo.

If the model directory does not exist, `make model` (or `make run`) will:

```sh
git lfs install
git clone https://huggingface.co/Qwen/Qwen2.5-Coder-32B-Instruct Qwen2.5-Coder-32B-Instruct
```

so you get a working out-of-the-box setup in the `angry-ai/` directory.

On FreeBSD, you may need to install `git` and `git-lfs` first, for example:

```sh
pkg install git git-lfs
```

## CPU vs GPU

This works on:

- **CPU-only machines:** it will be slow, but it will run.
- **GPU-equipped machines:** if you install a CUDA-enabled PyTorch build, it
  can use your NVIDIA GPU (or Apple MPS on Apple Silicon).

The script prints an environment summary at startup, for example:

```text
=== Angry AI Environment Summary ===
torch.__version__        = 2.9.1+cpu
torch.cuda.is_available()= False
nvidia-smi detected:
  NVIDIA GB10, 580.95.05, 13.0
Hint: You have an NVIDIA GPU. If torch.cuda.is_available() is False,
you probably installed a CPU-only torch wheel. Consider reinstalling
a CUDA-enabled wheel from the official PyTorch index, for example:

  pip uninstall -y torch
  pip install --index-url https://download.pytorch.org/whl/cuXXX torch

where 'cuXXX' matches (or is close to) the CUDA version reported above.
====================================
```

### CPU-only: do nothing special

If you are happy to run on CPU:

```sh
cd freebsd-src-on-angry-AI/angry-ai
make deps
make run
```

`make deps` will install a CPU-build of `torch` plus the other dependencies
(`transformers`, `accelerate`, `safetensors`, `sentencepiece`).

### GPU users (NVIDIA / CUDA)

If you see `nvidia-smi` output and `torch.cuda.is_available() = False`, you
likely have a CPU-only PyTorch wheel. You can switch to a CUDA wheel roughly
like this (example for CUDA 12.4; adjust as needed):

```sh
cd freebsd-src-on-angry-AI/angry-ai
. .venv/bin/activate
pip uninstall -y torch

# Example: CUDA 12.4
pip install --index-url https://download.pytorch.org/whl/cu124 torch

# Reinstall the remaining dependencies if necessary
pip install transformers accelerate safetensors sentencepiece
```

Then run:

```sh
make run
```

On startup you should now see `torch.cuda.is_available() = True` and a
listing of your GPU(s).

### Apple Silicon / MPS

If you are on macOS with Apple Silicon and a recent PyTorch build,
the script will also detect and note `torch.backends.mps.is_available()`.

In that case, PyTorch will use the MPS backend by default.

## How It Works (High Level)

- The repo’s `AI_START_HERE.md` contains the actual persona, goals, and
  rolling TODO list behavior. That file is **not** modified by this tool.
- `angry_ai.py` is a thin **ReAct-style wrapper** around a local HF model:
  - It feeds a small system prompt that explains the ACTION protocol.
  - It then feeds the contents of `AI_START_HERE.md` as the initial user
    message.
  - The model responds with natural language plus exactly one line starting
    with `ACTION: ...`.
  - Valid ACTIONs are:

    - `ACTION: READ_FILE relative/path/to/file`
    - `ACTION: LIST_DIR relative/path/to/dir`
    - `ACTION: APPLY_PATCH` (followed by a unified diff)
    - `ACTION: HALT`

  - `angry_ai.py` executes the requested ACTION (reading files, listing
    directories, applying patches via `patch(1)`), prints what it did, and
    feeds the results back to the model.
  - This loop continues until the model emits `ACTION: HALT` or a `max_steps`
    limit is reached.

All model replies and tool results are logged under `.angry-ai/logs/` (from the repo root).

## Tuning

In the `Makefile` you can adjust the defaults:

- `MAX_NEW_TOKENS` – how verbose each model response can be (default: 512).
- `TEMPERATURE` – randomness (default: 0.1).
- `MAX_STEPS` – maximum number of agent steps per run (default: 100).

You can also override these on the command line:

```sh
make run MAX_NEW_TOKENS=256 MAX_STEPS=20
```

If you want to point at a different model directory than the default
`./Qwen2.5-Coder-32B-Instruct`, you can set `MODEL` explicitly:

```sh
make run MODEL=/some/other/model/path
```

In that case, `make model` will still try to clone into that path if it
does not yet exist.

## Choosing a Model

You can use any reasonably capable code model that:

- is available as a Hugging Face Transformers checkpoint, and
- fits in your available CPU/GPU memory.

Examples (you must download these yourself, unless you rely on `make model`
with the default Qwen2.5 32B path):

- `Qwen/Qwen2.5-Coder-7B-Instruct`
- `Qwen/Qwen2.5-Coder-32B-Instruct`
- your own fine-tuned coding model

Point the `MODEL` variable at the directory where you cloned or downloaded
the model, e.g.:

```sh
git lfs install
git clone https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct /srv/models/Qwen2.5-Coder-7B-Instruct

make run MODEL=/srv/models/Qwen2.5-Coder-7B-Instruct
```

## Safety and Sanity

- This tool will apply patches to your working tree using `patch(1)`.
  Run it on a branch, and use `git status` / `git diff` to inspect changes.
- Logs are stored under `.angry-ai/logs` so you can inspect exactly what
  the model said and what patches it attempted to apply.
- If things go sideways, you can always reset your branch and try again
  with different parameters or a different model.

Have fun being angry at FreeBSD with your very own local AI. :)
