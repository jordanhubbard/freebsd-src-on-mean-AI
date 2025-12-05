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
        max_input_tokens = max_context - self.max_new_tokens - 200  # 200 token safety buffer
        
        inputs = self.tokenizer(
            prompt, 
            return_tensors="pt",
            truncation=True,
            max_length=max_input_tokens
        ).to(self.model.device)
        
        input_token_count = inputs["input_ids"].shape[1]
        if input_token_count >= max_input_tokens - 200:
            print(f"[LLM] *** CRITICAL WARNING: Input truncated! ***", file=sys.stderr)
            print(f"[LLM] Token usage: {input_token_count} / {max_input_tokens} (near limit!)", file=sys.stderr)
            print(f"[LLM] Context may be corrupted. Model output may be unreliable.", file=sys.stderr)
            print(f"[LLM] The agent's context pruning should prevent this.", file=sys.stderr)
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

    if action == "RUN_COMMAND":
        # Parse: ACTION: RUN_COMMAND
        #        <<<\ncommand here\n>>>
        body = llm_output[m.end():].strip()
        
        # Extract command block
        cmd_match = re.search(r'<<<\n(.*?)\n>>>', body, re.DOTALL)
        if not cmd_match:
            raise ValueError("RUN_COMMAND: Could not find <<<\n...\n>>> block")
        command = cmd_match.group(1)
        
        return ParsedAction(action="RUN_COMMAND", content=command)

    if action == "EDIT_MULTIPLE":
        # Parse: ACTION: EDIT_MULTIPLE
        #        <<<\n[JSON array]\n>>>
        body = llm_output[m.end():].strip()
        
        # Extract JSON block
        json_match = re.search(r'<<<\n(.*?)\n>>>', body, re.DOTALL)
        if not json_match:
            raise ValueError("EDIT_MULTIPLE: Could not find <<<\n...\n>>> block")
        json_content = json_match.group(1)
        
        return ParsedAction(action="EDIT_MULTIPLE", content=json_content)
    
    if action == "GIT_COMMIT":
        # Parse: ACTION: GIT_COMMIT
        #        <<<\ncommit message\n>>>
        body = llm_output[m.end():].strip()
        
        # Extract message block
        msg_match = re.search(r'<<<\n(.*?)\n>>>', body, re.DOTALL)
        if not msg_match:
            raise ValueError("GIT_COMMIT: Could not find <<<\n...\n>>> block")
        message = msg_match.group(1)
        
        return ParsedAction(action="GIT_COMMIT", content=message)

    # All other actions: the rest of the line is the argument
    argument = rest.strip()
    
    # Validate paths for file/dir operations
    if action in ("READ_FILE", "LIST_DIR") and argument:
        validate_relative_path(argument)
    
    return ParsedAction(action=action, argument=argument)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------


