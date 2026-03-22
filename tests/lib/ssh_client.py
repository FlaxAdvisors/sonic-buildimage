"""SSH client helper for Wedge 100S-32X SONiC platform tests."""

import configparser
import os
import uuid

try:
    import paramiko
except ImportError:
    raise ImportError("paramiko is required: python3.11 -m pip install paramiko")


class SSHClient:
    """SSH connection to a SONiC target device.

    Reads credentials from an INI-style target.cfg:

        [target]
        host = 192.168.1.100
        port = 22
        username = admin
        password = YourPassword
        # key_file = ~/.ssh/id_rsa   (optional; takes priority over password)
        # connect_timeout = 30       (optional)
    """

    def __init__(self, cfg_path):
        config = configparser.ConfigParser()
        if not config.read(cfg_path):
            raise FileNotFoundError(f"Cannot read config: {cfg_path}")
        sect = config["target"]
        self.host = sect["host"]
        self.port = int(sect.get("port", "22"))
        self.username = sect.get("username", "admin")
        self.password = sect.get("password", None)
        self.key_file = sect.get("key_file", None)
        self.connect_timeout = int(sect.get("connect_timeout", "30"))
        self._client = None

    def connect(self, retries=5, retry_delay=10):
        """Connect to the target, retrying on transient timeout failures.

        The Wedge 100S management SSH can be briefly unresponsive (~15-30s)
        due to BCM ASIC interrupt handling.  Retry up to `retries` times with
        `retry_delay` seconds between attempts.
        """
        import time
        last_exc = None
        for attempt in range(1, retries + 1):
            try:
                self._client = paramiko.SSHClient()
                self._client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                kwargs = dict(
                    hostname=self.host,
                    port=self.port,
                    username=self.username,
                    timeout=self.connect_timeout,
                    allow_agent=False,
                    look_for_keys=False,
                )
                if self.key_file:
                    kwargs["key_filename"] = os.path.expanduser(self.key_file)
                elif self.password:
                    kwargs["password"] = self.password
                self._client.connect(**kwargs)
                # Send SSH-level keepalives every 60 s.  If the switch stops
                # responding, paramiko closes the transport and any pending
                # recv_exit_status() unblocks with an exception — preventing
                # indefinite hangs during BCM IRQ storms or DHCP churn.
                self._client.get_transport().set_keepalive(60)
                return  # Success
            except Exception as exc:
                last_exc = exc
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None
                if attempt < retries:
                    time.sleep(retry_delay)
        raise last_exc

    def run(self, cmd, timeout=60):
        """Run a shell command on the target.

        Returns:
            (stdout: str, stderr: str, exit_code: int)
        """
        if self._client is None:
            raise RuntimeError("Not connected — call connect() first")
        # exec_command(timeout=timeout) caps the SSH channel-open negotiation at
        # the caller's timeout, so xcvrd I2C storms that stall channel opens for
        # ~57 s trigger a fast reconnect+retry rather than a multi-minute hang.
        try:
            stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            stdout.channel.settimeout(timeout)
            stderr.channel.settimeout(timeout)
        except (paramiko.ssh_exception.SSHException, EOFError, AttributeError):
            # Transport dropped or channel open timed out — reconnect and retry once.
            # exec_command(timeout=timeout) caps the SSH channel-open negotiation at
            # `timeout` seconds so that xcvrd I2C storms (which can stall channel opens
            # for ~57 s) cause a fast reconnect rather than a multi-minute hang.
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
            self.connect()
            stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
            stdout.channel.settimeout(timeout)
            stderr.channel.settimeout(timeout)
        try:
            stdin.close()
            if not stdout.channel.status_event.wait(timeout=timeout):
                raise TimeoutError(
                    f"Command timed out after {timeout}s: {cmd!r}"
                )
            exit_code = stdout.channel.exit_status
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
        finally:
            stdout.channel.close()
        return out, err, exit_code

    def run_python(self, code, timeout=60):
        """Upload and execute a Python script on the target.

        The script is transferred via base64-piped echo (not SFTP) so the
        upload goes through run() and respects the timeout — SFTP open/write
        calls have no timeout and stall indefinitely during BCM IRQ storms.

        The script runs as ``sudo python3`` so platform drivers are accessible.

        Returns:
            (stdout: str, stderr: str, exit_code: int)
        """
        import base64
        script_path = f"/tmp/sonic_test_{uuid.uuid4().hex[:8]}.py"
        encoded = base64.b64encode(code.encode()).decode()
        _, err, rc = self.run(
            f"echo {encoded} | base64 -d | sudo tee {script_path} > /dev/null",
            timeout=15,
        )
        if rc != 0:
            raise RuntimeError(f"Failed to upload test script: {err}")
        try:
            return self.run(f"sudo python3 {script_path}", timeout=timeout)
        finally:
            try:
                self.run(f"rm -f {script_path}", timeout=5)
            except Exception:
                pass

    def upload_file(self, local_path, remote_path):
        """Upload a local file to the target via SFTP.

        Args:
            local_path: Path to the local file to upload.
            remote_path: Destination path on the remote target.

        Raises:
            RuntimeError: If not connected.
            IOError: On SFTP transfer failure.
        """
        if self._client is None:
            raise RuntimeError("Not connected — call connect() first")
        sftp = self._client.open_sftp()
        try:
            sftp.put(local_path, remote_path)
        finally:
            sftp.close()

    def close(self):
        if self._client:
            self._client.close()
            self._client = None
