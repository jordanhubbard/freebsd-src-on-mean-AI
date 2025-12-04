# Angry FreeBSD AI

This directory contains a self-contained "angry AI" wrapper that can run locally against the `freebsd-src-on-angry-AI` repository using a local Hugging Face model (CPU-only or GPU-accelerated).

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

By default, the script:

- Treats the parent directory (`..`) as the FreeBSD repo root.
- Uses `../AI_START_HERE.md` as the bootstrap instructions.
- Expects the model to live in `angry-ai/Qwen2.5-Coder-32B-Instruct`.
- Logs all model replies and tool activity under `.angry-ai/logs` in the repo.
- **Automatically detects NVIDIA GPUs** and installs CUDA-enabled PyTorch if available.

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

### GPU Support

The `make deps` command automatically detects if you have an NVIDIA GPU (via `nvidia-smi`) and installs the appropriate PyTorch build:

- **NVIDIA GPU detected**: Installs CUDA-enabled PyTorch (nightly cu130 build)
- **No NVIDIA GPU**: Installs CPU-only PyTorch

No manual intervention required! Just run `make deps` and it will do the right thing.

## CPU vs GPU

This works on:

- **CPU-only machines:** it will be slow, but it will run.
- **GPU-equipped machines (NVIDIA):** `make deps` automatically detects your GPU and installs CUDA-enabled PyTorch.
- **Apple Silicon / MPS:** PyTorch will automatically use the MPS backend if available.

The script prints an environment summary at startup showing your hardware and PyTorch configuration.

### Manual PyTorch Installation (Advanced)

If you need a different PyTorch build than what `make deps` installs, you can manually install it:

1. Remove the auto-installed PyTorch:
   ```sh
   . .venv/bin/activate
   pip uninstall -y torch
   ```

2. Install your preferred build from **https://pytorch.org/get-started/locally/**

3. Run the script:
   ```sh
   make run
   ```

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

Examples (you must download these yourself, unless you rely on `make model` with the default Qwen2.5 32B path):

- `Qwen/Qwen2.5-Coder-7B-Instruct`
- `Qwen/Qwen2.5-Coder-32B-Instruct`
- your own fine-tuned coding model

Point the `MODEL` variable at the directory where you cloned or downloaded the model, e.g.:

```sh
git lfs install
git clone https://huggingface.co/Qwen/Qwen2.5-Coder-7B-Instruct /srv/models/Qwen2.5-Coder-7B-Instruct

make run MODEL=/srv/models/Qwen2.5-Coder-7B-Instruct
```

## Safety and Sanity

- This tool will apply patches to your working tree using `patch(1)`. Run it on a branch, and use `git status` / `git diff` to inspect changes.
- Logs are stored under `.angry-ai/logs` so you can inspect exactly what the model said and what patches it attempted to apply.
- If things go sideways, you can always reset your branch and try again with different parameters or a different model.

Have fun being angry at FreeBSD with your very own local AI. :)
