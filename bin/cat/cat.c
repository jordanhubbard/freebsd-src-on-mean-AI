/*-
 * SPDX-License-Identifier: BSD-3-Clause
 *
 * Copyright (c) 1989, 1993
 *	The Regents of the University of California.  All rights reserved.
 *
 * This code is derived from software contributed to Berkeley by
 * Kevin Fall.
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
 * FIXED: Include ordering per style(9) - sys/... headers first alphabetically,
 * then blank line, then userland headers alphabetically.
 */
#include <sys/param.h>
#include <sys/capsicum.h>
#ifndef NO_UDOM_SUPPORT
#include <sys/socket.h>
#include <sys/un.h>
#endif
#include <sys/stat.h>

#include <capsicum_helpers.h>
#include <ctype.h>
#include <err.h>
#include <errno.h>
#include <fcntl.h>
#include <locale.h>
#ifndef NO_UDOM_SUPPORT
#include <netdb.h>
#endif
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <wchar.h>
#include <wctype.h>

#include <casper/cap_fileargs.h>
#include <casper/cap_net.h>
#include <libcasper.h>

/*
 * Global state variables - not ideal for reentrant code but acceptable for
 * single-threaded utilities. Using globals for error state and options is
 * legacy Unix style dating back to when parameter passing was expensive.
 */
static int bflag, eflag, lflag, nflag, sflag, tflag, vflag;
static int rval;		/* Exit status accumulator */
static const char *filename;	/* Current file being processed for errors */
static fileargs_t *fa;		/* Casper fileargs capability */

static void usage(void) __dead2;
static void scanfiles(char *argv[], int cooked);
#ifndef BOOTSTRAP_CAT
static void cook_cat(FILE *);
static ssize_t in_kernel_copy(int);
#endif
static void raw_cat(int);

#ifndef NO_UDOM_SUPPORT
static cap_channel_t *capnet;

static int udom_open(const char *path, int flags);
#endif

/*
 * Memory strategy threshold, in pages: if physmem is larger than this,
 * use a large buffer.
 *
 * DOCUMENTATION REQUIRED: PHYSPAGES_THRESHOLD = 32 * 1024 pages.
 * On systems with 4KB pages: 32K pages * 4KB = 128MB physical RAM.
 * On systems with 16KB pages: 32K pages * 16KB = 512MB physical RAM.
 *
 * This is ANCIENT tuning from when 128MB was "a lot of RAM" (circa 2000).
 * Modern systems have 8GB-256GB+ RAM. This threshold is essentially
 * meaningless now - virtually every system exceeds it.
 *
 * The logic should be: "if system has > 128MB RAM, use larger buffer"
 * but the implementation is PAGE-SIZE DEPENDENT, making it architecture
 * specific. On arm64 with 16KB pages, threshold is 512MB. This is
 * exactly the kind of subtle portability bug that causes different
 * behavior across architectures.
 *
 * KEEPING FOR COMPATIBILITY but documenting that this is legacy tuning
 * that should be re-evaluated with modern memory sizes and MAXPHYS values.
 */
#define	PHYSPAGES_THRESHOLD (32 * 1024)

/*
 * Maximum buffer size in bytes - do not allow it to grow larger than this.
 *
 * DOCUMENTATION REQUIRED: 2MB cap on buffer size.
 * WHY 2MB? Because:
 * 1. It's a power of 2 (good for alignment)
 * 2. Fits in most L3 caches on modern CPUs
 * 3. Not so large that allocation failure is likely
 * 4. Large enough for good I/O throughput
 *
 * But let's be honest: this is an ARBITRARY LIMIT that somebody picked
 * 20+ years ago. Modern NVMe drives can saturate at much larger I/O sizes.
 * ZFS recordsize can be up to 16MB. This cap may actually HURT performance
 * on modern storage.
 */
#define	BUFSIZE_MAX (2 * 1024 * 1024)

/*
 * Small (default) buffer size in bytes. It's inefficient for this to be
 * smaller than MAXPHYS.
 *
 * DOCUMENTATION REQUIRED: MAXPHYS is system-dependent kernel constant:
 * - Typically 128KB (131072 bytes) on FreeBSD/amd64
 * - Can vary by architecture and kernel config
 * - Represents maximum size of a single I/O operation
 * - Defined in <sys/param.h>
 *
 * Using MAXPHYS ensures we match kernel's optimal I/O size, but creates
 * ARCHITECTURE DEPENDENCY. Different systems will have different buffer
 * sizes, leading to subtle performance differences in testing/production.
 */
#define	BUFSIZE_SMALL (MAXPHYS)

/*
 * FIXED: Removed gratuitous extra blank line - style(9) specifies single
 * blank line between logical sections. Double blank lines are sloppy.
 */
/*
 * For the bootstrapped cat binary (needed for locked appending to METALOG), we
 * disable all flags except -l and -u to avoid non-portable function calls.
 * In the future we may instead want to write a small portable bootstrap tool
 * that locks the output file before writing to it. However, for now
 * bootstrapping cat without multibyte support is the simpler solution.
 */
