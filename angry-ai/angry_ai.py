#!/usr/bin/env python3
"""Angry FreeBSD AI

Self-directed, ReAct-style code janitor for the
freebsd-src-on-angry-AI repository.

- Persona, goals, and task management live in AI_START_HERE.md
  at the repo root.
- This script only defines:
    * How to talk to the local model (Hugging Face Transformers).
    * How the model can request filesystem actions via ACTION: lines.
    * How to apply patches and feed results back.

It works on:
- CPU-only systems (slow, but it works).
- GPU systems (NVIDIA or Apple Silicon) if you install an appropriate
  torch build.

Typical usage (from angry-ai/ directory):

    make deps
    make run

"""

import argparse
import datetime
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import torch
from transformers import AutoModelForCausalLM, AutoTokenizer


# ---------------------------------------------------------------------------
# Environment / Device diagnostics
# ---------------------------------------------------------------------------


def probe_nvidia_smi() -> Optional[str]:
    """Return nvidia-smi summary string if available and working, else None."""
    try:
        proc = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=name,driver_version,cuda_version",
                "--format=csv,noheader",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=True,
            text=True,
        )
        out = proc.stdout.strip()
        return out or None
    except Exception:
        return None


def print_env_summary() -> None:
    print("=== Angry AI Environment Summary ===", file=sys.stderr)
    print(f"torch.__version__        = {torch.__version__}", file=sys.stderr)
    print(f"torch.cuda.is_available()= {torch.cuda.is_available()}", file=sys.stderr)

    nvidia_info = probe_nvidia_smi()
    if nvidia_info:
        print("nvidia-smi detected:", file=sys.stderr)
        for line in nvidia_info.splitlines():
            print(f"  {line}", file=sys.stderr)
        print(
            "Hint: You have an NVIDIA GPU. If torch.cuda.is_available() is False,\n"
            "you probably installed a CPU-only torch wheel. Consider reinstalling\n"
            "a CUDA-enabled wheel from the official PyTorch index, for example:\n\n"
            "  pip uninstall -y torch\n"
            "  pip install --index-url https://download.pytorch.org/whl/cuXXX torch\n\n"
            "where 'cuXXX' matches (or is close to) the CUDA version reported above.",
            file=sys.stderr,
        )
    else:
        print("nvidia-smi not found or not working (no NVIDIA driver / GPU, or not in PATH).", file=sys.stderr)

    # Apple Silicon / MPS
    if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
        print("MPS backend available (Apple Silicon GPU).", file=sys.stderr)

    if torch.cuda.is_available():
        num = torch.cuda.device_count()
        print(f"CUDA device count        = {num}", file=sys.stderr)
        for i in range(num):
            props = torch.cuda.get_device_properties(i)
            gb = props.total_memory / (1024 ** 3)
            print(f"  [{i}] {props.name} (total_memory ~ {gb:.1f} GiB)", file=sys.stderr)
        current = torch.cuda.current_device()
        print(f"Current CUDA device      = {current}", file=sys.stderr)
    else:
        print("Running without CUDA; model will use CPU or MPS if available.", file=sys.stderr)

    print("====================================", file=sys.stderr)
    sys.stderr.flush()


# ---------------------------------------------------------------------------
# LLM wrapper
# ---------------------------------------------------------------------------


class LocalLLM:
    def __init__(
        self,
        model_path: str,
        max_new_tokens: int = 512,
        temperature: float = 0.1,
    ):
        print_env_summary()
        print(f"[LLM] Loading model from {model_path}", file=sys.stderr)

        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, trust_remote_code=True
        )

        # Use dtype instead of torch_dtype to avoid deprecation warnings.
        if torch.cuda.is_available() or (hasattr(torch.backends, "mps") and torch.backends.mps.is_available()):
            dtype = torch.bfloat16
        else:
            dtype = torch.float32

        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            dtype=dtype,
            device_map=\"auto\",
            trust_remote_code=True,
        )

        self.max_new_tokens = max_new_tokens
        self.temperature = temperature
        self.has_chat_template = hasattr(self.tokenizer, "apply_chat_template") and (
            self.tokenizer.chat_template is not None
        )
        print(f"[LLM] Chat template: {self.has_chat_template}", file=sys.stderr)
        sys.stderr.flush()

    def _format_messages(self, messages: List[Dict[str, str]]) -> str:
        if self.has_chat_template:
            return self.tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        # Fallback: simple role-tagged prompt
        parts = []
        for m in messages:
            parts.append(f"{m['role'].upper()}:\n{m['content'].strip()}\n")
        parts.append("ASSISTANT:\n")
        return "\n".join(parts)

    @torch.no_grad()
    def chat(self, messages: List[Dict[str, str]]) -> str:
        prompt = self._format_messages(messages)
        inputs = self.tokenizer(
            prompt, return_tensors=\"pt\"
        ).to(self.model.device)

        print("[LLM] Starting generation...", file=sys.stderr)
        sys.stderr.flush()

        output_ids = self.model.generate(
            **inputs,
            do_sample=self.temperature > 0.0,
            temperature=self.temperature,
            max_new_tokens=self.max_new_tokens,
            pad_token_id=self.tokenizer.eos_token_id,
        )

        print("[LLM] Finished generation.", file=sys.stderr)
        sys.stderr.flush()

        generated = output_ids[0, inputs["input_ids"].shape[1]:]
        text = self.tokenizer.decode(generated, skip_special_tokens=True)
        return text.strip()


