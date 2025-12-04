#!/usr/bin/env python3
"""
Angry FreeBSD AI

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
    """
    Return a short nvidia-smi output if available and working, else None.

    We intentionally do *not* use --query-* flags here, to maximize
    compatibility across different nvidia-smi versions.
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout.strip()
        return out or None
    except FileNotFoundError:
        return None
    except Exception:
        return None


def print_env_summary() -> None:
    print("=== Angry AI Environment Summary ===", file=sys.stderr)
    print(f"torch.__version__        = {torch.__version__}", file=sys.stderr)
    print(f"torch.cuda.is_available()= {torch.cuda.is_available()}", file=sys.stderr)

    nvidia_info = probe_nvidia_smi()
    if nvidia_info:
        print("nvidia-smi detected (first lines):", file=sys.stderr)
        # Print only the first few lines so we don't spam too hard.
        for line in nvidia_info.splitlines()[:5]:
            print(f"  {line}", file=sys.stderr)
        print(
            "Hint: If torch.cuda.is_available() is False but nvidia-smi works,\n"
            "you probably installed a CPU-only torch wheel. Consider reinstalling\n"
            "a CUDA-enabled wheel from the official PyTorch index, for example:\n\n"
            "  pip uninstall -y torch\n"
            "  pip install --index-url https://download.pytorch.org/whl/cuXXX torch\n\n"
            "where 'cuXXX' matches (or is close to) the CUDA version reported above.",
            file=sys.stderr,
        )
    else:
        print(
            "nvidia-smi not found or not working (no NVIDIA driver / GPU, or not in PATH).",
            file=sys.stderr,
        )

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
        max_new_tokens: int = 2048,
        temperature: float = 0.1,
    ):
        print_env_summary()
        print(f"[LLM] Loading model from {model_path}", file=sys.stderr)

        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path,
            trust_remote_code=True,
        )

        # Use dtype instead of torch_dtype to avoid deprecation warnings.
        if torch.cuda.is_available() or (
            hasattr(torch.backends, "mps") and torch.backends.mps.is_available()
        ):
            dtype = torch.bfloat16
        else:
            dtype = torch.float32

        self.model = AutoModelForCausalLM.from_pretrained(
            model_path,
            dtype=dtype,
            device_map="auto",
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
        
        # Get model's maximum context length (default to 32K for Qwen2.5)
        max_context = getattr(self.tokenizer, 'model_max_length', 32768)
        # Reserve tokens for generation
        max_input_tokens = max_context - self.max_new_tokens - 100  # 100 token safety buffer
        
        inputs = self.tokenizer(
            prompt, 
            return_tensors="pt",
            truncation=True,
            max_length=max_input_tokens
        ).to(self.model.device)
        
        input_token_count = inputs["input_ids"].shape[1]
        if input_token_count >= max_input_tokens - 100:
            print(f"[LLM] WARNING: Input truncated! {input_token_count} tokens (limit: {max_input_tokens})", file=sys.stderr)
            print("[LLM] Consider reducing context or file sizes in READ_FILE results.", file=sys.stderr)
        else:
            print(f"[LLM] Input tokens: {input_token_count} / {max_input_tokens}", file=sys.stderr)

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

ACTION_RE = re.compile(r"^ACTION:\s*([A-Z_]+)(.*)$", re.MULTILINE)


@dataclass
class ParsedAction:
    action: str
    argument: Optional[str] = None
    patch: Optional[str] = None
    old_str: Optional[str] = None
    new_str: Optional[str] = None
    content: Optional[str] = None


def validate_relative_path(path: str) -> None:
    """
    Validate that a path is safe (relative, no escapes).
    
    Unix-specific implementation - we know we're always on Unix (Linux/macOS).
    
    Raises ValueError if the path is unsafe.
    """
    if not path:
        raise ValueError("Path cannot be empty")
    
    # Unix paths only - check for absolute path (starts with /)
    if path.startswith('/'):
        raise ValueError(f"Path must be relative, not absolute: {path}")
    
    # Check for null bytes (Unix path terminator)
    if '\0' in path:
        raise ValueError("Path contains null byte")
    
    # Normalize and check for parent directory escapes
    # Since we're on Unix, we only need to check for ../ patterns
    normalized = os.path.normpath(path)
    if normalized.startswith('..') or '/..' in normalized:
        raise ValueError(f"Path attempts to escape repo root: {path}")
    
    # Check for other dangerous patterns
    if path.startswith('~'):
        raise ValueError(f"Path cannot use home directory expansion: {path}")


def resolve_repo_path(relative_path: str, repo_root: Path) -> Path:
    """
    Resolve a relative path within the repo, ensuring it stays within repo bounds.
    
    Unix-specific: Uses realpath to resolve symlinks (always available on Unix).
    Handles macOS-specific /private prefix for /tmp and /var paths.
    
    Args:
        relative_path: Path relative to repo root
        repo_root: Absolute path to repository root
        
    Returns:
        Resolved absolute path
        
    Raises:
        ValueError if path escapes repo root
    """
    # On Unix, we can rely on realpath to resolve symlinks and get canonical paths
    # Resolve both paths to handle macOS /private prefix (e.g., /var -> /private/var)
    repo_root_resolved = repo_root.resolve()
    target = (repo_root / relative_path).resolve()
    
    # Ensure the resolved path is within the resolved repo_root
    # On Unix, this is a simple prefix check after canonicalization
    try:
        target.relative_to(repo_root_resolved)
    except ValueError:
        raise ValueError(f"Path '{relative_path}' resolves outside repo root: {target}")
    
    return target


def strip_markdown_fences(text: str) -> str:
    """
    Strip markdown code fences from text if present.
    Handles both ```language and ``` forms.
    """
    text = text.strip()
    lines = text.split('\n')
    
    # Check if first/last lines are fences
    if lines and lines[0].strip().startswith('```'):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith('```'):
        lines = lines[:-1]
    
    return '\n'.join(lines)


def parse_action(llm_output: str) -> ParsedAction:
    """
    Parse the LLM's output for an ACTION directive.

    Supported forms:

        ACTION: READ_FILE path/to/file
        ACTION: LIST_DIR path/to/dir
        ACTION: EDIT_FILE path/to/file
        OLD:
        <<<
        exact old text
        >>>
        NEW:
        <<<
        replacement text
        >>>
        ACTION: WRITE_FILE path/to/file
        CONTENT:
        <<<
        file content
        >>>
        ACTION: APPLY_PATCH
        <unified diff goes here>
        ACTION: HALT
    """
    # Find ALL ACTION lines and use the LAST one (as per instructions)
    matches = list(ACTION_RE.finditer(llm_output))
    if not matches:
        raise ValueError("No ACTION: line found in model output.")
    
    m = matches[-1]  # Use the LAST ACTION line

    action = m.group(1).strip()
    rest = m.group(2).strip()

    if action == "APPLY_PATCH":
        # Everything after the ACTION: APPLY_PATCH line is the patch body
        start_idx = m.end()
        patch_body = llm_output[start_idx:].strip()
        return ParsedAction(action="APPLY_PATCH", patch=patch_body)

    if action == "EDIT_FILE":
        # Parse: ACTION: EDIT_FILE path/to/file
        #        OLD:\n<<<\nold text\n>>>\nNEW:\n<<<\nnew text\n>>>
        path = rest.strip()
        validate_relative_path(path)
        
        body = llm_output[m.end():].strip()
        
        # Extract OLD block - more lenient regex (allows whitespace variations)
        # Matches: OLD: <<< or OLD:\n<<<
        old_match = re.search(r'OLD:\s*\n?\s*<<<\s*\n(.*?)\n\s*>>>', body, re.DOTALL)
        if not old_match:
            # Show what we found for debugging
            preview = body[:300].replace('\n', '\\n')
            raise ValueError(
                f"EDIT_FILE: Could not find OLD:\\n<<<\\n...\\n>>> block.\n"
                f"Expected format:\n"
                f"  OLD:\n"
                f"  <<<\n"
                f"  old text here\n"
                f"  >>>\n"
                f"Body preview: {preview}..."
            )
        old_str = old_match.group(1)
        old_str = strip_markdown_fences(old_str)
        
        # Extract NEW block - more lenient regex
        new_match = re.search(r'NEW:\s*\n?\s*<<<\s*\n(.*?)\n\s*>>>', body, re.DOTALL)
        if not new_match:
            preview = body[:300].replace('\n', '\\n')
            raise ValueError(
                f"EDIT_FILE: Could not find NEW:\\n<<<\\n...\\n>>> block.\n"
                f"Expected format:\n"
                f"  NEW:\n"
                f"  <<<\n"
                f"  new text here\n"
                f"  >>>\n"
                f"Body preview: {preview}..."
            )
        new_str = new_match.group(1)
        new_str = strip_markdown_fences(new_str)
        
        return ParsedAction(action="EDIT_FILE", argument=path, old_str=old_str, new_str=new_str)

    if action == "WRITE_FILE":
        # Parse: ACTION: WRITE_FILE path/to/file
        #        CONTENT:\n<<<\nfile content\n>>>
        path = rest.strip()
        validate_relative_path(path)
        
        body = llm_output[m.end():].strip()
        
        # Extract CONTENT block - more lenient regex
        content_match = re.search(r'CONTENT:\s*\n?\s*<<<\s*\n(.*?)\n\s*>>>', body, re.DOTALL)
        if not content_match:
            preview = body[:300].replace('\n', '\\n')
            raise ValueError(
                f"WRITE_FILE: Could not find CONTENT:\\n<<<\\n...\\n>>> block.\n"
                f"Expected format:\n"
                f"  CONTENT:\n"
                f"  <<<\n"
                f"  file content here\n"
                f"  >>>\n"
                f"Body preview: {preview}..."
            )
        content = content_match.group(1)
        content = strip_markdown_fences(content)
        
        return ParsedAction(action="WRITE_FILE", argument=path, content=content)

    # All other actions: the rest of the line is the argument
    argument = rest.strip()
    
    # Validate paths for file/dir operations
    if action in ("READ_FILE", "LIST_DIR") and argument:
        validate_relative_path(argument)
    
    return ParsedAction(action=action, argument=argument)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def tool_read_file(path: Path, max_chars: int = 50000) -> str:
    """
    Read a file and return its contents, truncating if necessary.
    
    Args:
        path: Path to the file
        max_chars: Maximum number of characters to return (default 50K)
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
        
        if len(text) > max_chars:
            lines = text.splitlines(keepends=True)
            truncated = ""
            char_count = 0
            line_count = 0
            
            for line in lines:
                if char_count + len(line) > max_chars:
                    break
                truncated += line
                char_count += len(line)
                line_count += 1
            
            total_lines = len(lines)
            remaining_lines = total_lines - line_count
            
            warning = (
                f"\n\n[... FILE TRUNCATED: showing {line_count}/{total_lines} lines "
                f"({char_count}/{len(text)} chars) ...]\n"
                f"[... {remaining_lines} more lines not shown ...]\n"
            )
            return f"READ_FILE_RESULT for {path}:\n```text\n{truncated}{warning}```\n"
        
        return f"READ_FILE_RESULT for {path}:\n```text\n{text}\n```\n"
    except Exception as e:
        return f"READ_FILE_ERROR for {path}: {e}\n"


def tool_list_dir(path: Path, show_ignored: bool = False) -> str:
    """
    List directory contents, optionally filtering out .gitignore'd files.
    
    Unix-specific: Uses 'git check-ignore' to filter (always available since we're in a repo).
    """
    try:
        if not path.exists():
            return f"LIST_DIR_ERROR: Path does not exist: {path}\n"
        if not path.is_dir():
            return f"LIST_DIR_ERROR: Path is not a directory: {path}\n"

        items = sorted(os.listdir(path))
        
        # Filter out gitignored files by default (Unix-specific optimization)
        if not show_ignored:
            filtered_items = []
            for item in items:
                item_path = path / item
                # Use git check-ignore to test if file should be ignored
                # This is fast and respects .gitignore rules
                result = subprocess.run(
                    ["git", "check-ignore", "-q", str(item_path)],
                    cwd=path,
                    capture_output=True
                )
                # Exit code 0 means ignored, 1 means not ignored
                if result.returncode != 0:
                    filtered_items.append(item)
            items = filtered_items
        
        listing = "\n".join(items) if items else "(empty or all files ignored)"
        return f"LIST_DIR_RESULT for {path}:\n```text\n{listing}\n```\n"
    except Exception as e:
        return f"LIST_DIR_ERROR for {path}: {e}\n"


def tool_edit_file(path: Path, old_str: str, new_str: str) -> str:
    """
    Edit a file by finding and replacing old_str with new_str.
    
    This is more reliable than unified diffs for LLMs since it just requires
    copying exact text from a previous READ_FILE result.
    """
    try:
        if not path.exists():
            return f"EDIT_FILE_ERROR: File does not exist: {path}\n"
        if not path.is_file():
            return f"EDIT_FILE_ERROR: Path is not a file: {path}\n"
        
        content = path.read_text(encoding="utf-8", errors="replace")
        
        # Check if old_str exists in the file
        if old_str not in content:
            return (
                f"EDIT_FILE_ERROR: Could not find the OLD text in {path}\n\n"
                "Make sure you copied the exact text from the file.\n"
                "The OLD text must match exactly, including all whitespace.\n\n"
                f"You provided:\n<<<\n{old_str}\n>>>\n"
            )
        
        # Check if old_str appears multiple times
        count = content.count(old_str)
        if count > 1:
            return (
                f"EDIT_FILE_ERROR: The OLD text appears {count} times in {path}\n\n"
                "The OLD text must be unique in the file.\n"
                "Please include more surrounding context to make it unique.\n\n"
                f"You provided:\n<<<\n{old_str}\n>>>\n"
            )
        
        # Perform the replacement
        new_content = content.replace(old_str, new_str)
        path.write_text(new_content, encoding="utf-8")
        
        return f"EDIT_FILE_OK: Successfully edited {path}\n"
    except Exception as e:
        return f"EDIT_FILE_ERROR for {path}: {e}\n"


def tool_write_file(path: Path, content: str) -> str:
    """
    Write content to a file, creating it if it doesn't exist.
    
    Useful for creating new files or completely rewriting small files.
    """
    try:
        # Create parent directories if they don't exist
        path.parent.mkdir(parents=True, exist_ok=True)
        
        path.write_text(content, encoding="utf-8")
        
        return f"WRITE_FILE_OK: Successfully wrote {path}\n"
    except Exception as e:
        return f"WRITE_FILE_ERROR for {path}: {e}\n"


def _strip_markdown_fences(patch_text: str) -> str:
    """
    If the model wrapped the diff in ```...``` fences, strip them.

    We look for the first line starting with ``` and the last such line,
    and keep only the content in between. If no fences, return as-is.
    """
    lines = patch_text.splitlines()
    fence_indices = [i for i, line in enumerate(lines) if line.strip().startswith("```")]
    if len(fence_indices) >= 2:
        start = fence_indices[0] + 1
        end = fence_indices[-1]
        return "\n".join(lines[start:end]).strip() + "\n"
    return patch_text


def tool_apply_patch(patch_text: str, repo_root: Path) -> str:
    """
    Apply a unified diff to the repo using patch(1).

    - Strips markdown fences if present.
    - Tries patch -p1 first (for git-style diffs: a/foo, b/foo).
    - Falls back to patch -p0 if -p1 fails.
    """
    if not patch_text.strip():
        return "APPLY_PATCH_ERROR: empty patch text\n"

    cleaned = _strip_markdown_fences(patch_text)
    
    # Validate that the patch looks like a unified diff
    lines = cleaned.strip().splitlines()
    if not lines:
        return "APPLY_PATCH_ERROR: empty patch after cleaning\n"
    
    # Check if patch has actual content beyond just the header
    has_hunks = any(line.startswith('@@ ') for line in lines)
    has_changes = any(line.startswith(('+', '-')) for line in lines[1:])  # Skip first line
    
    if not has_hunks or not has_changes:
        return (
            "APPLY_PATCH_ERROR: Incomplete patch detected.\n"
            "Your patch only contains headers but no actual changes.\n\n"
            "A valid unified diff must include:\n"
            "1. File headers (--- a/path/to/file and +++ b/path/to/file)\n"
            "2. Hunk headers (@@ -start,count +start,count @@)\n"
            "3. Context lines (unchanged, starting with space)\n"
            "4. Removed lines (starting with -)\n"
            "5. Added lines (starting with +)\n\n"
            "Example of a complete unified diff:\n"
            "```\n"
            "--- a/bin/pkill/pkill.c\n"
            "+++ b/bin/pkill/pkill.c\n"
            "@@ -100,7 +100,10 @@ int main(int argc, char **argv)\n"
            "     if (argc < 2) {\n"
            "         usage();\n"
            "     }\n"
            "-    process_args(argv);\n"
            "+    if (validate_args(argv) != 0) {\n"
            "+        return 1;\n"
            "+    }\n"
            "+    process_args(argv);\n"
            "     return 0;\n"
            " }\n"
            "```\n\n"
            f"Your patch was only:\n```\n{cleaned}\n```\n"
        )

    attempts = []
    for p_level in (1, 0):
        try:
            proc = subprocess.run(
                ["patch", f"-p{p_level}", "-u", "-N"],
                input=cleaned.encode("utf-8"),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                cwd=str(repo_root),
            )
            log = proc.stdout.decode("utf-8", errors="replace")
            attempts.append((p_level, proc.returncode, log))
            if proc.returncode == 0:
                return f"APPLY_PATCH_OK (patch -p{p_level}):\n```text\n{log}\n```\n"
        except Exception as e:
            attempts.append((p_level, -1, f"Exception: {e}"))

    # If we got here, all attempts failed
    msg_lines = ["APPLY_PATCH_FAILED:"]
    for p_level, rc, log in attempts:
        msg_lines.append(f"--- patch -p{p_level} (rc={rc}) ---")
        msg_lines.append(log)
    return "```text\n" + "\n".join(msg_lines) + "\n```\n"


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
        "    - Reads and returns file contents\n\n"
        "  ACTION: LIST_DIR relative/path/to/dir\n"
        "    - Lists directory contents\n\n"
        "  ACTION: EDIT_FILE relative/path/to/file\n"
        "  OLD:\n"
        "  <<<\n"
        "  exact text to find\n"
        "  >>>\n"
        "  NEW:\n"
        "  <<<\n"
        "  replacement text\n"
        "  >>>\n"
        "    - Edits a file by finding and replacing OLD text with NEW text\n"
        "    - OLD text must be EXACT (copy from READ_FILE output)\n"
        "    - OLD text must be UNIQUE in the file\n"
        "    - Include enough context to make OLD unique\n"
        "    - Whitespace is preserved\n\n"
        "  ACTION: WRITE_FILE relative/path/to/file\n"
        "  CONTENT:\n"
        "  <<<\n"
        "  entire file content\n"
        "  >>>\n"
        "    - Writes content to a file (creates if doesn't exist)\n"
        "    - Use for new files or complete rewrites\n\n"
        "  ACTION: APPLY_PATCH\n"
        "  <unified diff follows here>\n"
        "    - Legacy method: applies a unified diff\n"
        "    - Prefer EDIT_FILE for most edits (more reliable)\n\n"
        "  ACTION: HALT\n"
        "    - Signals completion\n\n"
        "Rules:\n"
        "- Always use paths relative to the repository root.\n"
        "- PREFER EDIT_FILE over APPLY_PATCH for code edits (simpler, more reliable).\n"
        "- For EDIT_FILE: Copy the exact text from READ_FILE, including whitespace.\n"
        "- For EDIT_FILE: Include enough surrounding lines to make OLD text unique.\n"
        "- When you are completely done, emit ACTION: HALT.\n"
        "- You may include commentary and analysis ABOVE the ACTION line, but your FINAL line\n"
        "  in every reply MUST be exactly one ACTION line.\n"
        "- Example of a valid reply:\n"
        "    I will now inspect the pkill implementation for security issues.\n"
        "    ACTION: READ_FILE bin/pkill/pkill.c\n"
        "- Another example (EDIT_FILE):\n"
        "    I will add input validation to the process_args function.\n"
        "    ACTION: EDIT_FILE bin/pkill/pkill.c\n"
        "    OLD:\n"
        "    <<<\n"
        "    int main(int argc, char **argv) {\n"
        "        process_args(argv);\n"
        "        return 0;\n"
        "    }\n"
        "    >>>\n"
        "    NEW:\n"
        "    <<<\n"
        "    int main(int argc, char **argv) {\n"
        "        if (validate_args(argv) != 0) {\n"
        "            return 1;\n"
        "        }\n"
        "        process_args(argv);\n"
        "        return 0;\n"
        "    }\n"
        "    >>>\n"
        "- Keep your natural language commentary concise; focus on concrete actions.\n"
    )


def ensure_logs_dir(repo_root: Path) -> Path:
    logs_dir = repo_root / ".angry-ai" / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    return logs_dir


def now_utc_string() -> str:
    # Simple UTC timestamp string; timezone-naive is fine for log naming.
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
                    "content": (
                        "ERROR: Your last reply was empty. You must respond with a valid ACTION line.\n"
                        "Remember: your FINAL line must be exactly one ACTION: ... line."
                    ),
                }
            )
            continue

        # Try to parse ACTION
        try:
            parsed = parse_action(llm_output)
        except Exception as e:
            print(f"[AGENT] ACTION PARSE ERROR: {e}", file=sys.stderr)
            # Record the analysis anyway
            history.append({"role": "assistant", "content": llm_output})
            # Now explicitly ask for a short follow-up that ends with an ACTION line
            history.append(
                {
                    "role": "user",
                    "content": (
                        "Your last reply contained analysis but no valid ACTION line. "
                        "That analysis is fine and has been recorded.\n\n"
                        "Now you MUST choose your next concrete step and reply in the following strict format:\n"
                        "1. Optionally one very short sentence describing what you are about to do next.\n"
                        "2. On the FINAL line, a single ACTION line, for example:\n"
                        "   ACTION: READ_FILE bin/pkill/pkill.c\n"
                        "   or\n"
                        "   ACTION: EDIT_FILE bin/pkill/pkill.c\n"
                        "   OLD:\n"
                        "   <<<\n"
                        "   exact old text\n"
                        "   >>>\n"
                        "   NEW:\n"
                        "   <<<\n"
                        "   replacement text\n"
                        "   >>>\n"
                        "   or\n"
                        "   ACTION: HALT\n\n"
                        "Do not omit the ACTION line. Do not send another analysis-only reply.\n"
                    ),
                }
            )
            continue

        # Record the assistant message
        history.append({"role": "assistant", "content": llm_output})

        action = parsed.action
        arg = (parsed.argument or "").strip() if parsed.argument else ""
        
        # Debug logging for successful parse
        debug_info = f"[AGENT] Parsed ACTION: {action}"
        if arg:
            debug_info += f" arg={arg}"
        if parsed.old_str is not None:
            debug_info += f" old_len={len(parsed.old_str)}"
        if parsed.new_str is not None:
            debug_info += f" new_len={len(parsed.new_str)}"
        if parsed.content is not None:
            debug_info += f" content_len={len(parsed.content)}"
        if parsed.patch is not None:
            debug_info += f" patch_len={len(parsed.patch)}"
        print(debug_info, file=sys.stderr)
        sys.stderr.flush()

        if action == "HALT":
            print("[AGENT] Received ACTION: HALT. Exiting.", file=sys.stderr)
            sys.stderr.flush()
            break

        elif action == "READ_FILE":
            try:
                target = resolve_repo_path(arg, repo_root)
                print(f"[AGENT TOOL] READ_FILE {target}", file=sys.stderr)
                result = tool_read_file(target)
            except ValueError as e:
                result = f"READ_FILE_ERROR: {e}\n"

            # Display full result (already truncated by tool_read_file if needed)
            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "LIST_DIR":
            try:
                target = resolve_repo_path(arg, repo_root)
                print(f"[AGENT TOOL] LIST_DIR {target}", file=sys.stderr)
                result = tool_list_dir(target)
            except ValueError as e:
                result = f"LIST_DIR_ERROR: {e}\n"

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "EDIT_FILE":
            try:
                target = resolve_repo_path(arg, repo_root)
                print(f"[AGENT TOOL] EDIT_FILE {target}", file=sys.stderr)
                print(f"[AGENT TOOL] OLD text length: {len(parsed.old_str or '')}", file=sys.stderr)
                print(f"[AGENT TOOL] NEW text length: {len(parsed.new_str or '')}", file=sys.stderr)
                result = tool_edit_file(target, parsed.old_str or "", parsed.new_str or "")
            except ValueError as e:
                result = f"EDIT_FILE_ERROR: {e}\n"

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "WRITE_FILE":
            try:
                target = resolve_repo_path(arg, repo_root)
                print(f"[AGENT TOOL] WRITE_FILE {target}", file=sys.stderr)
                print(f"[AGENT TOOL] Content length: {len(parsed.content or '')}", file=sys.stderr)
                result = tool_write_file(target, parsed.content or "")
            except ValueError as e:
                result = f"WRITE_FILE_ERROR: {e}\n"

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
                        "Valid actions are READ_FILE, LIST_DIR, EDIT_FILE, WRITE_FILE, APPLY_PATCH, HALT.\n"
                        "Respond again with a valid ACTION line as your FINAL line."
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
    parser.add_argument("--max-new-tokens", type=int, default=2048)
    parser.add_argument("--temperature", type=float, default=0.1)
    parser.add_argument("--max-steps", type=int, default=100)

    args = parser.parse_args()

    repo_root = Path(args.repo).resolve()
    bootstrap_path = Path(args.bootstrap).resolve()
    model_path = Path(args.model).resolve()

    if not repo_root.is_dir():
        print(f"[ERROR] Repo path is not a directory: {repo_root}", file=sys.stderr)
        sys.exit(1)

    if not bootstrap_path.is_file():
        print(f"[ERROR] Bootstrap file not found: {bootstrap_path}", file=sys.stderr)
        sys.exit(1)

    if not model_path.is_dir():
        print(f"[ERROR] Model path is not a directory: {model_path}", file=sys.stderr)
        sys.exit(1)

    # Work *inside* the repo, just like an IDE would.
    os.chdir(repo_root)
    print(f"[INFO] Changed directory to repo root: {repo_root}", file=sys.stderr)
    sys.stderr.flush()

    llm = LocalLLM(
        model_path=str(model_path),
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