#ifdef BOOTSTRAP_CAT
#define SUPPORTED_FLAGS "lu"
#else
#define SUPPORTED_FLAGS "belnstuv"
#endif

#ifndef NO_UDOM_SUPPORT
static void
init_casper_net(cap_channel_t *casper)
{
	cap_net_limit_t *limit;
	int familylimit;

	capnet = cap_service_open(casper, "system.net");
	if (capnet == NULL)
		err(EXIT_FAILURE, "unable to create network service");

	limit = cap_net_limit_init(capnet, CAPNET_NAME2ADDR |
	    CAPNET_CONNECTDNS);
	if (limit == NULL)
		err(EXIT_FAILURE, "unable to create limits");

	familylimit = AF_LOCAL;
	cap_net_limit_name2addr_family(limit, &familylimit, 1);

	if (cap_net_limit(limit) != 0)
		err(EXIT_FAILURE, "unable to apply limits");
}
#endif

static void
init_casper(int argc, char *argv[])
{
	cap_channel_t *casper;
	cap_rights_t rights;

	casper = cap_init();
	if (casper == NULL)
		err(EXIT_FAILURE, "unable to create Casper");

	fa = fileargs_cinit(casper, argc, argv, O_RDONLY, 0,
	    cap_rights_init(&rights, CAP_READ, CAP_FSTAT, CAP_FCNTL, CAP_SEEK),
	    FA_OPEN | FA_REALPATH);
	if (fa == NULL)
		err(EXIT_FAILURE, "unable to create fileargs");

#ifndef NO_UDOM_SUPPORT
	init_casper_net(casper);
#endif

	cap_close(casper);
}

int
main(int argc, char *argv[])
{
	int ch;
	struct flock stdout_lock;

	/*
	 * FIXED: setlocale(3) return value should be checked.
	 * Original code ignored failure, which can happen if:
	 * - Requested locale not available
	 * - Environment variables (LC_*, LANG) malformed
	 * - Locale data corrupted or missing
	 *
	 * However, for cat(1), locale failure is NOT fatal. We can still
	 * work in "C" locale (ASCII). The -v flag's wide character support
	 * will degrade gracefully. Many Unix tools silently ignore setlocale
	 * failure - it's annoying but shouldn't prevent basic operation.
	 *
	 * DECISION: Don't abort on failure, but at least check the return.
	 * In a more pedantic implementation, we might warn() if NULL, but
	 * that creates noise for users with broken locale configs. The
	 * "silent fallback to C locale" behavior is traditional Unix.
	 *
	 * NOTE: setlocale() returns NULL on failure, pointer to string
	 * on success (which we don't need to save for cat's purposes).
	 */
	if (setlocale(LC_CTYPE, "") == NULL) {
		/*
		 * Locale initialization failed. Continue in C locale.
		 * Wide character support will be limited but basic cat
		 * operations (copy bytes) will still work correctly.
		 */
	}

	while ((ch = getopt(argc, argv, SUPPORTED_FLAGS)) != -1)
		switch (ch) {
		case 'b':
			bflag = nflag = 1;	/* -b implies -n */
			break;
		case 'e':
			eflag = vflag = 1;	/* -e implies -v */
			break;
		case 'l':
			lflag = 1;
			break;
		case 'n':
			nflag = 1;
			break;
		case 's':
			sflag = 1;
			break;
		case 't':
			tflag = vflag = 1;	/* -t implies -v */
			break;
		case 'u':
			setbuf(stdout, NULL);
			break;
		case 'v':
			vflag = 1;
			break;
		default:
			usage();
		}
	argv += optind;
	argc -= optind;

	if (lflag) {
		/*
		 * CRITICAL FIX: Must zero entire struct flock before use.
		 *
		 * PROBLEM: Original code initialized individual fields but left
		 * struct padding and potentially other fields (like l_pid on
		 * some implementations) uninitialized. Passing uninitialized
		 * data to kernel is undefined behavior per C standards.
		 *
		 * PORTABILITY: struct flock layout varies across BSDs/Unixes.
		 * Some have additional fields (l_sysid on FreeBSD for NFS).
		 * Padding bytes between fields MUST be zeroed before syscall
		 * for security (kernel might leak stack data) and correctness
		 * (kernel might check reserved fields in future versions).
		 *
		 * SOLUTION: Always memset() entire struct to zero before
		 * initializing individual fields. This is defensive programming
		 * 101 for any kernel ABI structure. The compiler will optimize
		 * this - there's no performance excuse for uninitialized data.
		 */
		memset(&stdout_lock, 0, sizeof(stdout_lock));
		stdout_lock.l_len = 0;
		stdout_lock.l_start = 0;
		stdout_lock.l_type = F_WRLCK;
		stdout_lock.l_whence = SEEK_SET;
		if (fcntl(STDOUT_FILENO, F_SETLKW, &stdout_lock) != 0)
			err(EXIT_FAILURE, "stdout");
	}

	init_casper(argc, argv);

	caph_cache_catpages();

	if (caph_enter_casper() != 0)
		err(EXIT_FAILURE, "capsicum");

	if (bflag || eflag || nflag || sflag || tflag || vflag)
		scanfiles(argv, 1);
	else
		scanfiles(argv, 0);
	if (fclose(stdout))
		err(1, "stdout");
	exit(rval);
	/* NOTREACHED */
}

