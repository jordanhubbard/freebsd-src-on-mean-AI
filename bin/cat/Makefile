.include <src.opts.mk>

PACKAGE=runtime
PROG=	cat

.ifdef BOOTSTRAPPING
# For the bootstrap cat we disable all wide char support to allow building
# on Linux/macOS
CFLAGS+=-DBOOTSTRAP_CAT
.endif

HAS_TESTS=
SUBDIR.${MK_TESTS}+= tests

#
# CRITICAL BUILD INCONSISTENCY:
# Casper (capability mode) support is "temporarily" disabled here, but the
# C source code (cat.c) still contains all the Casper initialization and
# sandboxing logic (init_casper, caph_enter_casper, fileargs, etc).
#
# This means:
# 1. The Casper code is COMPILED IN but never linked against Casper libraries
# 2. The code is DEAD CODE that increases binary size for no benefit
# 3. Security features (Capsicum sandboxing) are DISABLED without obvious notice
# 4. Anyone reading the C code thinks cat is sandboxed, but it's NOT
#
# WHY DISABLED: Unknown. Comment says "temporary" but provides no context:
# - Was there a bug in Casper integration?
# - Performance regression?
# - Compatibility issue?
# - "Temporary" could mean 5 years ago...
#
# PROPER FIX: Either:
# A) Re-enable Casper (if whatever issue is resolved), OR
# B) Remove all Casper code from cat.c with #ifdef WITH_CASPER
#
# Current state is WORST OF BOTH WORLDS: Code complexity of Casper without
# security benefits. Technical debt accumulates.
#
# Temporary disable building cat with Casper.
#.if ${MK_CASPER} != "no" && !defined(RESCUE) && !defined(BOOTSTRAPPING)
#LIBADD+=        casper
#LIBADD+=        cap_fileargs
#LIBADD+=        cap_net
#CFLAGS+=-DWITH_CASPER
#.endif
# Depend on Makefile to rebuild when WITH_CASPER changes
cat.o:	Makefile

.include <bsd.prog.mk>