def tool_read_file(path: Path, max_chars: int = 8000) -> str:
    """
    Read a file and return its contents, truncating if necessary.
    
    Args:
        path: Path to the file
        max_chars: Maximum number of characters to return (default 8K)
        
    Note: 8K chars ≈ 2K tokens, helping prevent context overflow.
    For large files, use READ_FILE_LINES to read specific sections.
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
                f"[... {remaining_lines} more lines not shown ...]\n\n"
                f"TIP: This file is large. To review it in manageable chunks:\n"
                f"  1. Use READ_FILE_LINES {path.name} <start> <end> to read specific sections\n"
                f"  2. Use SCAN_FILE {path.name} to see the file structure\n"
                f"  3. Use GREP or FIND_DEFINITION to locate specific code\n"
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


def tool_grep(pattern: str, path: Path, repo_root: Path) -> str:
    """
    Search for a pattern in files using grep.
    
    If path is a file, search only that file.
    If path is a directory, search recursively.
    """
    try:
        cmd = ["grep", "-n", "-r", "-I", pattern]
        if path.is_file():
            cmd.append(str(path))
        elif path.is_dir():
            cmd.append(str(path))
        else:
            return f"GREP_ERROR: Path does not exist: {path}\n"
        
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode == 0:
            # Limit output to avoid overwhelming context
            lines = proc.stdout.splitlines()
            if len(lines) > 100:
                output = "\n".join(lines[:100])
                output += f"\n... (showing first 100 of {len(lines)} matches)"
            else:
                output = proc.stdout
            return f"GREP_RESULT for pattern '{pattern}' in {path}:\n```\n{output}\n```\n"
        elif proc.returncode == 1:
            return f"GREP_RESULT: No matches found for pattern '{pattern}' in {path}\n"
        else:
            return f"GREP_ERROR: {proc.stderr}\n"
    except Exception as e:
        return f"GREP_ERROR: {e}\n"


def tool_find_files(pattern: str, path: Path, repo_root: Path) -> str:
    """
    Find files matching a pattern (glob-style).
    """
    try:
        cmd = ["find", str(path), "-name", pattern, "-type", "f"]
        
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode == 0:
            lines = proc.stdout.splitlines()
            if len(lines) > 200:
                output = "\n".join(lines[:200])
                output += f"\n... (showing first 200 of {len(lines)} files)"
            else:
                output = proc.stdout
            return f"FIND_FILES_RESULT for pattern '{pattern}' in {path}:\n```\n{output}\n```\n"
        else:
            return f"FIND_FILES_ERROR: {proc.stderr}\n"
    except Exception as e:
        return f"FIND_FILES_ERROR: {e}\n"


def tool_run_command(command: str, repo_root: Path, allowed_commands: List[str]) -> str:
    """
    Run a shell command in the repo root.
    
    Only whitelisted commands are allowed for security.
    """
    # Parse first word of command
    cmd_parts = command.split()
    if not cmd_parts:
        return "RUN_COMMAND_ERROR: Empty command\n"
    
    cmd_name = cmd_parts[0]
    
    # Check whitelist
    if cmd_name not in allowed_commands and not any(cmd_name.startswith(prefix) for prefix in allowed_commands):
        return (
            f"RUN_COMMAND_ERROR: Command '{cmd_name}' not in whitelist.\n"
            f"Allowed commands: {', '.join(allowed_commands)}\n"
        )
    
    try:
        proc = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=str(repo_root),
            timeout=60,  # 60 second timeout
        )
        
        output = proc.stdout
        # Limit output
        if len(output) > 5000:
            output = output[:5000] + f"\n... (output truncated, showing first 5000 chars)"
        
        return f"RUN_COMMAND_RESULT (exit code: {proc.returncode}):\n```\n{output}\n```\n"
    except subprocess.TimeoutExpired:
        return "RUN_COMMAND_ERROR: Command timed out (60s limit)\n"
    except Exception as e:
        return f"RUN_COMMAND_ERROR: {e}\n"


def tool_read_file_lines(path: Path, start: int, end: int) -> str:
    """
    Read specific lines from a file (1-indexed, inclusive).
    """
    try:
        if not path.exists():
            return f"READ_FILE_LINES_ERROR: File does not exist: {path}\n"
        if not path.is_file():
            return f"READ_FILE_LINES_ERROR: Path is not a file: {path}\n"
        
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total_lines = len(lines)
        
        # Validate range
        if start < 1 or end < 1:
            return "READ_FILE_LINES_ERROR: Line numbers must be >= 1\n"
        if start > total_lines:
            return f"READ_FILE_LINES_ERROR: Start line {start} exceeds file length {total_lines}\n"
        
        # Adjust to 0-indexed and clamp end
        start_idx = start - 1
        end_idx = min(end, total_lines)
        
        selected = lines[start_idx:end_idx]
        output = "\n".join(f"{i+start}: {line}" for i, line in enumerate(selected))
        
        return f"READ_FILE_LINES_RESULT for {path} lines {start}-{end_idx} (total: {total_lines}):\n```\n{output}\n```\n"
    except Exception as e:
        return f"READ_FILE_LINES_ERROR for {path}: {e}\n"


def tool_scan_file(path: Path) -> str:
    """
    Show file structure/outline without full content.
    
    For C files: shows function definitions, struct definitions, #defines, etc.
    For other files: shows lines that look like definitions/headers.
    
    This is useful for getting an overview of a large file before diving into details.
    """
    try:
        if not path.exists():
            return f"SCAN_FILE_ERROR: File does not exist: {path}\n"
        if not path.is_file():
            return f"SCAN_FILE_ERROR: Path is not a file: {path}\n"
        
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        total_lines = len(lines)
        
        # Patterns that indicate structure (C/C++ focused, but works for others too)
        import re
        structure_lines = []
        
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            
            # Skip empty lines and pure block markers
            if not stripped or stripped in ['{', '}', '};']:
                continue
            
            # C/C++ structure indicators
            if (
                # Function definitions (return_type function_name(...))
                re.match(r'^[a-zA-Z_][a-zA-Z0-9_*\s]+\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\([^)]*\)\s*$', stripped) or
                # struct/union/enum definitions
                re.match(r'^(struct|union|enum|typedef)\s+', stripped) or
                # #define, #include, etc.
                stripped.startswith('#') or
                # Comments that look like section headers (/* ... */ or // ...)
                (re.match(r'^/\*.*\*/$', stripped) and len(stripped) > 10) or
                (stripped.startswith('//') and len(stripped) > 20) or
                # Function prototypes (ends with semicolon after params)
                re.match(r'^[a-zA-Z_][a-zA-Z0-9_*\s]+\s+[a-zA-Z_][a-zA-Z0-9_]*\s*\([^)]*\);', stripped) or
                # Static/extern declarations
                re.match(r'^(static|extern|const|volatile)\s+', stripped)
            ):
                structure_lines.append(f"{i:5d}: {line}")
        
        if not structure_lines:
            return f"SCAN_FILE_RESULT for {path} ({total_lines} lines): No clear structure detected.\nUse READ_FILE_LINES to read specific sections.\n"
        
        # Limit output
        if len(structure_lines) > 200:
            output = "\n".join(structure_lines[:200])
            output += f"\n... (showing first 200 of {len(structure_lines)} structure lines)"
        else:
            output = "\n".join(structure_lines)
        
        return f"SCAN_FILE_RESULT for {path} ({total_lines} lines, showing {min(len(structure_lines), 200)} structure lines):\n```\n{output}\n```\n"
    except Exception as e:
        return f"SCAN_FILE_ERROR for {path}: {e}\n"


def tool_git_status(repo_root: Path) -> str:
    """
    Run git status.
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--short"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode == 0:
            return f"GIT_STATUS_RESULT:\n```\n{proc.stdout}\n```\n"
        else:
            return f"GIT_STATUS_ERROR: {proc.stderr}\n"
    except Exception as e:
        return f"GIT_STATUS_ERROR: {e}\n"