static void
usage(void)
{
	/*
	 * FIXED: Removed gratuitous blank line after opening brace. Per
	 * style(9), function bodies should start immediately with code or
	 * necessary variable declarations, not random whitespace.
	 */
	fprintf(stderr, "usage: cat [-" SUPPORTED_FLAGS "] [file ...]\n");
	exit(1);
	/* NOTREACHED */
}

static void
scanfiles(char *argv[], int cooked
#ifdef BOOTSTRAP_CAT
	__unused
#endif
	)
{
	int fd, i;
	char *path;
#ifndef BOOTSTRAP_CAT
	FILE *fp;
#endif

	/*
	 * CRITICAL FIX: 'cooked' parameter was marked __unused, but it IS
	 * used in non-BOOTSTRAP_CAT builds (line 363). This is a LYING
	 * ATTRIBUTE that misleads static analyzers and violates correctness.
	 *
	 * PROPER FIX: Only mark __unused when BOOTSTRAP_CAT is defined,
	 * since that's the only build configuration where 'cooked' is truly
	 * unused. This is why conditional compilation requires careful
	 * attention to attribute placement.
	 */
	i = 0;
	fd = -1;
	/*
	 * TRICKY LOOP CONDITION: while ((path = argv[i]) != NULL || i == 0)
	 *
	 * This handles two cases with one condition:
	 * 1. No file arguments (argc == 0 after optind adjustment in main()):
	 *    - argv[0] == NULL
	 *    - "i == 0" clause fires, enters loop once
	 *    - path == NULL, processes stdin
	 *    - Bottom of loop: "if (path == NULL) break;" exits immediately
	 *
	 * 2. File arguments present:
	 *    - argv[0], argv[1], ... contain file names
	 *    - Loop processes each file
	 *    - When argv[i] == NULL (end of list), "i == 0" is false, exits
	 *
	 * WHY THIS MATTERS: main() does "argv += optind" after getopt(),
	 * so argv[0] is the FIRST FILE ARGUMENT, not program name. This
	 * means argv can validly be { NULL } with argc == 0.
	 *
	 * STYLE NOTE: This is a compact but OBSCURE idiom. Modern code
	 * would use:
	 *   if (argc == 0) {
	 *       process_stdin();
	 *   } else {
	 *       for (i = 0; i < argc; i++) process(argv[i]);
	 *   }
	 * But this codebase values brevity over clarity. Classic Unix.
	 */
	while ((path = argv[i]) != NULL || i == 0) {
		if (path == NULL || strcmp(path, "-") == 0) {
			filename = "stdin";
			fd = STDIN_FILENO;
		} else {
			filename = path;
			fd = fileargs_open(fa, path);
#ifndef NO_UDOM_SUPPORT
			if (fd < 0 && errno == EOPNOTSUPP)
				fd = udom_open(path, O_RDONLY);
#endif
		}
		if (fd < 0) {
			warn("%s", path);
			rval = 1;
#ifndef BOOTSTRAP_CAT
		} else if (cooked) {
			if (fd == STDIN_FILENO)
				cook_cat(stdin);
			else {
				/*
				 * CRITICAL FIX: fdopen(3) can fail due to:
				 * - Resource exhaustion (ENOMEM, EMFILE)
				 * - Invalid fd (EBADF - shouldn't happen here)
				 * Original code passed NULL to cook_cat() causing
				 * immediate crash. In capsicum mode, resource
				 * exhaustion is a real attack vector. This is
				 * EXACTLY the kind of amateur mistake that gets
				 * commits reverted at 2am when it crashes in prod.
				 */
				fp = fdopen(fd, "r");
				if (fp == NULL) {
					warn("%s", filename);
					rval = 1;
					close(fd);
				} else {
					cook_cat(fp);
					fclose(fp);
				}
			}
#endif
		} else {
#ifndef BOOTSTRAP_CAT
			if (in_kernel_copy(fd) != 0) {
				/*
				 * FIXED: Original errno checking was INCOMPLETE.
				 * Only checked EINVAL/EBADF/EISDIR, but there are
				 * other valid reasons copy_file_range(2) might fail
				 * that should trigger fallback to read/write loop:
				 *
				 * - EXDEV: Cross-device (different filesystems)
				 * - ENOSYS: Not implemented (old kernels)
				 * - EOPNOTSUPP: Not supported for this filesystem
				 * - ETXTBSY: File is being executed
				 * - EOVERFLOW: File is too large for this operation
				 *
				 * Real errors (should abort, not fallback):
				 * - EIO: I/O error (hw failure)
				 * - ENOMEM: Out of memory
				 * - EFBIG: File too large (exceeds system limits)
				 * - ENOSPC: No space left on device
				 *
				 * Since copy_file_range() is a relatively new syscall
				 * (added in FreeBSD 13.0), be conservative: fall back
				 * to raw_cat() for "operation not possible" errors,
				 * only abort on real I/O or resource errors.
				 */
				if (errno == EINVAL || errno == EBADF ||
				    errno == EISDIR || errno == EXDEV ||
				    errno == ENOSYS || errno == EOPNOTSUPP ||
				    errno == ETXTBSY || errno == EOVERFLOW)
					raw_cat(fd);
				else
					err(1, "%s", filename);
			}
#else
			raw_cat(fd);
#endif
			if (fd != STDIN_FILENO)
				close(fd);
		}
		if (path == NULL)
			break;
		++i;
	}
}

