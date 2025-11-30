/*-
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright (c) 1988, 1993
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

#include <sys/param.h>

#include <err.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static void usage(void) __dead2;

int
main(int argc, char *argv[])
{
	int ch, sflag, dflag;
	/*
	 * BUFFER SIZE: MAXHOSTNAMELEN is typically 256 bytes on FreeBSD.
	 * Per RFC 1035, DNS labels are limited to 63 chars, and FQDN to
	 * 255 chars. MAXHOSTNAMELEN includes space for null terminator.
	 * 
	 * CRITICAL ISSUE: gethostname(3) does NOT guarantee null termination
	 * if hostname is too long! See comment at line ~80 for details.
	 */
	char hostname[MAXHOSTNAMELEN], *hostp, *p;

	sflag = 0;
	dflag = 0;
	while ((ch = getopt(argc, argv, "fsd")) != -1)
		switch (ch) {
		case 'f':
			/*
			 * On Linux, "hostname -f" prints FQDN.
			 * BSD "hostname" always prints FQDN by
			 * default, so we accept but ignore -f.
			 */
			break;
		case 's':
			sflag = 1;
			break;
		case 'd':
			dflag = 1;
			break;
		case '?':
		default:
			usage();
		}
	argc -= optind;
	argv += optind;

	if (argc > 1 || (sflag && dflag))
		usage();

	if (*argv) {
		/*
		 * UNNECESSARY CAST: strlen() returns size_t, casting to int
		 * is unnecessary and potentially dangerous.
		 * 
		 * OVERFLOW RISK (theoretical): If strlen(*argv) > INT_MAX,
		 * the cast truncates the value. However:
		 * - Hostnames limited by MAXHOSTNAMELEN (256 bytes)
		 * - Kernel would reject names longer than that
		 * - INT_MAX is 2^31-1 (2GB), far larger than MAXHOSTNAMELEN
		 * 
		 * SAFE HERE, but BAD PRACTICE. Should be:
		 *   size_t len = strlen(*argv);
		 *   if (len >= MAXHOSTNAMELEN) { errno = ENAMETOOLONG; err(...); }
		 *   if (sethostname(*argv, len)) err(1, "sethostname");
		 */
		if (sethostname(*argv, (int)strlen(*argv)))
			err(1, "sethostname");
	} else {
		hostp = hostname;
		/*
		 * CRITICAL BUG: gethostname(3) NULL TERMINATION
		 * 
		 * From gethostname(3) man page:
		 * "If the name is longer than the space provided, it is
		 * truncated and the returned name is not necessarily
		 * null-terminated."
		 * 
		 * THIS IS A BUFFER OVERRUN WAITING TO HAPPEN!
		 * 
		 * If system hostname >= MAXHOSTNAMELEN:
		 * 1. gethostname() truncates to fit buffer
		 * 2. Buffer is NOT null-terminated
		 * 3. strchr() at line 83/87 reads PAST END of buffer
		 * 4. printf() at line 91 reads PAST END of buffer
		 * 5. Buffer overrun → undefined behavior, possible crash
		 * 
		 * ATTACK SCENARIO: Administrator sets hostname to 256+ chars
		 * (possible via sethostname() syscall). User runs "hostname"
		 * → buffer overrun → potential arbitrary code execution if
		 * attacker controls stack contents after hostname buffer.
		 * 
		 * FIX REQUIRED: After gethostname(), MUST force null termination:
		 *   hostname[MAXHOSTNAMELEN - 1] = '\0';
		 * 
		 * This ensures buffer is ALWAYS null-terminated regardless
		 * of whether gethostname() truncated the name.
		 */
		if (gethostname(hostname, (int)sizeof(hostname)))
			err(1, "gethostname");
		/*
		 * CRITICAL FIX: Force null termination to prevent buffer
		 * overrun if hostname was truncated. This is REQUIRED for
		 * security and correctness.
		 */
		hostname[MAXHOSTNAMELEN - 1] = '\0';
		if (sflag) {
			p = strchr(hostname, '.');
			if (p != NULL)
				*p = '\0';
		} else if (dflag) {
			p = strchr(hostname, '.');
			if (p != NULL)
				hostp = p + 1;
		}
		/*
		 * UNCHECKED RETURN VALUE: printf() return not checked.
		 * If stdout is closed, redirected to full disk, or broken
		 * pipe, the write may fail silently. User won't know the
		 * hostname wasn't actually printed.
		 * 
		 * BETTER: Check return value or add fflush(stdout) with
		 * error checking before exit.
		 */
		(void)printf("%s\n", hostp);
	}
	exit(0);
}

static void
usage(void)
{
	/*
	 * STYLE VIOLATION: Original had blank line after opening brace.
	 * Per style(9), no blank line after function opening brace.
	 * This is consistent with usage() in pwd.c and other utilities.
	 */
	(void)fprintf(stderr, "usage: hostname [-f] [-s | -d] [name-of-host]\n");
	exit(1);
}
