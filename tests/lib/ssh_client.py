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

    def connect(self):
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

    def run(self, cmd, timeout=60):
        """Run a shell command on the target.

        Returns:
            (stdout: str, stderr: str, exit_code: int)
        """
        if self._client is None:
            raise RuntimeError("Not connected — call connect() first")
        stdin, stdout, stderr = self._client.exec_command(cmd, timeout=timeout)
        exit_code = stdout.channel.recv_exit_status()
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        return out, err, exit_code

    def run_python(self, code, timeout=60):
        """Upload and execute a Python script on the target via SFTP.

        The script runs as ``sudo python3`` so platform drivers are accessible.

        Returns:
            (stdout: str, stderr: str, exit_code: int)
        """
        if self._client is None:
            raise RuntimeError("Not connected — call connect() first")
        script_path = f"/tmp/sonic_test_{uuid.uuid4().hex[:8]}.py"
        sftp = self._client.open_sftp()
        try:
            with sftp.file(script_path, "w") as fh:
                fh.write(code)
        finally:
            sftp.close()
        try:
            return self.run(f"sudo python3 {script_path}", timeout=timeout)
        finally:
            try:
                self.run(f"rm -f {script_path}", timeout=5)
            except Exception:
                pass

    def close(self):
        if self._client:
            self._client.close()
            self._client = None