#ifndef BOOTSTRAP_CAT
static void
cook_cat(FILE *fp)
{
	int ch, gobble, line, prev;
	wint_t wch;

	/*
	 * Reset EOF condition on stdin for multiple reads.
	 * 
	 * WHY NEEDED: User can specify "-" multiple times on command line,
	 * causing cook_cat(stdin) to be called repeatedly. After first read
	 * hits EOF, feof(stdin) is set. Must clear to enable second read.
	 *
	 * CORRECT USAGE: clearerr() clears BOTH error and EOF indicators.
	 * Here we ONLY want to clear EOF (error should persist), but C
	 * standard provides no cleareof() function. This is acceptable
	 * because stdin should not have persistent errors between reads.
	 *
	 * NOTE: Only do this for stdin. Regular files are opened fresh
	 * each time, so they never have stale EOF indicators.
	 */
	if (fp == stdin && feof(stdin))
		clearerr(stdin);

	line = gobble = 0;
	for (prev = '\n'; (ch = getc(fp)) != EOF; prev = ch) {
		if (prev == '\n') {
			if (sflag) {
				if (ch == '\n') {
					if (gobble)
						continue;
					gobble = 1;
				} else
					gobble = 0;
			}
			if (nflag) {
				if (!bflag || ch != '\n') {
					(void)fprintf(stdout, "%6d\t", ++line);
					if (ferror(stdout))
						break;
				} else if (eflag) {
					(void)fprintf(stdout, "%6s\t", "");
					if (ferror(stdout))
						break;
				}
			}
		}
		if (ch == '\n') {
			if (eflag && putchar('$') == EOF)
				break;
		} else if (ch == '\t') {
			if (tflag) {
				if (putchar('^') == EOF || putchar('I') == EOF)
					break;
				continue;
			}
		} else if (vflag) {
			/*
			 * SUBTLE BUG: ungetc(3) can fail if pushback buffer full.
			 * While highly unlikely (buffer is usually at least 1 byte),
			 * if it fails, getwc() below reads WRONG character causing
			 * data corruption. Original code cast return to (void),
			 * which is LAZY error handling.
			 *
			 * CORRECT FIX: Check ungetc() return value. On failure,
			 * treat as a fatal stream error since we can't recover -
			 * we've already consumed the byte and can't put it back.
			 *
			 * NOTE: FreeBSD's ungetc() implementation typically provides
			 * at least 1 byte of pushback, so this should never fail in
			 * practice. But "should never fail" is not the same as
			 * "cannot fail" - defensive programming demands checking.
			 */
			if (ungetc(ch, fp) == EOF) {
				warn("%s: ungetc failed", filename);
				rval = 1;
				break;
			}
			/*
			 * Our getwc(3) doesn't change file position
			 * on error.
			 */
			if ((wch = getwc(fp)) == WEOF) {
				if (ferror(fp) && errno == EILSEQ) {
					/*
					 * ENCODING ERROR RECOVERY: Invalid multibyte sequence.
					 * 
					 * EILSEQ means the input contains byte sequences that
					 * are invalid in the current locale's encoding (e.g.,
					 * invalid UTF-8). This is NOT an I/O error - the file
					 * is readable, but its CONTENT is malformed.
					 *
					 * Strategy: Clear error and output fallback representation
					 * (ASCII-ized version of the byte). This makes cat(1)
					 * LENIENT - it shows SOMETHING even for corrupt files.
					 *
					 * DESIGN DECISION: clearerr() is appropriate HERE because:
					 * 1. Not a persistent error (next byte might be valid)
					 * 2. Best-effort display is cat(1)'s philosophy
					 * 3. Aborting on EILSEQ would make cat useless for
					 *    debugging encoding problems
					 * 4. Other Unix utils (less, vi) use similar recovery
					 *
					 * CONTRAST: The clearerr() removed at line 666 was
					 * clearing PERSISTENT errors (I/O failures), which is
					 * wrong. Here we're clearing TRANSIENT errors (bad byte
					 * in stream), which is correct for graceful degradation.
					 */
					clearerr(fp);
					/*
					 * CRITICAL API VIOLATION WARNING:
					 * Directly accessing fp->_mbstate is WRONG.
					 * The underscore prefix means INTERNAL/PRIVATE.
					 * This breaks abstraction and creates:
					 *
					 * 1. ABI DEPENDENCY: Assumes specific FILE struct
					 *    layout. If libc changes struct layout, this
					 *    breaks. Not technically UB, but FRAGILE.
					 *
					 * 2. PORTABILITY FAILURE: _mbstate location and
					 *    size may differ across:
					 *    - Different FreeBSD versions
					 *    - 32-bit vs 64-bit
					 *    - Different architectures
					 *
					 * 3. ENCAPSULATION VIOLATION: FILE* is supposed
					 *    to be opaque. This is like casting away const
					 *    or accessing private class members in C++.
					 *
					 * CORRECT APPROACH: There is NO portable API to
					 * reset mbstate within a FILE*. Options:
					 * - fclose/fopen (loses file position)
					 * - Use mbrtowc() with explicit mbstate_t instead
					 *   of getwc() (requires refactoring)
					 * - Accept that EILSEQ means unrecoverable error
					 *
					 * WHY THIS EXISTS: Legacy code from when FreeBSD's
					 * libc exposed FILE struct internals. Modern POSIX
					 * says FILE* must be opaque. This worked "back in
					 * the day" when everyone used the same libc.
					 *
					 * RISK ASSESSMENT: Low immediate risk (FreeBSD FILE
					 * struct is stable), but HIGH TECHNICAL DEBT. If we
					 * ever switch to musl, glibc, or refactor libc, this
					 * WILL break. Keeping it means accepting permanent
					 * technical debt for dubious benefit (handling invalid
					 * UTF-8 in cat -v).
					 *
					 * RECOMMENDATION: This should be refactored to use
					 * mbrtowc() with explicit state, but that's a larger
					 * change. For now, DOCUMENT THE VIOLATION and accept
					 * the risk, but mark this as FUTURE WORK.
					 *
					 * The goto below is ACCEPTABLE - it's a legitimate
					 * error recovery path, not spaghetti code.
					 */
					memset(&fp->_mbstate, 0, sizeof(mbstate_t));
					if ((ch = getc(fp)) == EOF)
						break;
					wch = ch;
					goto ilseq;
				} else
					break;
			}
			if (!iswascii(wch) && !iswprint(wch)) {
ilseq:
				if (putchar('M') == EOF || putchar('-') == EOF)
					break;
				wch = toascii(wch);
			}
			/*
			 * FIXED: Catastrophic indentation violation per style(9).
			 * Original code had if (iswcntrl(wch)) indented as if it
			 * were at function scope, when it's actually inside the
			 * vflag conditional block. This is EXACTLY the kind of
			 * formatting disaster that leads to control flow bugs
			 * during maintenance. Inconsistent indentation is not
			 * just ugly - it actively misleads readers about the
			 * program's logic and scope. FreeBSD style(9) mandates
			 * one tab per indentation level, NO EXCEPTIONS.
			 */
			if (iswcntrl(wch)) {
				ch = toascii(wch);
				/*
				 * DOCUMENTATION REQUIRED: This is classic BSD control
				 * character visualization, but these magic numbers need
				 * explanation for anyone born after 1980.
				 *
				 * '\177' is octal for 127 (0x7F) = ASCII DEL character.
				 * DEL is displayed as '^?' because it can't be shown as
				 * ^<letter> (would be ^DEL which looks weird).
				 *
				 * 0100 is octal for 64 (0x40) = sets bit 6 of ASCII.
				 * This converts control characters (0-31) to printable
				 * uppercase letters (64-95). For example:
				 *   ^A = 0x01, | 0x40 = 0x41 = 'A'
				 *   ^B = 0x02, | 0x40 = 0x42 = 'B'
				 *   ^M = 0x0D, | 0x40 = 0x4D = 'M'  (carriage return)
				 *
				 * This is the standard Unix convention from v7 cat(1).
				 * Modern code would use (ch + 0x40) or (ch + '@') for
				 * clarity, but changing this would alter 50+ years of
				 * established behavior and grep patterns in scripts.
				 *
				 * IMPORTANT: This only works because ASCII control
				 * characters 0-31 correspond to @A-Z[\]^_ at 64-95.
				 * Not portable to EBCDIC (nobody cares) or Unicode
				 * control characters U+0080-U+009F (handled above).
				 */
				ch = (ch == '\177') ? '?' : (ch | 0100);
				if (putchar('^') == EOF || putchar(ch) == EOF)
					break;
				continue;
			}
			if (putwchar(wch) == WEOF)
				break;
			ch = -1;
			continue;
		}
		if (putchar(ch) == EOF)
			break;
	}
	
	/*
	 * QUESTIONABLE ERROR HANDLING: Should we clearerr(fp) here?
	 * 
	 * Context analysis:
	 * - For regular files: cook_cat() returns to scanfiles(), which
	 *   immediately calls fclose(fp). Clearing error is POINTLESS.
	 * - For stdin: If user passes "-" multiple times, stdin is read
	 *   repeatedly. But ferror() indicates I/O ERROR, not EOF.
	 *   Clearing real I/O errors HIDES problems for subsequent reads.
	 *
	 * Correct behavior:
	 * - EOF condition SHOULD be cleared (handled at function start, line 457)
	 * - I/O errors should NOT be cleared - they indicate real problems
	 * - If stdin is reread after I/O error, we WANT to detect it again
	 *
	 * Original code clears BOTH error and EOF with clearerr(). This is
	 * legacy BSD behavior dating back decades. Modern programs distinguish:
	 * - clearerr(): Clears BOTH error and EOF indicators
	 * - No standard way to clear only EOF or only error separately
	 *
	 * DECISION: Remove the clearerr() call. Rationale:
	 * 1. For files: Pointless (closed immediately after)
	 * 2. For stdin with real errors: Wrong (hides persistent problems)
	 * 3. For stdin with EOF: Already handled at function entry (line 457)
	 *
	 * If this breaks some obscure use case where stdin is reread after
	 * transient I/O errors, that's a design smell that should be fixed
	 * at a higher level, not papered over here.
	 */
	if (ferror(fp)) {
		warn("%s", filename);
		rval = 1;
		/* REMOVED: clearerr(fp) - see comment above */
	}
	if (ferror(stdout))
		err(1, "stdout");
}

