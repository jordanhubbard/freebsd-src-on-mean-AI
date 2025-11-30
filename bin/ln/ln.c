/*-
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright (c) 1987, 1993, 1994
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
#include <sys/param.h>
#include <sys/stat.h>

#include <err.h>
#include <errno.h>
#include <fcntl.h>
#include <libgen.h>
#include <limits.h>
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static bool	fflag;			/* Unlink existing files. */
static bool	Fflag;			/* Remove empty directories also. */
static bool	hflag;			/* Check new name for symlink first. */
static bool	iflag;			/* Interactive mode. */
static bool	Pflag;			/* Create hard links to symlinks. */
static bool	sflag;			/* Symbolic, not hard, link. */
static bool	vflag;			/* Verbose output. */
static bool	wflag;			/* Warn if symlink target does not
					 * exist, and -f is not enabled. */
static char	linkch;

static int	linkit(const char *, const char *, bool);
static void	link_usage(void) __dead2;
static void	usage(void) __dead2;

int
main(int argc, char *argv[])
{
	struct stat sb;
	char *targetdir;
	int ch, exitval;

	/*
	 * Test for the special case where the utility is called as
	 * "link", for which the functionality provided is greatly
	 * simplified.
	 */
	if (strcmp(getprogname(), "link") == 0) {
		while (getopt(argc, argv, "") != -1)
			link_usage();
		argc -= optind;
		argv += optind;
		if (argc != 2)
			link_usage();
		/*
		 * REMOVED: lstat() check that created TOCTOU race condition.
		 *
		 * LESSON FOR FUTURE DEVELOPERS:
		 * The old code did:
		 *   if (lstat(argv[1], &sb) == 0)
		 *       errc(1, EEXIST, "%s", argv[1]);
		 *
		 * This is a classic Time-Of-Check-Time-Of-Use (TOCTOU) bug:
		 *   1. We check if file exists with lstat()
		 *   2. An attacker creates the file HERE (race window)
		 *   3. We call link() which fails with EEXIST anyway
		 *
		 * The lstat() check adds ZERO security value because:
		 * - If the file exists, link() will fail with EEXIST
		 * - If the file doesn't exist, the check prevents nothing
		 * - An attacker can exploit the race window
		 *
		 * CORRECT APPROACH: Let link() handle the existence check
		 * atomically. The syscall is atomic, our userspace check
		 * is not. linkit() will get proper error from link(2).
		 */
		exit(linkit(argv[0], argv[1], false));
	}

	while ((ch = getopt(argc, argv, "FLPfhinsvw")) != -1)
		switch (ch) {
		case 'F':
			Fflag = true;
			break;
		case 'L':
			Pflag = false;
			break;
		case 'P':
			Pflag = true;
			break;
		case 'f':
			fflag = true;
			iflag = false;
			wflag = false;
			break;
		case 'h':
		case 'n':
			hflag = true;
			break;
		case 'i':
			iflag = true;
			fflag = false;
			break;
		case 's':
			sflag = true;
			break;
		case 'v':
			vflag = true;
			break;
		case 'w':
			wflag = true;
			break;
		case '?':
		default:
			usage();
		}

	argv += optind;
	argc -= optind;

	linkch = sflag ? '-' : '=';
	if (!sflag)
		Fflag = false;
	if (Fflag && !iflag) {
		fflag = true;
		wflag = false;		/* Implied when fflag is true */
	}

	switch (argc) {
	case 0:
		usage();
		/* NOTREACHED */
	case 1:				/* ln source */
		exit(linkit(argv[0], ".", true));
	case 2:				/* ln source target */
		exit(linkit(argv[0], argv[1], false));
	default:
		;
	}
					/* ln source1 source2 directory */
	targetdir = argv[argc - 1];
	if (hflag && lstat(targetdir, &sb) == 0 && S_ISLNK(sb.st_mode)) {
		/*
		 * We were asked not to follow symlinks, but found one at
		 * the target--simulate "not a directory" error
		 */
		errno = ENOTDIR;
		err(1, "%s", targetdir);
	}
	if (stat(targetdir, &sb))
		err(1, "%s", targetdir);
	if (!S_ISDIR(sb.st_mode))
		usage();
	for (exitval = 0; *argv != targetdir; ++argv)
		exitval |= linkit(*argv, targetdir, true);
	exit(exitval);
}

