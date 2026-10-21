"""Robot Framework keywords that drive NSO and netsim containers for CI.

Runtime settings are read from environment variables:

  NSO_IMAGE         NSO production image (default: cisco-nso-prod:6.7)
  NSO_NED_TARBALL   Path to the IOS XR NED tarball on the runner (required)
  NETSIM_USERNAME   netsim login user (required)
  NETSIM_PASSWORD   netsim login password (required)
  NSO_CI_ID         Unique id used to name containers (default: local)
  NSO_CI_WORKDIR    Scratch directory (default: $RUNNER_TEMP/nso-ci-<id>)
  NSO_CI_KEEP       "true" keeps containers after the run for debugging
"""

import json
import os
import re
import shutil
import subprocess
import tarfile
import time
from pathlib import Path

import yaml
from robot.api import logger
from robot.api.deco import keyword, library

CLI_ERROR_RE = re.compile(r"^(syntax error:|Error:|Aborted:).*$", re.M)
NETSIM_FIRST_CLI_PORT = 10022
NETSIM_LIST_RE = re.compile(r"name=(?P<name>\S+) .*?cli=(?P<cli>\d+)")


def _require_env(name):
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"Environment variable {name} is not set")
    return value


@library(scope="GLOBAL", auto_keywords=False)
class NsoCiLibrary:

    def __init__(self, params_file, artifacts_dir):
        self.params = yaml.safe_load(Path(params_file).read_text())
        self.params_file = Path(params_file)
        self.artifacts = Path(artifacts_dir)
        self.artifacts.mkdir(parents=True, exist_ok=True)

        self.image = os.environ.get("NSO_IMAGE", "cisco-nso-prod:6.7")
        ci_id = re.sub(r"[^A-Za-z0-9_.-]", "-", os.environ.get("NSO_CI_ID", "local"))
        self.prefix = f"nso-ci-{ci_id}"
        self.nso = f"{self.prefix}-nso"
        self.netsim = f"{self.prefix}-netsim"
        self.network = f"{self.prefix}-net"
        default_workdir = Path(os.environ.get("RUNNER_TEMP", ".")) / self.prefix
        self.workdir = Path(os.environ.get("NSO_CI_WORKDIR", default_workdir)).resolve()
        self.packages_dir = self.workdir / "packages"
        self.ned_dir = None
        self.netsim_ports = {}
        self.snapshots = {}

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------
    def _run(self, args, input_text=None, check=True, timeout=600):
        logger.debug("$ " + " ".join(args))
        proc = subprocess.run(args, input=input_text, capture_output=True,
                              text=True, timeout=timeout)
        if check and proc.returncode != 0:
            raise AssertionError(
                f"Command failed ({proc.returncode}): {' '.join(args)}\n"
                f"{proc.stdout}\n{proc.stderr}")
        return proc.stdout

    def _cli(self, lines, secrets=(), check_errors=True):
        script = "\n".join(lines) + "\n"
        shown = script
        for secret in secrets:
            shown = shown.replace(secret, "********")
        logger.info("ncs_cli input:\n" + shown)
        out = self._run(["docker", "exec", "-i", self.nso,
                         "ncs_cli", "-C", "-u", "admin", "-g", "ncsadmin"],
                        input_text=script)
        logger.info("ncs_cli output:\n" + out)
        if check_errors:
            match = CLI_ERROR_RE.search(out)
            if match:
                raise AssertionError(f"NSO CLI error: {match.group(0)}\n\n{out}")
        return out

    def _save(self, name, text):
        path = self.artifacts / name
        path.write_text(text)
        logger.info(f"Saved {path}")
        return path

    def _container_running(self, name):
        out = self._run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                        check=False)
        return out.strip() == "true"

    def _device_names(self):
        return [d["name"] for d in self.params["prerequisites"]["devices"]]

    def _service_lines(self):
        svc = self.params["service"]
        lines = [f"l3vpn {svc['name']}",
                 f"bgp-as {svc['bgp-as']}",
                 f"route-target {svc['route-target']}"]
        for pe in ("pe1", "pe2"):
            for leaf in ("device", "interface", "ip-address", "prefix-length", "rd"):
                lines.append(f"{pe} {leaf} {svc[pe][leaf]}")
        lines.append("top")
        return lines

    def _extract_ned(self):
        tarball = Path(_require_env("NSO_NED_TARBALL"))
        if not tarball.is_file():
            raise AssertionError(f"NED tarball not found: {tarball}")
        with tarfile.open(tarball) as tar:
            top = {m.name.split("/")[0] for m in tar.getmembers()}
            if len(top) != 1:
                raise AssertionError(f"Unexpected NED tarball layout: {top}")
            tar.extractall(self.packages_dir, filter="tar")
        self.ned_dir = self.packages_dir / top.pop()
        logger.info(f"NED package: {self.ned_dir.name}")

    # ------------------------------------------------------------------
    # 1. NSO container
    # ------------------------------------------------------------------
    @keyword("Start NSO Container")
    def start_nso_container(self, timeout=300):
        self._remove_workdir()
        self.packages_dir.mkdir(parents=True)
        self._extract_ned()

        self._run(["docker", "network", "create", self.network])
        self._run(["docker", "run", "-d", "--name", self.nso,
                   "--network", self.network,
                   "-v", f"{self.packages_dir}:/nso/run/packages",
                   self.image])

        deadline = time.time() + int(timeout)
        while time.time() < deadline:
            if not self._container_running(self.nso):
                logs = self._run(["docker", "logs", self.nso], check=False)
                raise AssertionError(f"NSO container stopped:\n{logs}")
            health = self._run(["docker", "inspect", "-f",
                                "{{.State.Health.Status}}", self.nso],
                               check=False).strip()
            if health == "healthy":
                break
            time.sleep(3)
        else:
            raise AssertionError("NSO container did not become healthy")

        self.wait_for_package_up(self.ned_dir.name, timeout)
        version = self._cli(["show ncs-state version"])
        logger.info(f"NSO version: {version.strip()}")

    @keyword("Wait For Package Up")
    def wait_for_package_up(self, package, timeout=300):
        deadline = time.time() + int(timeout)
        out = ""
        while time.time() < deadline:
            out = self._cli([f"show packages package {package} oper-status"],
                            check_errors=False)
            if re.search(r"oper-status\s+up", out):
                return
            time.sleep(5)
        raise AssertionError(f"Package {package} is not up:\n{out}")

    # ------------------------------------------------------------------
    # 2. netsim devices
    # ------------------------------------------------------------------
    @keyword("Start Netsim Devices")
    def start_netsim_devices(self, timeout=180):
        names = self._device_names()
        script = ["set -e", "cd /tmp",
                  f"ncs-netsim --dir /tmp/netsim create-device /ned {names[0]}"]
        script += [f"ncs-netsim --dir /tmp/netsim add-device /ned {n}" for n in names[1:]]
        script += ["ncs-netsim --dir /tmp/netsim start",
                   "touch /tmp/ready", "tail -f /dev/null"]

        last_port = NETSIM_FIRST_CLI_PORT + len(names) - 1
        # The bundled netsim SSH host keys are readable by root only.
        self._run(["docker", "run", "-d", "--name", self.netsim, "-u", "root",
                   "--network", self.network,
                   "-v", f"{self.ned_dir}:/ned:ro",
                   "-p", f"127.0.0.1::{NETSIM_FIRST_CLI_PORT}-{last_port}",
                   "--entrypoint", "bash", self.image, "-c", "; ".join(script)])

        deadline = time.time() + int(timeout)
        while time.time() < deadline:
            if not self._container_running(self.netsim):
                logs = self._run(["docker", "logs", self.netsim], check=False)
                raise AssertionError(f"netsim container stopped:\n{logs}")
            ready = subprocess.run(["docker", "exec", self.netsim, "test", "-f",
                                    "/tmp/ready"]).returncode == 0
            if ready:
                break
            time.sleep(2)
        else:
            raise AssertionError("netsim devices did not start")

        listing = self._run(["docker", "exec", self.netsim,
                             "ncs-netsim", "--dir", "/tmp/netsim", "list"])
        logger.info(listing)
        for m in NETSIM_LIST_RE.finditer(listing):
            port = int(m.group("cli"))
            host = self._run(["docker", "port", self.netsim, f"{port}/tcp"])
            host_port = int(host.splitlines()[0].rsplit(":", 1)[1])
            self.netsim_ports[m.group("name")] = (port, host_port)
        missing = set(names) - set(self.netsim_ports)
        if missing:
            raise AssertionError(f"netsim devices missing: {missing}")

    @keyword("Register Netsim Devices")
    def register_netsim_devices(self):
        username = _require_env("NETSIM_USERNAME")
        password = _require_env("NETSIM_PASSWORD")
        ned_id = self.ned_dir.name
        lines = ["config",
                 f"devices authgroups group netsim default-map remote-name {username}"
                 f" remote-password {password}"]
        for name, (port, _) in self.netsim_ports.items():
            lines += [f"devices device {name} address {self.netsim} port {port}"
                      f" authgroup netsim device-type cli ned-id {ned_id} protocol ssh",
                      f"devices device {name} state admin-state unlocked"]
        lines += ["commit", "end",
                  "devices fetch-ssh-host-keys",
                  "devices sync-from"]
        out = self._cli(lines, secrets=[password])
        synced = re.findall(r"result true", out)
        if len(synced) < len(self.netsim_ports):
            raise AssertionError(f"sync-from failed:\n{out}")

    @keyword("Get Device Names")
    def get_device_names(self):
        return self._device_names()

    # ------------------------------------------------------------------
    # 3. prerequisite config
    # ------------------------------------------------------------------
    @keyword("Apply Prerequisite Config")
    def apply_prerequisite_config(self):
        pre = self.params["prerequisites"]
        asn = pre["bgp-as"]
        lines = ["config"]
        for dev in pre["devices"]:
            peers = [d["loopback0"] for d in pre["devices"] if d is not dev]
            lines += [f"devices device {dev['name']} config",
                      "interface Loopback 0",
                      f"ipv4 address {dev['loopback0']} 255.255.255.255",
                      "exit",
                      f"router bgp {asn}",
                      f"bgp router-id {dev['loopback0']}",
                      "address-family vpnv4 unicast",
                      "exit"]
            for peer in peers:
                lines += [f"neighbor {peer}",
                          f"remote-as {asn}",
                          "update-source Loopback0",
                          "address-family vpnv4 unicast",
                          "exit", "exit"]
            lines.append("top")
        lines += ["commit", "end"]
        self._cli(lines)

        shown = []
        for name in self._device_names():
            shown.append(self._cli([
                f"show running-config devices device {name} config interface Loopback",
                f"show running-config devices device {name} config router bgp"]))
        self._save("prerequisite_config.txt", "\n".join(shown))

    # ------------------------------------------------------------------
    # 4. service package
    # ------------------------------------------------------------------
    @keyword("Install Service Package")
    def install_service_package(self, package_dir, timeout=300):
        src = Path(package_dir).resolve()
        dst = self.packages_dir / src.name
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns(
            "load-dir", "__pycache__", "*.pyc"))

        # ncsc is executable by the image's nso user only, so build as that user
        # in a scratch copy that both the runner and the container can write.
        for path in [dst, *dst.rglob("*")]:
            extra = 0o777 if path.is_dir() else 0o666
            path.chmod(path.stat().st_mode | extra)
        build = self._run(["docker", "run", "--rm", "-v", f"{dst}:/pkg",
                           "--entrypoint", "sh", self.image, "-c",
                           "umask 000 && make -C /pkg/src clean all"])
        logger.info("Package build:\n" + build)
        self._save("package_build.txt", build)

        out = self._cli(["packages reload"])
        self._save("packages_reload.txt", out)
        if not re.search(rf"package {re.escape(src.name)}\s+result true", out):
            raise AssertionError(f"packages reload failed for {src.name}:\n{out}")
        self.wait_for_package_up(src.name, timeout)

    # ------------------------------------------------------------------
    # 5-7. service
    # ------------------------------------------------------------------
    @keyword("Apply Service And Validate")
    def apply_service_and_validate(self):
        out = self._cli(["config"] + self._service_lines() + ["validate", "abort"])
        if "Validation complete" not in out:
            raise AssertionError(f"validate failed:\n{out}")
        return out

    @keyword("Dry Run Service")
    def dry_run_service(self):
        native = self._cli(["config"] + self._service_lines() +
                           ["commit dry-run outformat native", "abort"])
        cli = self._cli(["config"] + self._service_lines() +
                        ["commit dry-run", "abort"])
        self._save("dryrun_native.txt", native)
        self._save("dryrun_cli.txt", cli)
        return native

    @keyword("Commit Service")
    def commit_service(self):
        out = self._cli(["config"] + self._service_lines() + ["commit", "end"])
        self._save("commit.txt", out)
        if "Commit complete." not in out:
            raise AssertionError(f"commit failed:\n{out}")
        return out

    @keyword("Service Should Be In Sync")
    def service_should_be_in_sync(self):
        name = self.params["service"]["name"]
        out = self._cli([f"l3vpn {name} check-sync", "devices check-sync"])
        if "in-sync true" not in out or "out-of-sync" in out:
            raise AssertionError(f"check-sync failed:\n{out}")
        svc = self._cli([f"show running-config l3vpn {name}"])
        self._save("service_instance.txt", svc)

    # ------------------------------------------------------------------
    # 7-8. device verification (pyATS / Genie)
    # ------------------------------------------------------------------
    def _get_device_running_config(self, device, label):
        from pyats.topology import loader

        _, host_port = self.netsim_ports[device]
        testbed = loader.load({
            "testbed": {"name": "nso-ci-netsim"},
            "devices": {device: {
                # netsim prompts differ from real IOS XR; the generic plugin
                # learns the hostname from the prompt instead.
                "os": "generic",
                "type": "router",
                "credentials": {"default": {
                    "username": _require_env("NETSIM_USERNAME"),
                    "password": _require_env("NETSIM_PASSWORD")}},
                "connections": {"cli": {
                    "protocol": "ssh", "ip": "127.0.0.1", "port": host_port,
                    "ssh_options": "-o StrictHostKeyChecking=no "
                                   "-o UserKnownHostsFile=/dev/null",
                    "arguments": {"init_exec_commands": [],
                                  "init_config_commands": [],
                                  "learn_hostname": True,
                                  "connection_timeout": 60}}}}}})
        dev = testbed.devices[device]
        dev.connect(log_stdout=False,
                    logfile=str(self.artifacts / f"pyats_{label}_{device}.log"))
        try:
            config = dev.execute("show running-config | nomore", timeout=120)
        finally:
            dev.disconnect()
        return config.replace("\r\n", "\n")

    @staticmethod
    def _parse_config(text):
        """Parse indented CLI config into a nested dict with Genie Config."""
        from genie.utils.config import Config

        lines = []
        for line in text.splitlines():
            body = line.strip()
            # netsim prints "exit"/"!" block terminators and pads some values.
            if not body or body in ("exit", "!") or body.startswith("show running-config"):
                continue
            indent = line[:len(line) - len(line.lstrip())]
            lines.append(indent + " ".join(body.split()))
        config = Config("\n".join(lines))
        config.tree()
        return config.config

    @keyword("Take Device Config Snapshot")
    def take_device_config_snapshot(self, device, label):
        """Fetch show running-config with pyATS and store raw text + Genie tree."""
        text = self._get_device_running_config(device, label)
        tree = self._parse_config(text)
        self.snapshots.setdefault(label, {})[device] = tree
        self._save(f"showrun_{label}_{device}.txt", text)
        self._save(f"showrun_{label}_{device}.json", json.dumps(tree, indent=2))
        return tree

    @keyword("Diff Device Config Snapshots")
    def diff_device_config_snapshots(self, device, before="before", after="after"):
        """Genie Diff between two snapshots; fails if the commit changed nothing."""
        from genie.utils.diff import Diff

        diff = Diff(self.snapshots[before][device], self.snapshots[after][device])
        diff.findDiff()
        text = str(diff)
        self._save(f"showrun_diff_{device}.txt", text + "\n")
        logger.info(f"{device} config diff ({before} -> {after}):\n{text}")
        if not text.strip():
            raise AssertionError(f"{device}: no config change after commit")
        return text

    @keyword("Device Config Should Contain Service Structure")
    def device_config_should_contain_service_structure(self, device, label="after"):
        """Checks that the service objects exist; values are left to the LLM review."""
        tree = self.snapshots[label][device]
        svc = self.params["service"]
        vrf = svc["name"]
        pe = next(svc[p] for p in ("pe1", "pe2") if svc[p]["device"] == device)

        def children(node, prefix):
            return [v for k, v in node.items() if k.startswith(prefix)]

        problems = []
        if f"vrf {vrf}" not in tree:
            problems.append(f"vrf {vrf} not found")

        intf = tree.get(f"interface GigabitEthernet {pe['interface']}")
        if intf is None:
            problems.append(f"interface GigabitEthernet {pe['interface']} not found")
        else:
            if f"vrf {vrf}" not in intf:
                problems.append("interface is not bound to the VRF")
            if not children(intf, "ipv4 address "):
                problems.append("interface has no IPv4 address")

        bgp = children(tree, "router bgp ")
        if not bgp:
            problems.append("router bgp not found")
        else:
            bgp_vrf = bgp[0].get(f"vrf {vrf}")
            if bgp_vrf is None or not children(bgp_vrf, "rd "):
                problems.append(f"BGP vrf {vrf} with rd not found")

        if problems:
            raise AssertionError(f"{device}: " + "; ".join(problems))

    # ------------------------------------------------------------------
    # teardown
    # ------------------------------------------------------------------
    @keyword("Collect Logs And Cleanup")
    def collect_logs_and_cleanup(self):
        logs_dir = self.artifacts / "nso-logs"
        subprocess.run(["docker", "cp", f"{self.nso}:/log/.", str(logs_dir)],
                       capture_output=True)
        if os.environ.get("NSO_CI_KEEP", "").lower() == "true":
            logger.warn(f"NSO_CI_KEEP=true: keeping containers {self.prefix}-*")
            return
        for name in (self.nso, self.netsim):
            subprocess.run(["docker", "rm", "-f", name], capture_output=True)
        subprocess.run(["docker", "network", "rm", self.network], capture_output=True)
        self._remove_workdir()

    def _remove_workdir(self):
        if not self.workdir.exists():
            return
        # The NSO container takes ownership of the bind-mounted packages dir.
        subprocess.run(["docker", "run", "--rm", "-u", "root",
                        "-v", f"{self.workdir}:/w", "--entrypoint", "sh", self.image,
                        "-c", "rm -rf /w/* /w/.[!.]*"], capture_output=True)
        shutil.rmtree(self.workdir, ignore_errors=True)