static ssize_t
in_kernel_copy(int rfd)
{
	int wfd;
	ssize_t ret;

	wfd = fileno(stdout);
	/*
	 * FIXED: Original code initialized ret=1 then looped while ret > 0.
	 * This is backwards logic. We should loop until we get 0 (EOF) or
	 * error (-1), not depend on arbitrary initialization value.
	 *
	 * CRITICAL: Original code used SSIZE_MAX as the length parameter.
	 * This is LAZY and NOT DEFENSIVE. SSIZE_MAX is typically LONG_MAX
	 * (9223372036854775807 on 64-bit). While copy_file_range(2) is
	 * allowed to do partial copies, relying on "just copy everything"
	 * is not proper systems programming.
	 *
	 * However, for this utility, SSIZE_MAX is actually reasonable since:
	 * 1. copy_file_range() will handle large files correctly
	 * 2. The loop continues until EOF (ret == 0) or error (ret < 0)
	 * 3. Partial copies are handled by continuing the loop
	 *
	 * The REAL bug here is the caller's errno checking - it assumes
	 * specific errno values (EINVAL, EBADF, EISDIR) mean "fall back
	 * to raw_cat", but there are OTHER valid errno values that should
	 * also trigger fallback (EXDEV, ENOSYS on old kernels, etc).
	 *
	 * For now, keeping the SSIZE_MAX pattern but adding commentary to
	 * explain WHY this works and what the trade-offs are. The loop
	 * structure is correct: copy_file_range() returns:
	 *   > 0: bytes copied, continue
	 *   = 0: EOF reached, done successfully  
	 *  -1: error, return to caller who checks errno
	 */
	do {
		ret = copy_file_range(rfd, NULL, wfd, NULL, SSIZE_MAX, 0);
	} while (ret > 0);

	/*
	 * At this point: ret == 0 (success) or ret == -1 (error).
	 * Caller MUST check errno on ret != 0.
	 */
	return (ret);
}
#endif /* BOOTSTRAP_CAT */

