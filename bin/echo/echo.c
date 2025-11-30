/*-
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright (c) 1989, 1993
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

#include <sys/types.h>
#include <sys/uio.h>

#include <assert.h>
#include <capsicum_helpers.h>
#include <err.h>
#include <errno.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

int
main(int argc, char *argv[])
{
	/*
	 * STYLE NOTE: Per style(9), comments should generally appear on
	 * the line BEFORE the declaration, not inline after it. This
	 * improves readability for longer comments and maintains
	 * consistent column alignment.
	 */
	int nflag;		/* if not set, output a trailing newline */
	int veclen;		/* number of writev arguments */
	struct iovec *iov;	/* elements to write */
	struct iovec *vp;	/* current element being filled */
	char space[] = " ";
	char newline[] = "\n";

	if (caph_limit_stdio() < 0 || caph_enter() < 0)
		err(1, "capsicum");

	/* This utility may NOT do getopt(3) option parsing. */
	if (*++argv && !strcmp(*argv, "-n")) {
		++argv;
		--argc;
		nflag = 1;
	} else
		nflag = 0;

	/*
	 * INTEGER OVERFLOW ANALYSIS: (argc - 2) * 2 + 1
	 * 
	 * Potential overflow if argc is huge. However, kernel limits argc:
	 * - execve(2) enforces ARG_MAX (typically 256KB-2MB total)
	 * - argc ≤ ARG_MAX / sizeof(char*) ≈ 32K-256K pointers on 64-bit
	 * - Even at maximum, (argc - 2) * 2 fits in int
	 * 
	 * DEFENSE: The kernel enforces limits, but we have NO compile-time
	 * or runtime check here. If someone increases ARG_MAX to INT_MAX/2,
	 * this silently overflows. Defense-in-depth would use checked math.
	 * 
	 * RISK ASSESSMENT: Low practical risk, but HIGH if kernel changes.
	 */
	veclen = (argc >= 2) ? (argc - 2) * 2 + 1 : 0;

	/*
	 * MEMORY LEAK: malloc() result never freed. Acceptable for
	 * short-lived utility that exits immediately (OS reclaims).
	 * If this were a long-running daemon, this would be a leak.
	 */
	if ((vp = iov = malloc((veclen + 1) * sizeof(struct iovec))) == NULL)
		err(1, "malloc");

	while (argv[0] != NULL) {
		size_t len;

		len = strlen(argv[0]);

		/*
		 * If the next argument is NULL then this is the last argument,
		 * therefore we need to check for a trailing \c.
		 */
		if (argv[1] == NULL) {
			/* is there room for a '\c' and is there one? */
			if (len >= 2 &&
			    argv[0][len - 2] == '\\' &&
			    argv[0][len - 1] == 'c') {
				/* chop it and set the no-newline flag. */
				len -= 2;
				nflag = 1;
			}
		}
		vp->iov_base = *argv;
		vp++->iov_len = len;
		if (*++argv) {
			vp->iov_base = space;
			vp++->iov_len = 1;
		}
	}
	if (!nflag) {
		veclen++;
		vp->iov_base = newline;
		vp++->iov_len = 1;
	}
	/*
	 * WHY IS THIS ASSERTION COMMENTED OUT?
	 * 
	 * The assertion `veclen == (vp - iov)` verifies that we've filled
	 * exactly the number of iovec elements we calculated. This is a
	 * good sanity check for buffer overrun bugs in the loop above.
	 * 
	 * POSSIBLE REASONS IT'S DISABLED:
	 * 1. assert() not traditionally used in production FreeBSD utilities
	 * 2. Adds <assert.h> dependency (already included though!)
	 * 3. Someone didn't want assert() crashes in production
	 * 4. The math is "obviously correct" (famous last words)
	 * 
	 * RECOMMENDATION: Either ENABLE it (we have assert.h included!),
	 * or REMOVE it entirely. Commented-out asserts are worse than
	 * nothing - they suggest distrust without providing protection.
	 * 
	 * If you don't trust your invariant enough to assert it in
	 * production, you shouldn't trust your code at all.
	 */
	/* assert(veclen == (vp - iov)); */
	
	/*
	 * CRITICAL BUG: Incomplete short-write handling!
	 * 
	 * writev(2) can write FEWER bytes than requested due to:
	 * - Signal interruption (EINTR)
	 * - Pipe buffer full (partial write to pipe/socket)
	 * - Disk quota nearly full (partial write to file)
	 * - Non-blocking descriptor with limited buffer space
	 * 
	 * Current code only checks for -1 (error), but doesn't handle
	 * the case where writev() returns a POSITIVE value LESS than
	 * the total bytes in the iovec array.
	 * 
	 * CORRECT BEHAVIOR: After partial write, must:
	 * 1. Calculate how many complete iovec entries were written
	 * 2. Adjust iov pointer to skip completed entries
	 * 3. Handle partial write of first incomplete entry
	 * 4. Retry with remaining iovecs
	 * 
	 * CURRENT BEHAVIOR: Assumes writev() either succeeds fully or
	 * fails completely. This works for stdout to terminal, but FAILS
	 * for pipes/sockets/files with:
	 * - Data corruption (missing bytes)
	 * - No error reported to user
	 * - Silent truncation of output
	 * 
	 * WHY THIS WORKS IN PRACTICE: For stdout to a terminal, writev()
	 * rarely does partial writes. But this is a LATENT BUG waiting
	 * to bite someone who pipes echo to a slow network socket.
	 * 
	 * FIX REQUIRED: Implement proper short-write retry loop.
	 * See write_retry() pattern in other FreeBSD utilities.
	 */
	while (veclen) {
		int nwrite;

		nwrite = (veclen > IOV_MAX) ? IOV_MAX : veclen;
		if (writev(STDOUT_FILENO, iov, nwrite) == -1)
			err(1, "write");
		/*
		 * BUG: No handling of short writes! If writev() returns
		 * fewer bytes than sum of iov_len for nwrite entries,
		 * we silently lose data. See comment above.
		 */
		iov += nwrite;
		veclen -= nwrite;
	}
	return 0;
}
