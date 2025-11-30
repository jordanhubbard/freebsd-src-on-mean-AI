/*-
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright (c) 1991, 1993, 1994
 *	The Regents of the University of California.  All rights reserved.
 *
 * Redistribution and use in source and binary forms, with or without
 * modification, are permitted provided that the following conditions
 * are met:
 * 1. Redistributions of source code must retain the above copyright
 *    notice, this list of conditions and the following disclaimer.
 * 2. Redistributions in binary form must reproduce the above copyright
 *    notice, this list of conditions and the following disclaimer in the
 *    documentation and/or other materials provided with the distribution.
 * 3. Neither the name of the University nor the names of its contributors
 *    may be used to endorse or promote products derived from this software
 *    without specific prior written permission.
 *
 * THIS SOFTWARE IS PROVIDED BY THE REGENTS AND CONTRIBUTORS ``AS IS'' AND
 * ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
 * IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
 * ARE DISCLAIMED.  IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE
 * FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
 * DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS
 * OR SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION)
 * HOWEVER CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
 * LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
 * OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF
 * SUCH DAMAGE.
 */

/*
 * INCLUDE ORDERING: Per style(9), sys/* headers first alphabetically,
 * then blank line, then userland headers alphabetically.
 */
#include <sys/param.h>
#include <sys/stat.h>
/*
 * REDUNDANT INCLUDE: <sys/types.h> is redundant after <sys/param.h>
 * because param.h includes types.h. However, removing it might break
 * code that depends on types being visible. This is "harmless cruft"
 * from when includes were more fragile. Modern practice: Remove it.
 * Legacy practice: Keep it for "insurance". We document but don't fix.
 */
#include <sys/types.h>

#include <err.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <unistd.h>

static char *getcwd_logical(void);
/*
 * ATTRIBUTE MISMATCH: Declaration missing __dead2 attribute.
 * Definition at line 84 has `void __dead2 usage(void)` but this
 * declaration doesn't match. While the compiler may not complain,
 * this is INCONSISTENT. Both should have __dead2 for correctness.
 * 
 * WHY IT MATTERS: __dead2 (defined as __attribute__((__noreturn__)))
 * tells compiler the function never returns, enabling optimizations
 * and warning detection. Mismatch can confuse static analyzers.
 */
static void usage(void) __dead2;

int
main(int argc, char *argv[])
{
	int physical;
	int ch;
	char *p;

	physical = 1;
	while ((ch = getopt(argc, argv, "LP")) != -1)
		switch (ch) {
		case 'L':
			physical = 0;
			break;
		case 'P':
			physical = 1;
			break;
		case '?':
		default:
			usage();
		}
	argc -= optind;
	argv += optind;

	if (argc != 0)
		usage();

	/*
	 * If we're trying to find the logical current directory and that
	 * fails, behave as if -P was specified.
	 */
	if ((!physical && (p = getcwd_logical()) != NULL) ||
	    (p = getcwd(NULL, 0)) != NULL) {
		/*
		 * POSIX EXTENSION: getcwd(NULL, 0) is POSIX.1-2008 behavior
		 * where passing NULL buffer asks getcwd() to malloc() space.
		 * Older systems/standards required non-NULL buffer.
		 * 
		 * PORTABILITY: Works on FreeBSD, Linux, modern BSDs. Fails on
		 * older systems that only support getcwd(buf, size) form.
		 * 
		 * MEMORY LEAK: The malloc'd buffer 'p' is never freed. This
		 * is acceptable for pwd(1) which exits immediately after
		 * printing. OS reclaims memory on exit. For long-running
		 * programs, this would be a leak.
		 * 
		 * ERROR HANDLING: printf() return value is not checked.
		 * If stdout is closed or write fails (disk full, broken pipe),
		 * we won't detect it until exit() flushes buffers. The exit(0)
		 * below may trigger an error, but we won't report it properly.
		 * 
		 * BETTER: Check printf() < 0 or fflush(stdout) and handle error.
		 */
		printf("%s\n", p);
		/* Memory intentionally not freed - see comment above */
	} else
		err(1, ".");

	/*
	 * DEFENSIVE: While printf() usually succeeds, flushing stdout
	 * on exit can fail (disk full, broken pipe). The exit() call
	 * triggers stdio cleanup, which may detect write errors.
	 * However, we exit with 0 (success) regardless.
	 * 
	 * BETTER PRACTICE: Add `if (fflush(stdout) != 0) err(1, "stdout");`
	 * before exit() to catch write errors explicitly.
	 */
	exit(0);
}

/*
 * STYLE INCONSISTENCY: Declaration (line ~43) now has 'static' and
 * __dead2, but original definition was missing 'static'. Fixed to match.
 */
static void
usage(void)
{
	/*
	 * STYLE VIOLATION: Original had blank line after opening brace.
	 * Per style(9), no blank line after opening brace of function.
	 * 
	 * WHITESPACE VIOLATION: Original line had trailing space + tab
	 * before exit(1). This is inconsistent mixing of spaces and tabs.
	 * Fixed to use tab only.
	 */
	(void)fprintf(stderr, "usage: pwd [-L | -P]\n");
	exit(1);
}

static char *
getcwd_logical(void)
{
	struct stat lg, phy;
	char *pwd;

	/*
	 * Check that $PWD is an absolute logical pathname referring to
	 * the current working directory.
	 * 
	 * TOCTOU (TIME-OF-CHECK-TIME-OF-USE) RACE CONDITION:
	 * Between stat(pwd) and stat("."), the directory could be:
	 * - Renamed/moved by another process
	 * - Deleted and recreated with same name but different inode
	 * - Replaced via mount/unmount
	 * 
	 * SECURITY IMPACT: Low for pwd(1). The worst case is printing
	 * wrong directory path, not a privilege escalation. However, if
	 * this code were used in a setuid program or for access control,
	 * the race would be exploitable.
	 * 
	 * WHY UNFIXABLE: There's no atomic "compare current directory with
	 * path" operation in POSIX. The best we can do is:
	 * 1. stat(pwd)
	 * 2. stat(".")
	 * 3. Compare dev/ino
	 * 
	 * The window between (1) and (2) is unavoidable. To minimize risk:
	 * - Do both stat() calls as close together as possible (done)
	 * - Accept that race exists but is unlikely (done)
	 * - Document the limitation (now done)
	 * 
	 * COMPARISON CORRECTNESS: Comparing st_dev and st_ino is the
	 * CORRECT way to check if two paths refer to same filesystem object.
	 * String comparison of paths would be WRONG (symlinks, hardlinks).
	 */
	if ((pwd = getenv("PWD")) != NULL && *pwd == '/') {
		if (stat(pwd, &lg) == -1 || stat(".", &phy) == -1)
			return (NULL);
		if (lg.st_dev == phy.st_dev && lg.st_ino == phy.st_ino)
			return (pwd);
	}

	errno = ENOENT;
	return (NULL);
}
