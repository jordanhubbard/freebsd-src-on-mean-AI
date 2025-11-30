/*-
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

#include <sys/cdefs.h>
#include <sys/types.h>

#include <err.h>
#include <errno.h>
#include <limits.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "stty.h"
#include "extern.h"

static void gerr(const char *s) __dead2;

static void
gerr(const char *s)
{
	if (s)
		errx(1, "illegal gfmt1 option -- %s", s);
	else
		errx(1, "illegal gfmt1 option");
}

void
gprint(struct termios *tp, struct winsize *wp __unused, int ldisc __unused)
{
	struct cchar *cp;

	if (printf("gfmt1:cflag=%lx:iflag=%lx:lflag=%lx:oflag=%lx:",
	    (u_long)tp->c_cflag, (u_long)tp->c_iflag, (u_long)tp->c_lflag,
	    (u_long)tp->c_oflag) < 0)
		err(1, "stdout");
	for (cp = cchars1; cp->name; ++cp)
		if (printf("%s=%x:", cp->name, tp->c_cc[cp->sub]) < 0)
			err(1, "stdout");
	if (printf("ispeed=%lu:ospeed=%lu\n",
	    (u_long)cfgetispeed(tp), (u_long)cfgetospeed(tp)) < 0)
		err(1, "stdout");
}

void
gread(struct termios *tp, char *s)
{
	struct cchar *cp;
	char *ep, *p, *endp;
	unsigned long tmp;

	if ((s = strchr(s, ':')) == NULL)
		gerr(NULL);
	for (++s; s != NULL;) {
		p = strsep(&s, ":\0");
		if (!p || !*p)
			break;
		if (!(ep = strchr(p, '=')))
			gerr(p);
		*ep++ = '\0';

		/*
		 * Parse the value. Default to base 16 for flags,
		 * but use base 10 for speeds and special cc values.
		 * Always validate the result to prevent integer
		 * truncation vulnerabilities.
		 */
		errno = 0;
		tmp = strtoul(ep, &endp, 0x10);
		if (errno == ERANGE || *endp != '\0')
			gerr(p);

#define	CHK(s)	(*p == s[0] && !strcmp(p, s))
		if (CHK("cflag")) {
			/*
			 * Ensure value fits in tcflag_t without truncation.
			 * On most systems tcflag_t is unsigned int or
			 * unsigned long, but we verify to be safe.
			 */
			if (tmp > (unsigned long)(tcflag_t)-1)
				errx(1, "cflag value %lu out of range", tmp);
			tp->c_cflag = (tcflag_t)tmp;
			continue;
		}
		if (CHK("iflag")) {
			if (tmp > (unsigned long)(tcflag_t)-1)
				errx(1, "iflag value %lu out of range", tmp);
			tp->c_iflag = (tcflag_t)tmp;
			continue;
		}
		if (CHK("ispeed")) {
			errno = 0;
			tmp = strtoul(ep, &endp, 10);
			if (errno == ERANGE || *endp != '\0')
				gerr(p);
			if (tmp > (unsigned long)(speed_t)-1)
				errx(1, "ispeed value %lu out of range", tmp);
			tp->c_ispeed = (speed_t)tmp;
			continue;
		}
		if (CHK("lflag")) {
			if (tmp > (unsigned long)(tcflag_t)-1)
				errx(1, "lflag value %lu out of range", tmp);
			tp->c_lflag = (tcflag_t)tmp;
			continue;
		}
		if (CHK("oflag")) {
			if (tmp > (unsigned long)(tcflag_t)-1)
				errx(1, "oflag value %lu out of range", tmp);
			tp->c_oflag = (tcflag_t)tmp;
			continue;
		}
		if (CHK("ospeed")) {
			errno = 0;
			tmp = strtoul(ep, &endp, 10);
			if (errno == ERANGE || *endp != '\0')
				gerr(p);
			if (tmp > (unsigned long)(speed_t)-1)
				errx(1, "ospeed value %lu out of range", tmp);
			tp->c_ospeed = (speed_t)tmp;
			continue;
		}
		for (cp = cchars1; cp->name != NULL; ++cp)
			if (CHK(cp->name)) {
				if (cp->sub == VMIN || cp->sub == VTIME) {
					errno = 0;
					tmp = strtoul(ep, &endp, 10);
					if (errno == ERANGE || *endp != '\0')
						gerr(p);
				}
				/*
				 * CRITICAL: cc_t is typically unsigned char,
				 * so values must be 0-255. Validate to prevent
				 * truncation attacks.
				 */
				if (tmp > (unsigned long)(cc_t)-1)
					errx(1, "%s value %lu out of range "
					    "(max %u)", cp->name, tmp,
					    (unsigned int)(cc_t)-1);
				tp->c_cc[cp->sub] = (cc_t)tmp;
				break;
			}
		if (cp->name == NULL)
			gerr(p);
	}
}