def tool_git_diff(path: Optional[str], repo_root: Path) -> str:
    """
    Show git diff for a specific path or all changes.
    """
    try:
        cmd = ["git", "diff"]
        if path:
            cmd.append(path)
        
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode == 0:
            output = proc.stdout
            if len(output) > 10000:
                output = output[:10000] + "\n... (diff truncated, showing first 10000 chars)"
            return f"GIT_DIFF_RESULT:\n```\n{output}\n```\n"
        else:
            return f"GIT_DIFF_ERROR: {proc.stderr}\n"
    except Exception as e:
        return f"GIT_DIFF_ERROR: {e}\n"


def tool_git_commit(message: str, repo_root: Path) -> str:
    """
    Commit all changes with a message.
    """
    try:
        # First add all changes
        proc = subprocess.run(
            ["git", "add", "-A"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode != 0:
            return f"GIT_COMMIT_ERROR (git add): {proc.stderr}\n"
        
        # Then commit
        proc = subprocess.run(
            ["git", "commit", "-m", message],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode == 0:
            return f"GIT_COMMIT_OK:\n```\n{proc.stdout}\n```\n"
        else:
            return f"GIT_COMMIT_ERROR: {proc.stderr}\n"
    except Exception as e:
        return f"GIT_COMMIT_ERROR: {e}\n"


def tool_show_diff(path: Path, repo_root: Path) -> str:
    """
    Show git diff for a specific file (what changed since last commit).
    """
    return tool_git_diff(str(path.relative_to(repo_root)), repo_root)


def tool_edit_multiple(edits_json: str, repo_root: Path) -> str:
    """
    Apply multiple edits at once from a JSON array.
    
    Format: [{"file": "path", "old": "...", "new": "..."}, ...]
    """
    try:
        import json
        edits = json.loads(edits_json)
        
        if not isinstance(edits, list):
            return "EDIT_MULTIPLE_ERROR: JSON must be an array of edit objects\n"
        
        results = []
        for i, edit in enumerate(edits):
            if not isinstance(edit, dict):
                return f"EDIT_MULTIPLE_ERROR: Edit {i} is not an object\n"
            
            if "file" not in edit or "old" not in edit or "new" not in edit:
                return f"EDIT_MULTIPLE_ERROR: Edit {i} missing required fields (file, old, new)\n"
            
            file_path = (repo_root / edit["file"]).resolve()
            if not str(file_path).startswith(str(repo_root)):
                return f"EDIT_MULTIPLE_ERROR: Edit {i} path escapes repo root: {edit['file']}\n"
            
            result = tool_edit_file(file_path, edit["old"], edit["new"])
            results.append(f"Edit {i} ({edit['file']}): {result.strip()}")
        
        return "EDIT_MULTIPLE_RESULT:\n" + "\n".join(results) + "\n"
    except json.JSONDecodeError as e:
        return f"EDIT_MULTIPLE_ERROR: Invalid JSON: {e}\n"
    except Exception as e:
        return f"EDIT_MULTIPLE_ERROR: {e}\n"


def tool_undo_last_edit(repo_root: Path) -> str:
    """
    Undo the last edit by reverting to HEAD.
    """
    try:
        proc = subprocess.run(
            ["git", "checkout", "HEAD", "."],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode == 0:
            return "UNDO_LAST_EDIT_OK: Reverted all changes to last commit\n"
        else:
            return f"UNDO_LAST_EDIT_ERROR: {proc.stderr}\n"
    except Exception as e:
        return f"UNDO_LAST_EDIT_ERROR: {e}\n"


def tool_restore_file(path: Path, repo_root: Path) -> str:
    """
    Restore a specific file to its last committed version.
    """
    try:
        relative_path = path.relative_to(repo_root)
        proc = subprocess.run(
            ["git", "checkout", "HEAD", str(relative_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode == 0:
            return f"RESTORE_FILE_OK: Restored {path} to last commit\n"
        else:
            return f"RESTORE_FILE_ERROR: {proc.stderr}\n"
    except Exception as e:
        return f"RESTORE_FILE_ERROR: {e}\n"


def tool_find_definition(symbol: str, path: Path, repo_root: Path) -> str:
    """
    Find definition of a symbol (function, struct, etc.) using grep heuristics.
    
    Looks for common C definition patterns.
    """
    try:
        # Try different patterns for C definitions
        patterns = [
            f"^[a-zA-Z_][a-zA-Z0-9_* ]*\\s+{symbol}\\s*\\(",  # function definition
            f"^struct\\s+{symbol}\\s*{{",  # struct definition
            f"^typedef\\s+.*\\s+{symbol};",  # typedef
            f"^#define\\s+{symbol}\\b",  # macro definition
        ]
        
        results = []
        for pattern in patterns:
            cmd = ["grep", "-n", "-E", pattern]
            if path.is_file():
                cmd.append(str(path))
            elif path.is_dir():
                cmd.extend(["-r", str(path)])
            else:
                continue
            
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                cwd=str(repo_root),
            )
            
            if proc.returncode == 0 and proc.stdout.strip():
                results.append(proc.stdout.strip())
        
        if results:
            combined = "\n".join(results)
            lines = combined.splitlines()
            if len(lines) > 50:
                combined = "\n".join(lines[:50]) + f"\n... (showing first 50 of {len(lines)} matches)"
            return f"FIND_DEFINITION_RESULT for '{symbol}' in {path}:\n```\n{combined}\n```\n"
        else:
            return f"FIND_DEFINITION_RESULT: No definition found for '{symbol}' in {path}\n"
    except Exception as e:
        return f"FIND_DEFINITION_ERROR: {e}\n"


def tool_find_references(symbol: str, path: Path, repo_root: Path) -> str:
    """
    Find all references to a symbol using grep.
    """
    try:
        cmd = ["grep", "-n", "-w", symbol]
        if path.is_file():
            cmd.append(str(path))
        elif path.is_dir():
            cmd.extend(["-r", "-I", str(path)])
        else:
            return f"FIND_REFERENCES_ERROR: Path does not exist: {path}\n"
        
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(repo_root),
        )
        
        if proc.returncode == 0:
            lines = proc.stdout.splitlines()
            if len(lines) > 100:
                output = "\n".join(lines[:100])
                output += f"\n... (showing first 100 of {len(lines)} matches)"
            else:
                output = proc.stdout
            return f"FIND_REFERENCES_RESULT for '{symbol}' in {path}:\n```\n{output}\n```\n"
        elif proc.returncode == 1:
            return f"FIND_REFERENCES_RESULT: No references found for '{symbol}' in {path}\n"
        else:
            return f"FIND_REFERENCES_ERROR: {proc.stderr}\n"
    except Exception as e:
        return f"FIND_REFERENCES_ERROR: {e}\n"


def tool_check_syntax(path: Path, repo_root: Path) -> str:
    """
    Check syntax of a C file using gcc -fsyntax-only.
    """
    try:
        if not path.exists():
            return f"CHECK_SYNTAX_ERROR: File does not exist: {path}\n"
        if not path.is_file():
            return f"CHECK_SYNTAX_ERROR: Path is not a file: {path}\n"
        
        # Try gcc first, fall back to clang
        for compiler in ["gcc", "clang"]:
            try:
                proc = subprocess.run(
                    [compiler, "-fsyntax-only", "-std=c99", str(path)],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    cwd=str(repo_root),
                    timeout=10,
                )
                
                if proc.returncode == 0:
                    return f"CHECK_SYNTAX_OK: No syntax errors in {path}\n"
                else:
                    output = proc.stderr
                    if len(output) > 2000:
                        output = output[:2000] + "\n... (output truncated)"
                    return f"CHECK_SYNTAX_RESULT for {path}:\n```\n{output}\n```\n"
            except FileNotFoundError:
                continue
            except subprocess.TimeoutExpired:
                return "CHECK_SYNTAX_ERROR: Compilation timed out (10s limit)\n"
        
        return "CHECK_SYNTAX_ERROR: No C compiler (gcc/clang) found\n"
    except Exception as e:
        return f"CHECK_SYNTAX_ERROR: {e}\n"


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
        "  ACTION: GREP pattern relative/path\n"
        "    - Search for a pattern in files (file or directory)\n"
        "    - Example: ACTION: GREP \"security_check\" bin/pkill\n\n"
        "  ACTION: FIND_FILES pattern relative/path\n"
        "    - Find files matching a glob pattern\n"
        "    - Example: ACTION: FIND_FILES \"*.c\" bin\n\n"
        "  ACTION: READ_FILE_LINES relative/path/to/file start end\n"
        "    - Read specific line range (1-indexed, inclusive)\n"
        "    - Example: ACTION: READ_FILE_LINES bin/pkill/pkill.c 100 150\n"
        "    - IMPORTANT: Use this for large files to avoid context overflow\n\n"
        "  ACTION: SCAN_FILE relative/path/to/file\n"
        "    - Show file structure/outline without full content\n"
        "    - Shows function definitions, structs, #defines, section comments\n"
        "    - Use this FIRST for large files to get an overview\n"
        "    - Then use READ_FILE_LINES to examine specific sections\n"
        "    - Example: ACTION: SCAN_FILE bin/pkill/pkill.c\n\n"
        "  ACTION: RUN_COMMAND\n"
        "  <<<\n"
        "  command to run\n"
        "  >>>\n"
        "    - Execute a shell command (whitelisted commands only)\n"
        "    - Default whitelist: make, gcc, clang, python, python3, pytest, sh, bash\n"
        "    - Use for testing, building, syntax checking\n\n"
        "  ACTION: SHOW_DIFF relative/path/to/file\n"
        "    - Show git diff for a file (what changed since last commit)\n\n"
        "  ACTION: GIT_STATUS\n"
        "    - Show git status (modified/untracked files)\n\n"
        "  ACTION: GIT_DIFF [relative/path]\n"
        "    - Show git diff for a file or all changes\n\n"
        "  ACTION: GIT_COMMIT\n"
        "  <<<\n"
        "  commit message\n"
        "  >>>\n"
        "    - Commit all changes with a message\n\n"
        "  ACTION: EDIT_MULTIPLE\n"
        "  <<<\n"
        "  [{\"file\": \"path1\", \"old\": \"...\", \"new\": \"...\"},\n"
        "   {\"file\": \"path2\", \"old\": \"...\", \"new\": \"...\"}]\n"
        "  >>>\n"
        "    - Apply multiple edits at once (JSON format)\n\n"
        "  ACTION: UNDO_LAST_EDIT\n"
        "    - Revert all changes to last commit (git checkout HEAD .)\n\n"
        "  ACTION: RESTORE_FILE relative/path/to/file\n"
        "    - Restore a specific file to last committed version\n\n"
        "  ACTION: FIND_DEFINITION symbol relative/path\n"
        "    - Find where a symbol (function, struct, etc.) is defined\n"
        "    - Example: ACTION: FIND_DEFINITION process_args bin/pkill\n\n"
        "  ACTION: FIND_REFERENCES symbol relative/path\n"
        "    - Find all uses of a symbol\n"
        "    - Example: ACTION: FIND_REFERENCES process_args bin/pkill\n\n"
        "  ACTION: CHECK_SYNTAX relative/path/to/file.c\n"
        "    - Check C syntax using gcc/clang\n\n"
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
        "- For LARGE files (>8K chars): Use SCAN_FILE first, then READ_FILE_LINES for specific sections.\n"
        "  This prevents context overflow and ensures you can review files methodically.\n"
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


def commit_and_push_changes(repo_root: Path, commit_msg: str, timeout: int = 300) -> tuple[bool, str]:
    """
    Commit all changes and push to remote.
    
    Returns: (success, output/error message)
    """
    try:
        # Stage all changes
        result = subprocess.run(
            ["git", "add", "-A"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            return False, f"git add failed: {result.stderr}"
        
        # Commit with [AI-REVIEW] prefix
        full_msg = f"[AI-REVIEW] {commit_msg}"
        result = subprocess.run(
            ["git", "commit", "-m", full_msg],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            # Check if it's just "nothing to commit"
            if "nothing to commit" in result.stdout.lower():
                return True, "Nothing to commit"
            return False, f"git commit failed: {result.stderr}"
        
        # Push
        result = subprocess.run(
            ["git", "push"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if result.returncode != 0:
            return False, f"git push failed: {result.stderr}"
        
        return True, f"Committed and pushed: {full_msg}"
    
    except subprocess.TimeoutExpired:
        return False, f"git operation timed out after {timeout} seconds"
    except Exception as e:
        return False, f"git operation failed: {e}"


def run_validation_command(cmd: str, timeout: int = 300) -> tuple[bool, str]:
    """
    Run SSH validation command.
    
    Returns: (success, output)
    """
    if not cmd or not cmd.strip():
        return True, "Validation disabled (no command configured)"
    
    try:
        print(f"[VALIDATION] Running: {cmd}", file=sys.stderr)
        sys.stderr.flush()
        
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        
        output = f"STDOUT:\n{result.stdout}\n\nSTDERR:\n{result.stderr}"
        success = result.returncode == 0
        
        return success, output
    
    except subprocess.TimeoutExpired:
        return False, f"Validation command timed out after {timeout} seconds"
    except Exception as e:
        return False, f"Validation command failed: {e}"


def prune_history(history: List[Dict[str, str]], max_turns: int = 15) -> List[Dict[str, str]]:
    """
    Prune conversation history to prevent context overflow.
    
    Strategy:
    - Always keep messages at index 0 and 1 (system prompt + bootstrap)
    - Keep only the last max_turns conversation turns
    - Each turn = 1 assistant message + 1 user message
    
    Args:
        history: Full conversation history
        max_turns: Maximum number of recent turns to keep (default: 15)
    
    Returns:
        Pruned history
    """
    if len(history) <= 2:
        return history
    
    # Keep system prompt and bootstrap (indices 0, 1)
    essential = history[:2]
    
    # Get conversation turns (everything after bootstrap)
    conversation = history[2:]
    
    # Keep last max_turns * 2 messages (each turn has assistant + user message)
    max_messages = max_turns * 2
    if len(conversation) > max_messages:
        # Add a summary message indicating we pruned history
        pruned_count = len(conversation) - max_messages
        summary = {
            "role": "user",
            "content": (
                f"[CONTEXT MANAGEMENT: Pruned {pruned_count} older messages to prevent context overflow. "
                f"Keeping last {max_turns} conversation turns. System prompt and bootstrap remain intact.]"
            )
        }
        recent = conversation[-max_messages:]
        return essential + [summary] + recent
    
    return history


def agent_loop(
    repo_root: Path,
    bootstrap_path: Path,
    llm: LocalLLM,
    max_steps: int = 100,
    allowed_commands: Optional[List[str]] = None,
    ssh_validation_cmd: str = "",
) -> None:
    if allowed_commands is None:
        allowed_commands = ["make", "gcc", "clang", "python", "python3", "pytest", "sh", "bash"]
    logs_dir = ensure_logs_dir(repo_root)
    
    # Track if we need validation after this step
    needs_validation = False
    last_change_description = ""

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
        
        # Prune history to prevent context overflow (keep last 15 turns = 30 messages)
        # This ensures we stay well under the 32K token limit
        history = prune_history(history, max_turns=15)
        print(f"[AGENT] Context: {len(history)} messages in history", file=sys.stderr)
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
                
                # Mark for validation if successful
                if "EDIT_FILE_OK" in result:
                    needs_validation = True
                    last_change_description = f"Edited {arg}"
            except ValueError as e:
                result = f"EDIT_FILE_ERROR: {e}\n"

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            
            # Don't continue here - fall through to validation if needed
            if not needs_validation:
                continue

        elif action == "WRITE_FILE":
            try:
                target = resolve_repo_path(arg, repo_root)
                print(f"[AGENT TOOL] WRITE_FILE {target}", file=sys.stderr)
                print(f"[AGENT TOOL] Content length: {len(parsed.content or '')}", file=sys.stderr)
                result = tool_write_file(target, parsed.content or "")
                
                # Mark for validation if successful
                if "WRITE_FILE_OK" in result:
                    needs_validation = True
                    last_change_description = f"Wrote {arg}"
            except ValueError as e:
                result = f"WRITE_FILE_ERROR: {e}\n"

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            
            # Don't continue here - fall through to validation if needed
            if not needs_validation:
                continue

        elif action == "GREP":
            # Format: ACTION: GREP pattern path
            parts = arg.split(None, 1)
            if len(parts) < 2:
                result = "GREP_ERROR: Usage: ACTION: GREP pattern path\n"
            else:
                pattern, path_str = parts
                target = (repo_root / path_str).resolve()
                if not str(target).startswith(str(repo_root)):
                    result = f"GREP_ERROR: Path escapes repo root: {path_str}\n"
                else:
                    print(f"[AGENT TOOL] GREP '{pattern}' in {target}", file=sys.stderr)
                    result = tool_grep(pattern, target, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result[:2000] if len(result) > 2000 else result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "FIND_FILES":
            # Format: ACTION: FIND_FILES pattern path
            parts = arg.split(None, 1)
            if len(parts) < 2:
                result = "FIND_FILES_ERROR: Usage: ACTION: FIND_FILES pattern path\n"
            else:
                pattern, path_str = parts
                target = (repo_root / path_str).resolve()
                if not str(target).startswith(str(repo_root)):
                    result = f"FIND_FILES_ERROR: Path escapes repo root: {path_str}\n"
                else:
                    print(f"[AGENT TOOL] FIND_FILES '{pattern}' in {target}", file=sys.stderr)
                    result = tool_find_files(pattern, target, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result[:2000] if len(result) > 2000 else result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "READ_FILE_LINES":
            # Format: ACTION: READ_FILE_LINES path start end
            parts = arg.split(None, 2)
            if len(parts) < 3:
                result = "READ_FILE_LINES_ERROR: Usage: ACTION: READ_FILE_LINES path start end\n"
            else:
                path_str, start_str, end_str = parts
                try:
                    start = int(start_str)
                    end = int(end_str)
                    target = (repo_root / path_str).resolve()
                    if not str(target).startswith(str(repo_root)):
                        result = f"READ_FILE_LINES_ERROR: Path escapes repo root: {path_str}\n"
                    else:
                        print(f"[AGENT TOOL] READ_FILE_LINES {target} {start}-{end}", file=sys.stderr)
                        result = tool_read_file_lines(target, start, end)
                except ValueError:
                    result = "READ_FILE_LINES_ERROR: start and end must be integers\n"

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "SCAN_FILE":
            try:
                target = resolve_repo_path(arg, repo_root)
                print(f"[AGENT TOOL] SCAN_FILE {target}", file=sys.stderr)
                result = tool_scan_file(target)
            except ValueError as e:
                result = f"SCAN_FILE_ERROR: {e}\n"

            print("[AGENT TOOL RESULT BEGIN]")
            print(result[:5000] if len(result) > 5000 else result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "RUN_COMMAND":
            command = parsed.content or ""
            print(f"[AGENT TOOL] RUN_COMMAND: {command[:100]}", file=sys.stderr)
            result = tool_run_command(command, repo_root, allowed_commands)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "SHOW_DIFF":
            target = (repo_root / arg).resolve()
            if not str(target).startswith(str(repo_root)):
                result = f"SHOW_DIFF_ERROR: Path escapes repo root: {arg}\n"
            else:
                print(f"[AGENT TOOL] SHOW_DIFF {target}", file=sys.stderr)
                result = tool_show_diff(target, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "GIT_STATUS":
            print("[AGENT TOOL] GIT_STATUS", file=sys.stderr)
            result = tool_git_status(repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "GIT_DIFF":
            path_str = arg if arg else None
            print(f"[AGENT TOOL] GIT_DIFF {path_str or '(all)'}", file=sys.stderr)
            result = tool_git_diff(path_str, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "GIT_COMMIT":
            message = parsed.content or ""
            print(f"[AGENT TOOL] GIT_COMMIT", file=sys.stderr)
            result = tool_git_commit(message, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "EDIT_MULTIPLE":
            edits_json = parsed.content or ""
            print(f"[AGENT TOOL] EDIT_MULTIPLE", file=sys.stderr)
            result = tool_edit_multiple(edits_json, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "UNDO_LAST_EDIT":
            print("[AGENT TOOL] UNDO_LAST_EDIT", file=sys.stderr)
            result = tool_undo_last_edit(repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "RESTORE_FILE":
            target = (repo_root / arg).resolve()
            if not str(target).startswith(str(repo_root)):
                result = f"RESTORE_FILE_ERROR: Path escapes repo root: {arg}\n"
            else:
                print(f"[AGENT TOOL] RESTORE_FILE {target}", file=sys.stderr)
                result = tool_restore_file(target, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "FIND_DEFINITION":
            # Format: ACTION: FIND_DEFINITION symbol path
            parts = arg.split(None, 1)
            if len(parts) < 2:
                result = "FIND_DEFINITION_ERROR: Usage: ACTION: FIND_DEFINITION symbol path\n"
            else:
                symbol, path_str = parts
                target = (repo_root / path_str).resolve()
                if not str(target).startswith(str(repo_root)):
                    result = f"FIND_DEFINITION_ERROR: Path escapes repo root: {path_str}\n"
                else:
                    print(f"[AGENT TOOL] FIND_DEFINITION '{symbol}' in {target}", file=sys.stderr)
                    result = tool_find_definition(symbol, target, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result[:2000] if len(result) > 2000 else result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "FIND_REFERENCES":
            # Format: ACTION: FIND_REFERENCES symbol path
            parts = arg.split(None, 1)
            if len(parts) < 2:
                result = "FIND_REFERENCES_ERROR: Usage: ACTION: FIND_REFERENCES symbol path\n"
            else:
                symbol, path_str = parts
                target = (repo_root / path_str).resolve()
                if not str(target).startswith(str(repo_root)):
                    result = f"FIND_REFERENCES_ERROR: Path escapes repo root: {path_str}\n"
                else:
                    print(f"[AGENT TOOL] FIND_REFERENCES '{symbol}' in {target}", file=sys.stderr)
                    result = tool_find_references(symbol, target, repo_root)

            print("[AGENT TOOL RESULT BEGIN]")
            print(result[:2000] if len(result) > 2000 else result)
            print("[AGENT TOOL RESULT END]")
            sys.stdout.flush()
            sys.stderr.flush()

            history.append({"role": "user", "content": result})
            continue

        elif action == "CHECK_SYNTAX":
            target = (repo_root / arg).resolve()
            if not str(target).startswith(str(repo_root)):
                result = f"CHECK_SYNTAX_ERROR: Path escapes repo root: {arg}\n"
            else:
                print(f"[AGENT TOOL] CHECK_SYNTAX {target}", file=sys.stderr)
                result = tool_check_syntax(target, repo_root)

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
                        "Valid actions are: READ_FILE, LIST_DIR, EDIT_FILE, WRITE_FILE, "
                        "GREP, FIND_FILES, READ_FILE_LINES, SCAN_FILE, RUN_COMMAND, SHOW_DIFF, "
                        "GIT_STATUS, GIT_DIFF, GIT_COMMIT, EDIT_MULTIPLE, UNDO_LAST_EDIT, "
                        "RESTORE_FILE, FIND_DEFINITION, FIND_REFERENCES, CHECK_SYNTAX, "
                        "APPLY_PATCH, HALT.\n"
                        "Respond again with a valid ACTION line as your FINAL line."
                    ),
                }
            )
            continue
        
        # Validation loop: after mutagenic changes (EDIT_FILE, WRITE_FILE)
        if needs_validation and ssh_validation_cmd:
            print(f"[AGENT] Mutagenic change detected, starting validation loop", file=sys.stderr)
            sys.stderr.flush()
            
            max_validation_retries = 3
            for retry in range(max_validation_retries):
                # Commit and push changes
                print(f"[AGENT] Committing and pushing changes (attempt {retry + 1}/{max_validation_retries})", file=sys.stderr)
                commit_success, commit_msg = commit_and_push_changes(repo_root, last_change_description)
                
                if not commit_success:
                    print(f"[AGENT] WARNING: {commit_msg}", file=sys.stderr)
                    if "timed out" in commit_msg.lower():
                        # Timeout, log and continue without validation
                        print(f"[AGENT] Skipping validation due to commit timeout", file=sys.stderr)
                        break
                    # Other commit errors, try to continue
                
                print(f"[AGENT] {commit_msg}", file=sys.stderr)
                sys.stderr.flush()
                
                # Run validation
                validation_success, validation_output = run_validation_command(ssh_validation_cmd)
                
                if "timed out" in validation_output.lower():
                    print(f"[AGENT] WARNING: Validation timed out, continuing anyway", file=sys.stderr)
                    sys.stderr.flush()
                    break
                
                if validation_success:
                    print(f"[AGENT] ✓ Validation passed!", file=sys.stderr)
                    sys.stderr.flush()
                    # Tell the model about success
                    history.append({
                        "role": "user",
                        "content": f"VALIDATION_SUCCESS: Changes committed and validated successfully.\n{validation_output[:1000]}"
                    })
                    break
                else:
                    print(f"[AGENT] ✗ Validation failed (attempt {retry + 1}/{max_validation_retries})", file=sys.stderr)
                    sys.stderr.flush()
                    
                    # Feed errors back to model for fixing
                    history.append({
                        "role": "user",
                        "content": (
                            f"VALIDATION_FAILED: The changes you made caused build/test errors.\n\n"
                            f"Please analyze the errors below and fix them:\n\n"
                            f"```\n{validation_output[:5000]}\n```\n\n"
                            f"Respond with an ACTION to fix the issues."
                        )
                    })
                    
                    if retry < max_validation_retries - 1:
                        # Let the model try to fix it
                        print(f"[AGENT] Asking model to fix validation errors...", file=sys.stderr)
                        sys.stderr.flush()
                        break  # Exit validation loop, let model respond
                    else:
                        print(f"[AGENT] Max validation retries reached, continuing anyway", file=sys.stderr)
                        sys.stderr.flush()
            
            # Reset validation flag
            needs_validation = False
            last_change_description = ""

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
    parser.add_argument(
        "--allowed-commands",
        nargs="+",
        default=["make", "gcc", "clang", "python", "python3", "pytest", "sh", "bash"],
        help="Whitelist of commands allowed for RUN_COMMAND action",
    )
    parser.add_argument(
        "--ssh-validation-cmd",
        default="",
        help="SSH command to run after mutagenic changes for validation (empty = disabled)",
    )

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
        allowed_commands=args.allowed_commands,
        ssh_validation_cmd=args.ssh_validation_cmd,
    )


if __name__ == "__main__":
    main()