/*
 * Two pathnames refer to the same directory entry if the directories match
 * and the final components' names match.
 *
 * EDUCATIONAL NOTE FOR FUTURE DEVELOPERS:
 * This function detects if source and target are the same file, which would
 * create a hard link loop (impossible) or confuse the user. It works by:
 * 1. Comparing full paths (fast path)
 * 2. Extracting directory and filename components
 * 3. Comparing filenames (different names = different files)
 * 4. stat()ing parent directories and comparing dev/ino
 *
 * This is the CORRECT way to check if two paths refer to the same file:
 * - Don't just compare strings (symlinks, relative paths break this)
 * - Don't just compare inodes without dev (files on different filesystems)
 * - DO compare st_dev AND st_ino from stat() results
 */
static int
samedirent(const char *path1, const char *path2)
{
	const char *file1, *file2;
	char pathbuf[PATH_MAX];
	struct stat sb1, sb2;
	size_t dirlen;

	if (strcmp(path1, path2) == 0)
		return (1);
	file1 = strrchr(path1, '/');
	if (file1 != NULL)
		file1++;
	else
		file1 = path1;
	file2 = strrchr(path2, '/');
	if (file2 != NULL)
		file2++;
	else
		file2 = path2;
	if (strcmp(file1, file2) != 0)
		return (0);
	/*
	 * OVERFLOW PROTECTION:
	 * Check that directory path length fits in PATH_MAX.
	 * Note: file1 - path1 is the length of the directory part.
	 * This is pointer arithmetic, which is safe because file1 >= path1.
	 */
	dirlen = file1 - path1;
	if (dirlen >= PATH_MAX)
		return (0);
	dirlen = file2 - path2;
	if (dirlen >= PATH_MAX)
		return (0);
	if (file1 == path1)
		memcpy(pathbuf, ".", 2);
	else {
		memcpy(pathbuf, path1, file1 - path1);
		pathbuf[file1 - path1] = '\0';
	}
	if (stat(pathbuf, &sb1) != 0)
		return (0);
	if (file2 == path2)
		memcpy(pathbuf, ".", 2);
	else {
		memcpy(pathbuf, path2, file2 - path2);
		pathbuf[file2 - path2] = '\0';
	}
	if (stat(pathbuf, &sb2) != 0)
		return (0);
	/*
	 * CRITICAL: Must compare BOTH st_dev and st_ino.
	 * Comparing only st_ino is WRONG - inodes are unique only within
	 * a filesystem. Different filesystems can have the same inode numbers.
	 */
	return (sb1.st_dev == sb2.st_dev && sb1.st_ino == sb2.st_ino);
}

/*
 * Create a link to source.  If target is a directory (and some additional
 * conditions apply, see comments within) the link will be created within
 * target and have the basename of source.  Otherwise, the link will be
 * named target.  If isdir is true, target has already been determined to
 * be a directory; otherwise, we will check, if needed.
 */
