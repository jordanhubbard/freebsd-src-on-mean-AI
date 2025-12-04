# SSH Validation Feature

## Overview

The Angry AI now includes an automatic validation loop that commits, pushes, and validates changes after every mutagenic operation (EDIT_FILE, WRITE_FILE). This creates a self-healing loop where the AI can detect and fix build errors automatically.

## How It Works

### 1. After Mutagenic Changes

When the AI executes `EDIT_FILE` or `WRITE_FILE` successfully, it:
- Marks that validation is needed
- Stores a description of the change

### 2. Commit and Push

Before validation runs:
```bash
git add -A
git commit -m "[AI-REVIEW] <change description>"
git push
```

All AI-generated commits are prefixed with `[AI-REVIEW]` for easy identification.

### 3. Run Validation Command

The SSH validation command is executed (default: 300 second timeout):
```bash
ssh freebsd.local "cd Src/freebsd-src-on-angry-AI && git pull && make buildworld"
```

### 4. Self-Healing Loop

- **Success**: AI is notified and continues with next task
- **Failure**: Build errors are fed back to the AI for fixing
- **Timeout**: Warning logged, AI continues anyway
- **Max Retries**: 3 attempts, then continues regardless

## Configuration

### Makefile Variable

```makefile
SSH_VALIDATION_CMD ?= ssh freebsd.local "cd Src/freebsd-src-on-angry-AI && git pull && make buildworld"
```

Set to empty string to disable:
```bash
make run SSH_VALIDATION_CMD=""
```

### Command Line

```bash
python angry_ai.py --ssh-validation-cmd "ssh host 'cd path && make test'"
```

### Timeouts

- **Git operations**: 5 minutes (300 seconds)
- **Validation command**: 5 minutes (300 seconds)

Both timeouts are configurable in the source code if needed.

## Example Workflow

1. AI edits `sys/kern/vfs_syscalls.c` to fix a bug
2. System commits: `[AI-REVIEW] Edited sys/kern/vfs_syscalls.c`
3. System pushes to remote
4. SSH command runs: `ssh freebsd.local "cd ... && make buildworld"`
5. Build succeeds → AI continues to next task
6. **OR** Build fails → AI receives error log
7. AI analyzes errors and makes fix
8. Loop repeats until build succeeds (max 3 attempts)

## Error Handling

### Git Commit Failures

- **"nothing to commit"**: Treated as success
- **Timeout**: Logged, validation skipped
- **Other errors**: Logged, but validation still attempted

### Validation Failures

Errors are truncated to 5000 chars and fed to model:

```
VALIDATION_FAILED: The changes you made caused build/test errors.

Please analyze the errors below and fix them:

```
<build errors>
```

Respond with an ACTION to fix the issues.
```

### Timeouts

If git or validation times out after 5 minutes:
- Warning is logged to stderr
- System continues without blocking
- No infinite waiting

## Benefits

1. **Automatic Quality Control**: Every change is validated before proceeding
2. **Self-Healing**: AI automatically fixes its own mistakes
3. **Traceable**: All changes have git commits with `[AI-REVIEW]` prefix
4. **Non-Blocking**: Timeouts prevent infinite waits
5. **Configurable**: Easy to customize validation command or disable entirely

## Limitations

1. **Network Dependency**: Requires SSH access to build machine
2. **Build Time**: Adds build time to each change (mitigated by timeout)
3. **Max Retries**: After 3 failed attempts, continues anyway
4. **Simple Error Parsing**: Doesn't parse structured build output (just raw text)

## Future Improvements

- Parse structured build output (JSON, XML)
- Different validation commands for different file types
- Parallel builds on multiple hosts
- Smarter error extraction (show only relevant errors)
- Caching: skip validation if change is in comments/docs only
- Incremental builds instead of full buildworld

## Example Configuration

### FreeBSD Kernel Development

```makefile
SSH_VALIDATION_CMD = ssh freebsd.local "cd /usr/src && git pull && make -j16 kernel"
```

### Python Project

```makefile
SSH_VALIDATION_CMD = ssh devbox "cd /app && git pull && pytest tests/ && mypy ."
```

### Disabled

```makefile
SSH_VALIDATION_CMD =
```

Or:
```bash
make run SSH_VALIDATION_CMD=""
```

## Logs

Validation activity is logged to stderr:

```
[AGENT] Mutagenic change detected, starting validation loop
[AGENT] Committing and pushing changes (attempt 1/3)
[AGENT] Committed and pushed: [AI-REVIEW] Edited sys/kern/vfs_syscalls.c
[VALIDATION] Running: ssh freebsd.local "cd Src/... && make buildworld"
[AGENT] ✓ Validation passed!
```

Or on failure:
```
[AGENT] ✗ Validation failed (attempt 1/3)
[AGENT] Asking model to fix validation errors...
```