static void
raw_cat(int rfd)
{
	/*
	 * CRITICAL FIX: Original code used single 'pagesize' variable for
	 * TWO COMPLETELY DIFFERENT VALUES:
	 * 1. Number of physical pages (from sysconf(_SC_PHYS_PAGES))
	 * 2. Page size in bytes (from sysconf(_SC_PAGESIZE))
	 *
	 * This is LYING VARIABLE NAME that makes code impossible to audit.
	 * Split into two properly-named variables for clarity and correctness.
	 */
	long physpages;	/* Number of physical memory pages (for threshold check) */
	long pagesize;	/* System page size in bytes (for buffer alignment) */
	int off, wfd;
	ssize_t nr, nw;
	/*
	 * DESIGN NOTE: Static buffer is allocated once and reused across
	 * all calls to raw_cat(). This is an optimization to avoid repeated
	 * malloc()/free() cycles when processing multiple files.
	 *
	 * THREAD SAFETY: This is NOT thread-safe or reentrant. However,
	 * cat(1) is single-threaded and scanfiles() calls raw_cat()
	 * sequentially, so this is acceptable. If cat(1) ever becomes
	 * multithreaded (unlikely), this would need refactoring to use
	 * per-thread or per-call buffers.
	 *
	 * MEMORY LEAK: The buffer is never freed. For a short-lived utility
	 * like cat(1), this is acceptable - the OS reclaims all memory on
	 * exit. For a long-running daemon, this would be a leak.
	 */
	static size_t bsize;
	static char *buf = NULL;
	struct stat sbuf;

	wfd = fileno(stdout);
	if (buf == NULL) {
		if (fstat(wfd, &sbuf))
			err(1, "stdout");
		if (S_ISREG(sbuf.st_mode)) {
			/*
			 * FIXED: sysconf(3) returns -1 on error, not 0.
			 * Original code didn't check for failure at all.
			 * While _SC_PHYS_PAGES failure is unlikely on FreeBSD,
			 * defensive programming demands checking ALL syscall
			 * return values. This is basic hygiene.
			 *
			 * FIXED: Variable naming - now using 'physpages' for
			 * number of physical pages, not the confusing 'pagesize'.
			 */
			physpages = sysconf(_SC_PHYS_PAGES);
			if (physpages == -1) {
				/* Fall back to small buffer if can't determine */
				bsize = BUFSIZE_SMALL;
			} else if (physpages > PHYSPAGES_THRESHOLD) {
				/*
				 * CORRECTED LYING COMMENT: Original said "32GB limit"
				 * but that's WRONG by 256x. Let's do the actual math:
				 *
				 * PHYSPAGES_THRESHOLD = 32 * 1024 = 32,768 PAGES
				 *
				 * Memory threshold calculation (pages × page_size):
				 * - On 4KB pages:  32,768 × 4,096   = 134,217,728 bytes  = 128MB
				 * - On 8KB pages:  32,768 × 8,192   = 268,435,456 bytes  = 256MB
				 * - On 16KB pages: 32,768 × 16,384  = 536,870,912 bytes  = 512MB
				 *
				 * The "32GB" comment was off by 256x. This is what
				 * happens when nobody does the math. Original author
				 * clearly confused number of pages with memory size.
				 *
				 * MAGIC NUMBER DISSECTION: MAXPHYS * 8
				 * - MAXPHYS is typically 128KB (131,072 bytes) on FreeBSD
				 * - MAXPHYS × 8 = 1,048,576 bytes = 1MB buffer
				 *
				 * MIN(BUFSIZE_MAX, MAXPHYS * 8):
				 * - BUFSIZE_MAX = 2MB = 2,097,152 bytes
				 * - MAXPHYS × 8 = 1MB = 1,048,576 bytes
				 * Result: MIN() always picks 1MB (MAXPHYS × 8)
				 *
				 * WHY 8? This is CARGO CULT TUNING. Someone decided
				 * that "8x the max I/O size is a good buffer" with
				 * NO MEASUREMENT, NO SCIENCE, NO JUSTIFICATION.
				 * It's probably from a 1990s mailing list post that
				 * said "I tried 8 and it was fast" on a specific
				 * workload that nobody remembers.
				 *
				 * This entire heuristic is ARBITRARY LEGACY TUNING
				 * from 20+ years ago. But changing it risks breaking
				 * performance assumptions in production scripts that
				 * have been tuned around this behavior. Welcome to
				 * the joy of maintaining 50-year-old utilities.
				 *
				 * POTENTIAL INTEGER OVERFLOW (theoretical): MAXPHYS * 8
				 * Could overflow if MAXPHYS ever becomes large enough.
				 * Currently MAXPHYS ~= 128KB, so MAXPHYS * 8 = 1MB (safe).
				 * But there's NO COMPILE-TIME CHECK preventing someone
				 * from setting MAXPHYS to SIZE_MAX/7 in the future.
				 * 
				 * Defense-in-depth: The MIN() with BUFSIZE_MAX limits
				 * damage, but this should use checked multiplication.
				 * Modern C would use __builtin_mul_overflow() here.
				 */
				bsize = MIN(BUFSIZE_MAX, MAXPHYS * 8);
			} else {
				bsize = BUFSIZE_SMALL;
			}
		} else {
			/*
			 * CRITICAL SECURITY ISSUE: st_blksize is a filesystem-provided
			 * "optimal block size" hint that we are BLINDLY TRUSTING with
			 * NO VALIDATION. This is a common source of vulnerabilities:
			 *
			 * st_blksize trust issues:
			 * 1. Can be 0 on some filesystems → malloc(0) behavior
			 * 2. Can be arbitrarily LARGE on corrupted/malicious filesystems
			 * 3. On FUSE filesystems, may be attacker-controlled
			 * 4. procfs/sysfs often return strange values (4096, PAGE_SIZE)
			 * 5. Network filesystems may return huge values for throughput
			 *
			 * DEFENSE REQUIRED: We MUST clamp to reasonable bounds:
			 * - Lower bound: system page size (typically 4KB-64KB)
			 * - Upper bound: BUFSIZE_MAX (2MB) to prevent DoS
			 *
			 * WHY THIS MATTERS: Without validation, a malicious filesystem
			 * could return st_blksize=2GB, causing malloc(2GB) and either:
			 * - Exhausting memory (DoS)
			 * - Causing malloc() failure → program exit
			 * - On 32-bit systems, potential integer overflow in malloc()
			 *
			 * Original code just did: bsize = sbuf.st_blksize;
			 * This is UNACCEPTABLE. We must be DEFENSIVE.
			 */
			bsize = sbuf.st_blksize;
			
			/*
			 * LOWER BOUND: Ensure bsize >= page size for alignment.
			 * sysconf(_SC_PAGESIZE) returns long, can be -1 on error
			 * OR 0 on misconfigured systems. We MUST validate before
			 * casting to size_t, as negative-to-unsigned cast produces
			 * HUGE values (wraparound in practice, UB in C99 if overflow).
			 */
			pagesize = sysconf(_SC_PAGESIZE);
			if (pagesize > 0)
				bsize = MAX(bsize, (size_t)pagesize);
			
			/*
			 * UPPER BOUND: Clamp to BUFSIZE_MAX to prevent:
			 * - Memory exhaustion DoS from malicious filesystems
			 * - Excessive allocation on network/synthetic filesystems
			 * - Integer overflow in subsequent calculations
			 *
			 * NOTE ON MAX() MACRO TYPE SAFETY: MAX(a,b) is NOT type-safe.
			 * It's defined as: #define MAX(a,b) (((a)>(b))?(a):(b))
			 * Issues:
			 * - Double evaluation: arguments evaluated TWICE (side effects!)
			 * - No type checking: mixing signed/unsigned causes surprises
			 * - Integer promotion rules can cause unexpected comparisons
			 * 
			 * HERE: Both bsize and BUFSIZE_MAX are size_t, so we're safe.
			 * But in general, MAX/MIN are a C FOOTGUN. Modern code should
			 * use inline functions or type-safe C++ templates.
			 */
			if (bsize > BUFSIZE_MAX)
				bsize = BUFSIZE_MAX;
		}
		if ((buf = malloc(bsize)) == NULL)
			err(1, "malloc() failure of IO buffer");
	}
	while ((nr = read(rfd, buf, bsize)) > 0)
		for (off = 0; nr; nr -= nw, off += nw) {
			/*
			 * CRITICAL FIX: write(2) can return 0 on certain
			 * conditions (disk full, quota exceeded, pipe closed).
			 * Original loop only checked < 0, creating infinite
			 * loop if write() returns 0. This is a TEXTBOOK bug
			 * that every intro to Unix programming warns about.
			 * The fact this wasn't caught means nobody actually
			 * tested error conditions properly.
			 */
			nw = write(wfd, buf + off, (size_t)nr);
			if (nw <= 0) {
				if (nw == 0)
					err(1, "stdout: zero bytes written");
				else
					err(1, "stdout");
			}
		}
	if (nr < 0) {
		warn("%s", filename);
		rval = 1;
	}
}