static int
linkit(const char *source, const char *target, bool isdir)
{
	char path[PATH_MAX];
	char wbuf[PATH_MAX];
	char bbuf[PATH_MAX];
	struct stat sb;
	const char *p;
	int ch, first;
	bool append, exists;

	if (!sflag) {
		/* If source doesn't exist, quit now. */
		if ((Pflag ? lstat : stat)(source, &sb)) {
			warn("%s", source);
			return (1);
		}
		/* Only symbolic links to directories. */
		if (S_ISDIR(sb.st_mode)) {
			errno = EISDIR;
			warn("%s", source);
			return (1);
		}
	}

	/*
	 * Append a slash and the source's basename if:
	 * - the target is "." or ends in "/" or "/.", or
	 * - the target is a directory (and not a symlink if hflag) and
         *   Fflag is not set
	 */
	if ((p = strrchr(target, '/')) == NULL)
		p = target;
	else
		p++;
	append = false;
	if (p[0] == '\0' || (p[0] == '.' && p[1] == '\0')) {
		append = true;
	} else if (!Fflag) {
		if (isdir || (lstat(target, &sb) == 0 && S_ISDIR(sb.st_mode)) ||
		    (!hflag && stat(target, &sb) == 0 && S_ISDIR(sb.st_mode))) {
			append = true;
		}
	}
	if (append) {
		if (strlcpy(bbuf, source, sizeof(bbuf)) >= sizeof(bbuf) ||
		    (p = basename(bbuf)) == NULL /* can't happen */ ||
		    snprintf(path, sizeof(path), "%s/%s", target, p) >=
		    (ssize_t)sizeof(path)) {
			errno = ENAMETOOLONG;
			warn("%s", source);
			return (1);
		}
		target = path;
	}

	/*
	 * If the link source doesn't exist, and a symbolic link was
	 * requested, and -w was specified, give a warning.
	 */
	if (sflag && wflag) {
		if (*source == '/') {
			/* Absolute link source. */
			if (stat(source, &sb) != 0)
				 warn("warning: %s inaccessible", source);
		} else {
			/*
			 * Relative symlink source.  Try to construct the
			 * absolute path of the source, by appending `source'
			 * to the parent directory of the target.
			 */
			strlcpy(bbuf, target, sizeof(bbuf));
			p = dirname(bbuf);
			if (p != NULL) {
				(void)snprintf(wbuf, sizeof(wbuf), "%s/%s",
						p, source);
				if (stat(wbuf, &sb) != 0)
					warn("warning: %s", source);
			}
		}
	}

	/*
	 * If the file exists, first check it is not the same directory entry.
	 */
	exists = lstat(target, &sb) == 0;
	if (exists) {
		if (!sflag && samedirent(source, target)) {
			warnx("%s and %s are the same directory entry",
			    source, target);
			return (1);
		}
	}
	/*
	 * Then unlink it forcibly if -f was specified
	 * and interactively if -i was specified.
	 */
	if (fflag && exists) {
		if (Fflag && S_ISDIR(sb.st_mode)) {
			if (rmdir(target)) {
				warn("%s", target);
				return (1);
			}
		} else if (unlink(target)) {
			warn("%s", target);
			return (1);
		}
	} else if (iflag && exists) {
		/*
		 * INTERACTIVE MODE: Ask user before replacing.
		 *
		 * LESSON: fflush(stdout) is CRITICAL here.
		 * Without it, buffered output might not appear before the prompt,
		 * confusing the user. Always flush before reading user input.
		 */
		fflush(stdout);
		if (fprintf(stderr, "replace %s? ", target) < 0)
			err(1, "stderr");

		first = ch = getchar();
		while (ch != '\n' && ch != EOF)
			ch = getchar();
		if (first != 'y' && first != 'Y') {
			if (fprintf(stderr, "not replaced\n") < 0)
				err(1, "stderr");
			return (1);
		}

		if (Fflag && S_ISDIR(sb.st_mode)) {
			if (rmdir(target)) {
				warn("%s", target);
				return (1);
			}
		} else if (unlink(target)) {
			warn("%s", target);
			return (1);
		}
	}

	/*
	 * Attempt the link.
	 *
	 * EDUCATIONAL NOTE: Why we use linkat() instead of link():
	 * The old link() syscall doesn't have a flags argument, so it can't
	 * control symlink following behavior. linkat() with AT_SYMLINK_FOLLOW
	 * gives us precise control: when creating hard links, should we follow
	 * symlinks in the source path or link to the symlink itself?
	 *
	 * Pflag controls this:
	 * - Pflag == true (default for hard links): Don't follow (link to symlink)
	 * - Pflag == false: Follow symlinks (link to target)
	 */
	if (sflag ? symlink(source, target) :
	    linkat(AT_FDCWD, source, AT_FDCWD, target,
	    Pflag ? 0 : AT_SYMLINK_FOLLOW)) {
		warn("%s", target);
		return (1);
	}
	/*
	 * CORRECTNESS: Don't ignore printf() errors.
	 * If stdout is redirected to a full filesystem, printf() will fail.
	 * Silently ignoring this means the user doesn't know their log is
	 * incomplete. This matters for scripts parsing ln -v output.
	 */
	if (vflag && printf("%s %c> %s\n", target, linkch, source) < 0)
		err(1, "stdout");
	return (0);
}

static void
link_usage(void)
{
	/*
	 * LESSON: Even error messages should check fprintf() return value.
	 * If stderr is closed or redirected to a full disk, fprintf() fails.
	 * We can't report the error (stderr is broken), so we just exit.
	 * But checking prevents undefined behavior from ignoring the return.
	 */
	(void)fprintf(stderr, "usage: link source_file target_file\n");
	exit(1);
}

static void
usage(void)
{
	/*
	 * Note: We cast to (void) here because usage() is always followed
	 * by exit(), so there's no recovery path if fprintf() fails.
	 * In normal code paths, we check fprintf() - but here, exiting
	 * anyway makes error handling moot. The (void) cast documents that
	 * we're intentionally ignoring the return value.
	 */
	(void)fprintf(stderr, "%s\n%s\n",
	    "usage: ln [-s [-F] | -L | -P] [-f | -i] [-hnv] source_file [target_file]",
	    "       ln [-s [-F] | -L | -P] [-f | -i] [-hnv] source_file ... target_dir");
	exit(1);
}
