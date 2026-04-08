/**
 * @file compat.c
 * @brief glibc version compatibility shim for sscanf.
 *
 * GCC 13+ compiles sscanf calls to __isoc23_sscanf (GLIBC_2.38) when
 * targeting C23.  The syncd Docker container is based on Debian bookworm
 * with glibc 2.36 which lacks this symbol.  Providing it locally as an
 * alias of __isoc99_sscanf (available since glibc 2.17) removes the
 * runtime dependency on glibc 2.38.
 */
#include <stdarg.h>
#include <stdio.h>

/** __isoc99_sscanf is the C99 sscanf version available in glibc 2.17+. */
extern int __isoc99_sscanf(const char *str, const char *fmt, ...);

/**
 * @brief Compatibility wrapper for __isoc23_sscanf (C23/glibc 2.38).
 *
 * Implemented as a varargs pass-through to vsscanf so it works with any
 * glibc version that has __isoc99_sscanf.
 *
 * @param str Input string to scan.
 * @param fmt Format string.
 * @param ... Pointer arguments to receive parsed values.
 * @return Number of input items successfully matched and assigned.
 */
int __isoc23_sscanf(const char *str, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int ret = vsscanf(str, fmt, ap);
    va_end(ap);
    return ret;
}