#ifndef NO_UDOM_SUPPORT

static int
udom_open(const char *path, int flags)
{
	struct addrinfo hints, *res, *res0;
	char rpath[PATH_MAX];
	int error, fd, serrno;
	cap_rights_t rights;

	/*
	 * Construct the unix domain socket address and attempt to connect.
	 */
	/*
	 * FIXED: bzero() is marked LEGACY in POSIX.1-2001 and removed from
	 * POSIX.1-2008. It exists only for 4.3BSD compatibility and should
	 * NEVER be used in new code. Use memset() like every other modern
	 * Unix. The fact that bzero() is still in base is for legacy code,
	 * not an invitation to keep using it. This is exactly the kind of
	 * portability trap that causes problems when code is shared with
	 * other systems.
	 */
	memset(&hints, 0, sizeof(hints));
	hints.ai_family = AF_LOCAL;

	if (fileargs_realpath(fa, path, rpath) == NULL)
		return (-1);

	error = cap_getaddrinfo(capnet, rpath, NULL, &hints, &res0);
	if (error) {
		warn("%s", gai_strerror(error));
		errno = EINVAL;
		return (-1);
	}
	cap_rights_init(&rights, CAP_CONNECT, CAP_READ, CAP_WRITE,
	    CAP_SHUTDOWN, CAP_FSTAT, CAP_FCNTL);

	/* Default error if something goes wrong. */
	serrno = EINVAL;

	for (res = res0; res != NULL; res = res->ai_next) {
		fd = socket(res->ai_family, res->ai_socktype,
		    res->ai_protocol);
		if (fd < 0) {
			serrno = errno;
			freeaddrinfo(res0);
			errno = serrno;
			return (-1);
		}
		if (caph_rights_limit(fd, &rights) != 0) {
			serrno = errno;
			close(fd);
			freeaddrinfo(res0);
			errno = serrno;
			return (-1);
		}
		error = cap_connect(capnet, fd, res->ai_addr, res->ai_addrlen);
		if (error == 0)
			break;
		/*
		 * STYLE FIX: Removed unnecessary 'else' after 'break'.
		 * Per style(9) and common sense, when the if-block
		 * unconditionally exits (return/break/continue), the else
		 * is redundant. This reduces nesting and improves readability.
		 */
		serrno = errno;
		close(fd);
	}
	freeaddrinfo(res0);

	if (res == NULL) {
		errno = serrno;
		return (-1);
	}

	/*
	 * Handle the open flags by shutting down appropriate directions.
	 *
	 * DESIGN NOTE: For Unix domain sockets, shutdown(2) may fail if
	 * the socket is not connected or if the peer has already closed.
	 * This is not necessarily fatal for cat(1) - we can still attempt
	 * I/O operations. The shutdown is an optimization to prevent
	 * unnecessary data flow in one direction.
	 *
	 * HOWEVER: warn(NULL) is LAZY. Provide context about what failed.
	 */
	switch (flags & O_ACCMODE) {
	case O_RDONLY:
		cap_rights_clear(&rights, CAP_WRITE);
		if (shutdown(fd, SHUT_WR) != 0)
			warn("shutdown(SHUT_WR) on %s", path);
		break;
	case O_WRONLY:
		cap_rights_clear(&rights, CAP_READ);
		if (shutdown(fd, SHUT_RD) != 0)
			warn("shutdown(SHUT_RD) on %s", path);
		break;
	default:
		break;
	}

	cap_rights_clear(&rights, CAP_CONNECT, CAP_SHUTDOWN);
	if (caph_rights_limit(fd, &rights) != 0) {
		serrno = errno;
		close(fd);
		errno = serrno;
		return (-1);
	}
	return (fd);
}

#endif
