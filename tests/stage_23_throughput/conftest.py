"""Stage 23 conftest — fixtures for host SSH connections and iperf3 availability."""

import configparser
import json
import os
import socket
import subprocess

import pytest

TOPOLOGY_PATH = os.path.join(os.path.dirname(__file__), '..', '..', 'tools', 'topology.json')
TARGET_CFG_DEFAULT = os.path.join(os.path.dirname(__file__), '..', 'target.cfg')


def _load_topology():
    with open(TOPOLOGY_PATH) as f:
        return json.load(f)


def _load_target_cfg(cfg_path):
    cfg = configparser.ConfigParser()
    cfg.read(cfg_path)
    return cfg


def _host_reachable(mgmt_ip, ssh_user, key_file, timeout=5):
    """Return True if we can open an SSH connection to mgmt_ip."""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {"hostname": mgmt_ip, "username": ssh_user, "timeout": timeout}
        if key_file:
            connect_kwargs["key_filename"] = os.path.expanduser(key_file)
        client.connect(**connect_kwargs)
        client.close()
        return True
    except Exception:
        return False


def _iperf3_available(mgmt_ip, ssh_user, key_file):
    """Return True if iperf3 binary is present on the remote host."""
    try:
        import paramiko
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        connect_kwargs = {"hostname": mgmt_ip, "username": ssh_user, "timeout": 10}
        if key_file:
            connect_kwargs["key_filename"] = os.path.expanduser(key_file)
        client.connect(**connect_kwargs)
        _, stdout, _ = client.exec_command("which iperf3 2>/dev/null; echo $?")
        rc = int(stdout.read().decode().strip().splitlines()[-1])
        client.close()
        return rc == 0
    except Exception:
        return False


def _run_on_host(mgmt_ip, ssh_user, key_file, cmd, timeout=30):
    """Run cmd on mgmt_ip via SSH; return (stdout, stderr, returncode)."""
    import paramiko
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    connect_kwargs = {"hostname": mgmt_ip, "username": ssh_user, "timeout": 10}
    if key_file:
        connect_kwargs["key_filename"] = os.path.expanduser(key_file)
    client.connect(**connect_kwargs)
    _, stdout, stderr = client.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode()
    err = stderr.read().decode()
    rc  = stdout.channel.recv_exit_status()
    client.close()
    return out, err, rc


@pytest.fixture(scope="session")
def topology():
    return _load_topology()


@pytest.fixture(scope="session")
def host_ssh_creds(request):
    cfg_path = request.config.getoption("--target-cfg", default=TARGET_CFG_DEFAULT)
    cfg = _load_target_cfg(cfg_path)
    ssh_user = cfg.get("hosts", "ssh_user", fallback="flax")
    key_file  = cfg.get("hosts", "key_file",  fallback="~/.ssh/id_rsa")
    return {"ssh_user": ssh_user, "key_file": key_file}


@pytest.fixture(scope="session")
def host_by_port(topology):
    """Dict mapping port name → host entry from topology.json."""
    return {h["port"]: h for h in topology.get("hosts", [])}
