/* compat.c — glibc version compatibility shim.
 *
 * GCC 13+ compiles sscanf calls to __isoc23_sscanf (GLIBC_2.38) when targeting
 * C23.  The syncd Docker container is based on Debian bookworm with glibc 2.36
 * which lacks this symbol.  Providing it locally as an alias of __isoc99_sscanf
 * (available since glibc 2.17) removes the runtime dependency on glibc 2.38.
 */
#include <stdarg.h>
#include <stdio.h>

/* __isoc99_sscanf is the C99 version available in glibc 2.17+. */
extern int __isoc99_sscanf(const char *str, const char *fmt, ...);

int __isoc23_sscanf(const char *str, const char *fmt, ...)
{
    va_list ap;
    va_start(ap, fmt);
    int ret = vsscanf(str, fmt, ap);
    va_end(ap);
    return ret;
}
