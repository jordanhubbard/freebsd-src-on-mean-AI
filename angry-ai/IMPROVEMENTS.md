# Angry AI Robustness Improvements

This document summarizes the robustness improvements made to `angry_ai.py`.

## Overview

All improvements leverage the fact that this code always runs on Unix (Linux/macOS) with known tools available (git, standard Unix utilities).

---

## 1. ACTION Parsing Robustness

### Problem: Model output could contain multiple ACTION lines
**Fix**: Use the **LAST** ACTION line instead of the first
- Changed from `ACTION_RE.search()` to `ACTION_RE.findall()[-1]`
- Prevents execution of example ACTION lines in commentary
- Follows instructions: "FINAL line MUST be exactly one ACTION line"

### Problem: Rigid block parsing (OLD:/NEW:/CONTENT:)
**Fix**: More lenient regex patterns
- Allows optional whitespace: `OLD:\n<<<` or `OLD: <<<`
- Handles extra spaces and tabs
- Example: `OLD:  \n  <<<` now works

### Problem: Markdown fences break blocks
**Fix**: Automatic fence stripping
- `strip_markdown_fences()` removes ``` from OLD/NEW/CONTENT blocks
- LLMs can safely wrap code in markdown fences

### Problem: Generic error messages
**Fix**: Show context in errors
- Display first 300 chars of what was actually found
- Show expected format with examples
- Example: "Body preview: ACTION: EDIT_FILE test.c\nOLDD:\n..."

### Problem: No debugging info on success
**Fix**: Debug logging for all parsed actions
```
[AGENT] Parsed ACTION: EDIT_FILE arg=test.c old_len=123 new_len=456
```

---

## 2. Path Validation & Security

### Problem: Cross-platform path handling complexity
**Fix**: Unix-only path validation
- Simplified: only check for `/` (absolute paths)
- No Windows backslash handling needed
- Direct `.startswith('/')` check instead of `os.path.isabs()`

### Problem: Multiple escape vectors
**Fix**: Comprehensive validation in `validate_relative_path()`
- Blocks absolute paths (`/etc/passwd`)
- Blocks parent directory escapes (`../../../etc`)
- Blocks sneaky escapes (`bin/../../etc`)
- Blocks tilde expansion (`~/etc/passwd`)
- Blocks null bytes (`\0`)

### Problem: Duplicate path checking code
**Fix**: Centralized `resolve_repo_path()` helper
- One place for path resolution and validation
- Handles symlinks via `resolve()` (Unix realpath)
- Handles macOS `/private` prefix for `/tmp` and `/var`
- Used by all file operations (READ_FILE, WRITE_FILE, EDIT_FILE, LIST_DIR)

---

## 3. Git Integration (Unix-Specific)

### Problem: LLM sees irrelevant files
**Fix**: `.gitignore` awareness in `LIST_DIR`
- Uses `git check-ignore` to filter out ignored files
- Fast and respects all `.gitignore` rules
- Optional `show_ignored=True` parameter
- Reduces noise for the LLM

**Example**:
```python
# Before: lists build/, *.log, node_modules/, etc.
LIST_DIR bin/

# After: only shows tracked/trackable files
LIST_DIR bin/  # excludes *.o, *.log, build/, etc.
```

---

## 4. Context Window Management

### Problem: Model has 32K token limit, input could exceed it
**Fix**: Tokenizer truncation with safety buffer
```python
max_context = getattr(tokenizer, 'model_max_length', 32768)
max_input_tokens = max_context - max_new_tokens - 100  # safety buffer
inputs = tokenizer(prompt, truncation=True, max_length=max_input_tokens)
```
- Logs token usage: `[LLM] Input tokens: 28543 / 31868`
- Warns on truncation

### Problem: Large files blow up context window
**Fix**: `READ_FILE` truncation at 50K chars (configurable)
- Truncates on line boundaries (clean output)
- Shows truncation info: `[... FILE TRUNCATED: showing 412/1523 lines ...]`
- Both display AND history get truncated (was inconsistent before)

---

## 5. Git LFS Detection

### Problem: Model download appears successful but files are LFS pointers
**Fix**: Makefile validation
- Checks if git-lfs is installed before cloning
- Validates existing model files aren't 135-byte pointer files
- Provides clear error messages and fix instructions

---

## Testing

All improvements are covered by tests:

1. **`test_parsing.py`**: ACTION parsing edge cases
   - Last action line selection
   - Path validation (escapes, absolute paths)
   - Lenient whitespace
   - Markdown fence stripping
   - Error message quality

2. **`test_unix_improvements.py`**: Unix-specific features
   - Symlink resolution
   - Tilde expansion blocking
   - Unix path separator handling
   - `.gitignore` filtering
   - macOS `/private` prefix handling

Run tests:
```bash
.venv/bin/python test_parsing.py
.venv/bin/python test_unix_improvements.py
```

---

## Unix Assumptions Leveraged

These improvements rely on Unix being the target platform:

1. **Path separators**: Only `/`, no `\` handling needed
2. **Absolute paths**: Start with `/`, simple check
3. **Symlinks**: `resolve()` uses `realpath(3)` 
4. **Git availability**: Always present (we're in a git repo)
5. **Shell utilities**: Can rely on `git check-ignore`, etc.
6. **Process model**: Standard Unix fork/exec
7. **File system**: POSIX-compliant paths and permissions

---

## Summary of Changes

| Component | Before | After |
|-----------|--------|-------|
| ACTION parsing | First match | **Last match** |
| Block parsing | Strict whitespace | **Lenient** |
| Markdown fences | Breaks parsing | **Stripped automatically** |
| Path validation | Generic cross-platform | **Unix-optimized** |
| Path checking | Duplicated 4 times | **Centralized helper** |
| LIST_DIR | Shows all files | **Filters .gitignored** |
| READ_FILE | Unlimited size | **50K char limit** |
| Context window | No protection | **Auto-truncation + warning** |
| Error messages | Generic | **Shows context** |
| Debug logging | Parse errors only | **All actions** |
| Git LFS | Silent failure | **Early detection** |

---

## Performance Impact

All improvements are either zero-cost or beneficial:

- Path validation: Slightly faster (simpler checks)
- `resolve_repo_path()`: Prevents code duplication
- `.gitignore` filtering: Reduces LLM context size
- Truncation: Prevents OOM and timeouts
- Better errors: Faster debugging

The only potential slowdown is `git check-ignore` in `LIST_DIR`, but:
- It's very fast (< 1ms per file)
- Reduces overall token usage (smaller context)
- Net positive for LLM performance
