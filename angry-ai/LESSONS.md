# 🧠 Angry AI: Lessons Learned & Technical Wisdom

*Cumulative knowledge from the ongoing audit of the FreeBSD source tree.*

## 1. The "Assumption of Safety" Fallacy
**Case Study:** `bin/hostname/hostname.c`
- **The Bug:** Buffer overrun.
- **The Cause:** Developer assumed `gethostname()` always null-terminates.
- **The Reality:** `man 3 gethostname` explicitly says it *does not* if truncated.
- **Lesson:** **NEVER assume a C standard library function does what you think it does.** Read the man page.

## 2. The "It Works on My Machine" Trap
**Case Study:** `bin/echo/echo.c`
- **The Bug:** Missing short-write handling in `writev()`.
- **The Cause:** `writev()` almost always writes everything to a terminal.
- **The Reality:** On pipes, sockets, or full disks, it writes partially.
- **Lesson:** Test for failure modes (slow I/O, full disks), not just happy paths.

## 3. The "Trusted Source" Myth
**Case Study:** `bin/cat/cat.c`
- **The Bug:** Unchecked `st_blksize` used for `malloc()`.
- **The Cause:** Trusting `stat()` return values from the filesystem.
- **The Reality:** FUSE filesystems, network mounts, or corruption can return `st_blksize` of 0 or 2GB.
- **Lesson:** Treat **ALL** external data as hostile. This includes:
  - Filesystem metadata (`stat`, `dirent`)
  - Environment variables (`getenv`)
  - Kernel parameters (`sysconf`, `sysctl`)
  - Network data

## 4. The Integer Overflow Blind Spot
**Case Study:** `sysconf(_SC_PAGESIZE)` in `cat.c`
- **The Bug:** Casting `long` (-1 on error) to `size_t` (unsigned).
- **The Consequence:** -1 becomes `SIZE_MAX` (huge number) -> buffer overflow.
- **Lesson:** Validate **BEFORE** casting. `if (val > 0) cast(val)`.

## 5. Legacy APIs exist for a reason (usually a bad one)
- **bzero()**: Deprecated. Use `memset()`.
- **sprintf()**: Dangerous. Use `snprintf()`.
- **gets()**: **FATAL**. Never use.
- **strcpy()**: Dangerous. Use `strlcpy()`.

## 6. Comment Syntax Errors Can Break Builds
**Case Study:** AI reviewer added comments containing `sys/*`
- **The Bug:** `/*` within a `/* ... */` comment block
- **The Compiler:** `-Werror,-Wcomment` treats nested `/*` as error
- **The Impact:** Build breaks with "error: '/*' within block comment"
- **The Fix:** Use `sys/...` or `sys/xxx` instead of `sys/*`
- **Lesson:** **C doesn't support nested comments.** Any `/*` or `*/` pattern inside a comment will break. When writing comments:
  - Avoid glob patterns with `*` adjacent to `/`
  - Use `...` or `xxx` for wildcards
  - Test build with `-Werror` enabled
  - Remember: Comments are code too!

**REPEAT OFFENSE WARNING:** This mistake was made MULTIPLE TIMES despite being documented:
- First occurrence: `bin/cat/cat.c` (fixed, documented in PERSONA.md and LESSONS.md)
- Second occurrence: `bin/pwd/pwd.c` and `bin/rm/rm.c` (same error!)
- **Root cause:** Not checking existing comments before commit
- **Prevention:** Automated pre-commit hook to grep for `sys/\*` in comments
- **Lesson:** Documentation alone is insufficient. Humans (and AIs) make the same mistakes repeatedly. AUTOMATE THE CHECK.

## 7. Shell Builtin Redefinitions Break Standard Assumptions
**Case Study:** `bin/kill/kill.c` with `#ifdef SHELL`
- **The Bug:** Checking `printf`/`fprintf` return values caused compilation errors
- **The Error:** `error: invalid operands to binary expression ('void' and 'int')`
- **The Cause:** When compiled as shell builtin, `bltin/bltin.h` redefines `printf` and `fprintf` to return `void` instead of `int`
- **The Impact:** Standard C assumption that printf returns int is WRONG in shell builtin context
- **The Reality:** FreeBSD utilities often serve dual purposes:
  1. Standalone programs (`/bin/kill`)
  2. Shell builtins (for performance)
  - When used as builtins, I/O is handled differently by the shell
  - Standard I/O functions are redefined for shell integration
- **Lesson:** **Context matters!** Don't blindly apply "best practices" without understanding the compilation context:
  - Check for `#ifdef SHELL` or similar conditional compilation
  - Shell builtins may redefine standard functions
  - What's correct for standalone programs may be wrong for builtins
  - Read the headers being included (`bltin/bltin.h`, etc.)
- **Rule:** Before adding I/O error checking, verify the function actually returns `int` in ALL compilation contexts

**FILES WITH DUAL COMPILATION:**
- `bin/kill/kill.c` - standalone + shell builtin
- `bin/test/test.c` - standalone + shell builtin (also has `#ifdef SHELL`)
- Likely others in bin/ directory

**PREVENTION:** Search for `#ifdef SHELL` before adding printf/fprintf error checks.

*Add to this file as new classes of bugs are discovered.*
