/**
 * @file wedge100s-bmc-auth.c
 * @brief Push SSH public key to OpenBMC via network (IPv6 link-local over usb0).
 *
 * Uses ssh-copy-id with OpenSSH's SSH_ASKPASS mechanism to feed the default
 * OpenBMC root password. No extra packages needed (ssh-copy-id and OpenSSH
 * are already part of the SONiC base image; sshpass is NOT in the SONiC apt
 * repos).
 *
 * The -f flag skips ssh-copy-id's "are keys already installed" probe — that
 * probe authenticates via password (through SSH_ASKPASS), which makes the
 * probe succeed and fool ssh-copy-id into thinking the key is already there.
 * Without -f: no-op every time. With -f: actual append; ssh-copy-id's remote
 * helper still dedups, so repeated invocations don't duplicate.
 *
 * If /etc/sonic/wedge100s-bmc-key{,.pub} is absent, generates an ed25519
 * keypair inline — self-contained, no race with postinst on first boot.
 *
 * Retries up to MAX_TRIES times at RETRY_DELAY second intervals to tolerate
 * BMC sshd not being ready when this tool first runs during boot.
 *
 * Replaces the earlier /dev/ttyACM0 TTY-based implementation which had
 * unreliable prompt detection (exited 0 without actually installing the key).
 *
 * Called from platform-init (do_install) on every SONiC boot.
 * Also called by wedge100s-bmc-daemon on every BMC reconnect, since the
 * BMC clears /root/.ssh/authorized_keys on every BMC reboot.
 *
 * Exits 0 on success, 1 on any failure after exhausting retries.
 *
 * Build: gcc -O2 -o wedge100s-bmc-auth wedge100s-bmc-auth.c
 */

#include <errno.h>
#include <fcntl.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/wait.h>
#include <unistd.h>

#define PRIVKEY_PATH  "/etc/sonic/wedge100s-bmc-key"
#define PUBKEY_PATH   "/etc/sonic/wedge100s-bmc-key.pub"
#define BMC_PASS      "0penBmc"
#define BMC_HOST      "root@fe80::ff:fe00:1%usb0"
/* /run is mounted noexec on SONiC; /tmp is tmpfs + exec OK */
#define ASKPASS_PATH  "/tmp/wedge100s-bmc-askpass.sh"
#define MAX_TRIES     6
#define RETRY_DELAY   5

/**
 * @brief Ensure the SONiC-side BMC SSH keypair exists, generating it if absent.
 *
 * Called before ssh-copy-id so this tool is idempotent and self-contained —
 * no race with postinst's bmc.provision_ssh_key() on first boot.
 *
 * @return 0 if the keypair is present (pre-existing or freshly generated),
 *         -1 if ssh-keygen failed.
 */
static int ensure_keypair(void)
{
    struct stat st;
    if (stat(PUBKEY_PATH, &st) == 0) return 0;

    fprintf(stderr, "wedge100s-bmc-auth: %s absent — generating ed25519 keypair\n",
            PUBKEY_PATH);
    int rc = system("ssh-keygen -q -t ed25519 -N '' -f " PRIVKEY_PATH);
    if (rc != 0) {
        fprintf(stderr, "wedge100s-bmc-auth: ssh-keygen failed (rc=%d)\n", rc);
        return -1;
    }
    return 0;
}

/**
 * @brief Write a one-shot SSH_ASKPASS helper script that echoes the BMC password.
 *
 * Lives under /run (tmpfs, cleared on reboot). Mode 0700; deleted after use.
 * The password (BMC_PASS) is the documented default OpenBMC root credential;
 * it is not meant to be private on this platform.
 *
 * @return 0 on success, -1 on write/chmod failure.
 */
static int write_askpass(void)
{
    int fd = open(ASKPASS_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0700);
    if (fd < 0) {
        fprintf(stderr, "wedge100s-bmc-auth: open %s: %s\n",
                ASKPASS_PATH, strerror(errno));
        return -1;
    }
    const char *body = "#!/bin/sh\necho '" BMC_PASS "'\n";
    ssize_t n = write(fd, body, strlen(body));
    close(fd);
    if (n != (ssize_t)strlen(body)) {
        fprintf(stderr, "wedge100s-bmc-auth: short write to %s\n", ASKPASS_PATH);
        return -1;
    }
    return 0;
}

/**
 * @brief Probe whether the SONiC-side key is already installed on the BMC.
 *
 * Runs ssh with pubkey-only authentication (password disabled, BatchMode). If
 * it connects, the key is already in the BMC's authorized_keys and we can
 * skip the push entirely — avoiding duplicate entries that ssh-copy-id -f
 * would otherwise accumulate.
 *
 * @return 1 if the key is already installed, 0 if not (or BMC unreachable).
 */
static int pubkey_already_works(void)
{
    const char *cmd =
        "ssh "
        "-o BatchMode=yes "
        "-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null "
        "-o ConnectTimeout=5 "
        "-o PubkeyAuthentication=yes "
        "-o PasswordAuthentication=no "
        "-o ControlMaster=no "
        "-o ControlPath=none "
        "-i " PRIVKEY_PATH " "
        BMC_HOST " true >/dev/null 2>&1";
    int rc = system(cmd);
    return WIFEXITED(rc) && WEXITSTATUS(rc) == 0;
}

int main(void)
{
    if (ensure_keypair() < 0) return 1;

    /* Idempotence guard: skip the whole dance if BMC already accepts our key. */
    if (pubkey_already_works()) {
        fprintf(stderr, "wedge100s-bmc-auth: BMC already accepts our key\n");
        return 0;
    }

    if (write_askpass() < 0) return 1;

    const char *cmd =
        "SSH_ASKPASS=" ASKPASS_PATH " "
        "SSH_ASKPASS_REQUIRE=force "
        "setsid -w ssh-copy-id -f "
        "-o StrictHostKeyChecking=no "
        "-o UserKnownHostsFile=/dev/null "
        "-o ConnectTimeout=5 "
        "-i " PRIVKEY_PATH " "
        BMC_HOST " </dev/null 2>&1";

    int final_rc = 1;
    for (int i = 1; i <= MAX_TRIES; i++) {
        int rc = system(cmd);
        if (WIFEXITED(rc) && WEXITSTATUS(rc) == 0) {
            fprintf(stderr,
                    "wedge100s-bmc-auth: BMC key push succeeded (attempt %d)\n", i);
            final_rc = 0;
            break;
        }
        if (i < MAX_TRIES) {
            fprintf(stderr,
                    "wedge100s-bmc-auth: attempt %d failed (exit %d), "
                    "retrying in %ds\n",
                    i, WIFEXITED(rc) ? WEXITSTATUS(rc) : -1, RETRY_DELAY);
            sleep(RETRY_DELAY);
        } else {
            fprintf(stderr,
                    "wedge100s-bmc-auth: BMC key push failed after %d attempts\n",
                    MAX_TRIES);
        }
    }

    unlink(ASKPASS_PATH);
    return final_rc;
}