# ---------------------------------------------------------------------------
# ACTION protocol
# ---------------------------------------------------------------------------

ACTION_RE = re.compile(r\"^ACTION:\\s*([A-Z_]+)(.*)$\", re.MULTILINE)


@dataclass
class ParsedAction:
    action: str
    argument: Optional[str] = None
    patch: Optional[str] = None


def parse_action(llm_output: str) -> ParsedAction:
    """Parse the LLM's output for an ACTION directive."""
    m = ACTION_RE.search(llm_output)
    if not m:
        raise ValueError("No ACTION: line found in model output.")

    action = m.group(1).strip()
    rest = m.group(2).strip()

    if action == "APPLY_PATCH":
        # Everything after the ACTION: APPLY_PATCH line is the patch body
        start_idx = m.end()
        patch_body = llm_output[start_idx:].strip()
        return ParsedAction(action="APPLY_PATCH", patch=patch_body)

    # All other actions: the rest of the line is the argument
    argument = rest.strip()
    return ParsedAction(action=action, argument=argument)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def tool_read_file(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        return f"READ_FILE_RESULT for {path}:\n```text\n{text}\n```\n"
    except Exception as e:
        return f"READ_FILE_ERROR for {path}: {e}\n"


def tool_list_dir(path: Path) -> str:
    try:
        if not path.exists():
            return f"LIST_DIR_ERROR: Path does not exist: {path}\n"
        if not path.is_dir():
            return f"LIST_DIR_ERROR: Path is not a directory: {path}\n"

        items = sorted(os.listdir(path))
        listing = "\n".join(items)
        return f"LIST_DIR_RESULT for {path}:\n```text\n{listing}\n```\n"
    except Exception as e:
        return f"LIST_DIR_ERROR for {path}: {e}\n"


def tool_apply_patch(patch_text: str, repo_root: Path) -> str:
    if not patch_text.strip():
        return "APPLY_PATCH_ERROR: empty patch text\n"

    try:
        proc = subprocess.run(
            ["patch", "-p0", "-u", "-N"],
            input=patch_text.encode("utf-8"),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=str(repo_root),
        )
        log = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode == 0:
            return f"APPLY_PATCH_OK:\n```text\n{log}\n```\n"
        else:
            return f"APPLY_PATCH_FAILED (exit code {proc.returncode}):\n```text\n{log}\n```\n"
    except Exception as e:
        return f"APPLY_PATCH_ERROR: {e}\n"


# ---------------------------------------------------------------------------
# Agent loop
# ---------------------------------------------------------------------------


def build_wrapper_system_prompt() -> str:
    """System-level instructions that wrap the repo's own AI_START_HERE.md."""
    return (
        "You are an autonomous code-review and refactoring AI running *inside* a local\n"
        "FreeBSD source tree. You do not have a shell; you can only perform actions\n"
        "through the ACTION protocol described below.\n\n"
        "The human has provided your detailed instructions, goals, and persona in a\n"
        "Markdown file (AI_START_HERE.md). You MUST read and follow those instructions.\n\n"
        "When you need to interact with the repository, you MUST use one of these ACTION forms:\n\n"
        "  ACTION: READ_FILE relative/path/to/file\n"
        "  ACTION: LIST_DIR relative/path/to/dir\n"
        "  ACTION: APPLY_PATCH\n"
        "  <unified diff follows here>\n"
        "  ACTION: HALT\n\n"
        "Rules:\n"
        "- Always use paths relative to the repository root.\n"
        "- When editing code, emit a unified diff under ACTION: APPLY_PATCH.\n"
        "- When you are completely done, emit ACTION: HALT.\n"
        "- Every reply MUST contain exactly one ACTION line, even if you also include explanations.\n"
        "- Keep your natural language commentary concise; focus on concrete actions.\n"
    )


def ensure_logs_dir(repo_root: Path) -> Path:
    logs_dir = repo_root / ".angry-ai" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def now_utc_string() -> str:
    return datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")


def agent_loop(
    repo_root: Path,
    bootstrap_path: Path,
    llm: LocalLLM,
    max_steps: int = 100,
) -> None:
    logs_dir = ensure_logs_dir(repo_root)

    # Read the repo's AI_START_HERE.md (bootstrap instructions & persona)
    bootstrap_text = bootstrap_path.read_text(encoding="utf-8")

    # System wrapper + bootstrap as user
    history: List[Dict[str, str]] = [
        {"role": "system", "content": build_wrapper_system_prompt()},
        {
            "role": "user",
            "content": (
                "Here is your bootstrap instruction file (AI_START_HERE.md). " 
                "Read it carefully and then begin following its directions.\n\n"
                "```markdown\n"
                f"{bootstrap_text}\n"
                "```\n"
            ),
        },
    ]

    for step in range(1, max_steps + 1):
        print(f"[AGENT] Step {step} - querying LLM...", file=sys.stderr)
        sys.stderr.flush()

        llm_output = llm.chat(history)

        # Log raw output
        ts = now_utc_string()
        log_path = logs_dir / f"agent_step_{step}_{ts}.txt"
        log_path.write_text(llm_output, encoding="utf-8")

        # Show user what the model said
        print("[AGENT OUTPUT BEGIN]")
        print(llm_output)
        print("[AGENT OUTPUT END]")
        sys.stdout.flush()
        sys.stderr.flush()

        if not llm_output.strip():
            print("[AGENT] Empty model output; asking it to respond again.", file=sys.stderr)
            history.append({"role": "assistant", "content": llm_output})
            history.append(
                {
                    "role": "user",
                    "content": "ERROR: Your last reply was empty. You must respond with a valid ACTION line.",
                }
            )
            continue

        # Try to parse ACTION
        try:
            parsed = parse_action(llm_output)
        except Exception as e:
            print(f"[AGENT] ACTION PARSE ERROR: {e}", file=sys.stderr)
            history.append({"role": "assistant", "content": llm_output})
            history.append(
                {
                    "role": "user",
                    "content": (
                        "ERROR: Could not find or parse an ACTION line in your last reply. "
                        "Remember: each reply MUST include exactly one line starting with "
                        "'ACTION:' followed by one of READ_FILE, LIST_DIR, APPLY_PATCH, HALT, "
                        "and follow the protocol described earlier. Try again."
                    ),
                }
            )
            continue

        # Record the assistant message
        history.append({"role": "assistant", "content": llm_output})

        action = parsed.action
        arg = (parsed.argument or "").strip() if parsed.argument else ""

        if action == "HALT":
            print("[AGENT] Received ACTION: HALT. Exiting.", file=sys.stderr)
            sys.stderr.flush()
            break

        elif action == "READ_FILE":
            target = (repo_root / arg).resolve()
            if not str(target).startswith(str(repo_root)):
                result = f"READ_FILE_ERROR: Path escapes repo root: {arg}\n"
            else:
                print(f"[AGENT TOOL] READ_FILE {target}", file=sys.stderr)
                result = tool_read_file(target)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result[:2000])
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "LIST_DIR":
            target = (repo_root / arg).resolve()
            if not str(target).startswith(str(repo_root)):
                result = f"LIST_DIR_ERROR: Path escapes repo root: {arg}\n"
            else:
                print(f"[AGENT TOOL] LIST_DIR {target}", file=sys.stderr)
                result = tool_list_dir(target)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "APPLY_PATCH":
            print("[AGENT TOOL] APPLY_PATCH invoked", file=sys.stderr)
            print("[AGENT TOOL PATCH PREVIEW BEGIN]")
            print((parsed.patch or "")[:2000])
            print("[AGENT TOOL PATCH PREVIEW END]")
            sys.stdout.flush()
            sys.stderr.flush()

            result = tool_apply_patch(parsed.patch or "", repo_root)

            print("[AGENT TOOL APPLY_PATCH RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL APPLY_PATCH RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        else:
            print(f"[AGENT] Unknown ACTION: {action}", file=sys.stderr)
            history.append(
                {
                    "role": "user",
                    "content": (
                        f"ERROR: Unknown ACTION '{action}'. "
                        "Valid actions are READ_FILE, LIST_DIR, APPLY_PATCH, HALT.\n"
                    ),
                }
            )
            continue

    else:
        print(f"[AGENT] Reached max_steps={max_steps} without HALT. Stopping.", file=sys.stderr)
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Angry FreeBSD AI (local ReAct wrapper over AI_START_HERE.md)",
    )
    parser.add_argument(
        "--repo",
        default="..",
        help="Path to repository root (default: ..)",
    )
    parser.add_argument(
        "--bootstrap",
        default="../AI_START_HERE.md",
        help="Path to AI_START_HERE.md in the repo (default: ../AI_START_HERE.md)",
    )
    parser.add_argument(
        "--model",
        required=True,
        help="Path to local HF model directory.",
    )
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-steps", type=int, default=100)

    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    bootstrap_path = Path(args.bootstrap).resolve()

    if not repo_root.is_dir():
        print(f"[ERROR] Repo path is not a directory: {repo_root}", file=sys.stderr)
        sys.exit(1)

    if not bootstrap_path.is_file():
        print(f"[ERROR] Bootstrap file not found: {bootstrap_path}", file=sys.stderr)
        sys.exit(1)

    # Work *inside* the repo, just like an IDE would.
    os.chdir(repo_root)
    print(f"[INFO] Changed directory to repo root: {repo_root}", file=sys.stderr)
    sys.stderr.flush()

    llm = LocalLLM(
        model_path=args.model,
        max_new_tokens=args.max_new_tokens,
        temperature=args.temperature,
    )

    agent_loop(
        repo_root=repo_root,
        bootstrap_path=bootstrap_path,
        llm=llm,
        max_steps=args.max_steps,
    )


if __name__ == "__main__":
    main()
