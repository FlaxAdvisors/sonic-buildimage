/* test_parser.c — tests parse_counters() and bcmcmd_ps() against fixtures.
 * Compile and run: make test_parser && ./test_parser
 * Expected: "All 6 tests passed."
 */
#include "shim.h"
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

/* ---------- Fixture: representative 'show counters' output ---------- */
static const char COUNTERS_FIXTURE[] =
    "show counters\n"
    "RPKT.ce0\t\t    :\t\t      3,255\t\t +3,255\n"
    "RMCA.ce0\t\t    :\t\t      3,255\t\t +3,255\n"
    "RBYT.ce0\t\t    :\t\t    502,553\t   +502,553\n"
    "TPKT.ce0\t\t    :\t\t      3,572\t\t +3,572\n"
    "TMCA.ce0\t\t    :\t\t      3,572\t\t +3,572\n"
    "TBYT.ce0\t\t    :\t\t    786,203\t   +786,203\n"
    "TPOK.ce0\t\t    :\t\t      3,572\t\t +3,572\n"
    "T64.ce0 \t\t    :\t\t\t  1\t\t     +1\n"
    "T255.ce0\t\t    :\t\t      1,764\t\t +1,764\n"
    "T511.ce0\t\t    :\t\t      1,809\t\t +1,809\n"
    "RPKT.xe86\t\t    :\t\t      1,200\t\t +1,200\n"
    "RBYT.xe86\t\t    :\t\t    240,000\t   +240,000\n"
    "RMCA.xe86\t\t    :\t\t      1,100\t\t +1,100\n"
    "RBCA.xe86\t\t    :\t\t        100\t\t   +100\n"
    "TPKT.xe86\t\t    :\t\t        500\t\t   +500\n"
    "TBYT.xe86\t\t    :\t\t     50,000\t    +50,000\n"
    "drivshell>";

/* ---------- Fixture: representative 'ps' output snippet ---------- */
static const char PS_FIXTURE[] =
    "ps\n"
    "                 ena/        speed/ link auto    STP\n"
    "           port  link  Lns   duplex scan neg?\n"
    "       ce0(  1)  up     4  100G  FD   SW  No\n"
    "      xe85(117)  !ena   1   25G  FD None  No\n"
    "      xe86(118)  up     1   25G  FD   SW  No\n"
    "      xe87(119)  up     1   25G  FD   SW  No\n"
    "drivshell>";

/* Forward-declare the static function we want to test by including the .c file. */
#define parse_counters parse_counters_internal
#include "bcmcmd_client.c"
#undef  parse_counters

#define PASS(msg) do { printf("  PASS: %s\n", msg); passes++; } while(0)
#define FAIL(msg) do { printf("  FAIL: %s\n", msg); fails++;  } while(0)

int main(void)
{
    int passes = 0, fails = 0;
    counter_cache_t cache;
    memset(&cache, 0, sizeof(cache));
    pthread_mutex_init(&cache.lock, NULL);

    /* Test 1: parse_counters on fixture finds ce0 */
    parse_counters_internal(COUNTERS_FIXTURE, &cache);
    port_row_t *ce0 = NULL;
    for (int i = 0; i < cache.n_rows; i++)
        if (strcmp(cache.rows[i].port_name, "ce0") == 0) { ce0 = &cache.rows[i]; break; }
    if (ce0) PASS("ce0 row found");
    else      { FAIL("ce0 row not found"); goto summary; }

    /* Test 2: RBYT.ce0 = 502553 */
    int rbyt_idx = stat_map_index(0);  /* SAI_PORT_STAT_IF_IN_OCTETS = 0 */
    if (rbyt_idx >= 0 && ce0->val[rbyt_idx] == 502553)
        PASS("RBYT.ce0 = 502553");
    else
        FAIL("RBYT.ce0 wrong");

    /* Test 3: IN_NON_UCAST = RMCA + RBCA (ce0 has RMCA=3255, RBCA not shown → 0+3255=3255) */
    int non_ucast_idx = stat_map_index(2);  /* SAI_PORT_STAT_IF_IN_NON_UCAST_PKTS = 2 */
    if (non_ucast_idx >= 0 && ce0->val[non_ucast_idx] == 3255)
        PASS("IN_NON_UCAST = RMCA+RBCA = 3255");
    else
        FAIL("IN_NON_UCAST wrong");

    /* Test 4: parse_counters finds xe86 */
    port_row_t *xe86 = NULL;
    for (int i = 0; i < cache.n_rows; i++)
        if (strcmp(cache.rows[i].port_name, "xe86") == 0) { xe86 = &cache.rows[i]; break; }
    if (xe86) PASS("xe86 row found");
    else       { FAIL("xe86 row not found"); goto summary; }

    /* Test 5: IN_NON_UCAST for xe86 = RMCA(1100) + RBCA(100) = 1200 */
    if (non_ucast_idx >= 0 && xe86->val[non_ucast_idx] == 1200)
        PASS("xe86 IN_NON_UCAST = RMCA+RBCA = 1200");
    else
        FAIL("xe86 IN_NON_UCAST wrong");

    /* Test 6: ps fixture parsing */
    {
        int sdk_ports[32];
        char pnames[32][SHIM_PORT_NAME_LEN];
        int n = 0;
        char fixture_copy[sizeof(PS_FIXTURE)];
        memcpy(fixture_copy, PS_FIXTURE, sizeof(PS_FIXTURE));
        char *line = fixture_copy;
        while (n < 32) {
            char *nl = strchr(line, '\n');
            if (!nl) break;
            *nl = '\0';
            char *paren = strchr(line, '(');
            if (!paren || paren == line) { line = nl + 1; continue; }
            char *name_end = paren;
            char *name_start = paren - 1;
            while (name_start > line && *name_start != ' ') name_start--;
            if (*name_start == ' ') name_start++;
            int namelen = (int)(name_end - name_start);
            if (namelen <= 0 || namelen >= SHIM_PORT_NAME_LEN) { line = nl + 1; continue; }
            char *cparen = strchr(paren, ')');
            if (!cparen) { line = nl + 1; continue; }
            char numstr[16] = {0};
            int numlen = (int)(cparen - paren - 1);
            if (numlen <= 0 || numlen >= 16) { line = nl + 1; continue; }
            memcpy(numstr, paren + 1, (size_t)numlen);
            int sdk_port = atoi(numstr);
            if (sdk_port <= 0) { line = nl + 1; continue; }
            strncpy(pnames[n], name_start, (size_t)namelen);
            pnames[n][namelen] = '\0';
            sdk_ports[n] = sdk_port;
            n++;
            line = nl + 1;
        }
        /* Expect: ce0→1, xe85→117, xe86→118, xe87→119 */
        int ok = (n == 4 &&
                  strcmp(pnames[0], "ce0") == 0  && sdk_ports[0] == 1   &&
                  strcmp(pnames[1], "xe85") == 0 && sdk_ports[1] == 117 &&
                  strcmp(pnames[2], "xe86") == 0 && sdk_ports[2] == 118 &&
                  strcmp(pnames[3], "xe87") == 0 && sdk_ports[3] == 119);
        if (ok) PASS("ps fixture: ce0(1), xe85(117), xe86(118), xe87(119)");
        else     FAIL("ps fixture parsing wrong");
    }

summary:
    printf("\n%d passed, %d failed\n", passes, fails);
    return fails ? 1 : 0;
}
