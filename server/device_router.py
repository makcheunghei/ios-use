"""Resolve explicit device selectors without changing another task's default device."""
import fcntl
import json
import re
import socket
import threading
import time

from wda_client import WDAError


def load_config(path):
    try:
        return json.loads((path / "config.json").read_text())
    except FileNotFoundError:
        return {}


class DeviceRouter:
    def __init__(self, runtime):
        self.root = runtime
        self.directory = runtime.state_dir / "devices"
        self.sessions = {}
        self.lock = threading.RLock()
        self.inventory = []
        self.updated = None

    def devices(self, refresh=False):
        with self.lock:
            if refresh or self.updated is None or time.monotonic() - self.updated > 15:
                result = self.root.setup_manager.discover()
                if not result.get("ok"):
                    raise WDAError("device_discovery_failed", "Xcode could not list physical devices.")
                self.inventory = [d for d in result["devices"] if d.get("udid") and d.get("available") is not False
                                  and d.get("connection_state") not in ("disconnected","unavailable")]
                self.updated = time.monotonic()
            return self.inventory

    def resolve(self, selector):
        # A previously resolved ID can be used after disconnect to inspect its setup job.
        if re.fullmatch(r"[A-Fa-f0-9-]{8,64}", selector):
            path = self.directory / selector
            if load_config(path).get("udid") == selector or self.root.setup_manager.config.get("udid") == selector:
                return selector
        matches = []
        for device in self.devices():
            model = (device.get("model") or device.get("name") or "").lower()
            if selector.lower() in (device["udid"].lower(), device.get("name", "").lower()) or (
                    selector.lower() in ("iphone", "ipad") and model.startswith(selector.lower())):
                matches.append(device["udid"])
        if len(matches) != 1:
            raise WDAError("ambiguous_device" if matches else "device_not_found",
                           "Select one physical device from pua_devices.", details={"candidates": matches})
        return matches[0]

    def dispatch(self, name, args):
        if not isinstance(args, dict):
            raise WDAError("invalid_argument","Tool arguments must be an object.",details={"action_executed":False})
        args = dict(args)
        selector = args.pop("device", None)
        if selector is not None and (not isinstance(selector, str) or not 1 <= len(selector) <= 128):
            raise WDAError("invalid_argument", "Invalid device selector.")
        if name == "pua_devices":
            if args or selector is not None:
                raise WDAError("invalid_argument", "pua_devices takes no arguments.")
            return {"devices": self.devices(refresh=True)}
        configure = name == "pua_setup" and args.get("action") == "configure"
        if selector is None:
            legacy = self.root.setup_manager.config.get("udid")
            if not isinstance(legacy,str) or not re.fullmatch(r"[A-Za-z0-9-]{8,64}",legacy):
                return self.root.call_local(name, args)
            if configure and args.get("udid",legacy) != legacy:
                return self.root.call_local(name, args)
            if not (self.directory / legacy / "config.json").is_file():
                result = self.root.call_local(name, args)
                if name in ("pua_ready","pua_screen","pua_screen_frame","pua_screen_action"):
                    result["device_id"] = legacy
                return result
            selector = legacy
        udid = self.resolve(selector)
        if not re.fullmatch(r"[A-Za-z0-9-]{8,64}", udid):
            raise WDAError("invalid_device", "Invalid physical device identifier.")
        path = self.directory / udid
        if configure:
            if args.get("udid", udid) != udid:
                raise WDAError("invalid_argument", "The configuration targets another device.")
            self.directory.mkdir(mode=0o700, exist_ok=True)
            with (self.directory / ".configure.lock").open("a") as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                configs = [load_config(p.parent) for p in self.directory.glob("*/config.json")]
                used = {c["local_port"] for c in configs if c["udid"] != udid}
                used.add(self.root.setup_manager.port)
                current = load_config(path)
                port = args.get("local_port", current.get("local_port"))
                if port is None:
                    port = next((p for p in range(18220, 18320) if p not in used and self.free_port(p)), None)
                if port is not None and (type(port) is not int or not 1024 <= port <= 65535):
                    raise WDAError("invalid_argument","Invalid loopback port.")
                if port is None or port in used:
                    raise WDAError("port_conflict", "Choose a separate loopback port for this device.")
                args.update(udid=udid, local_port=port)
                session = self.session(path, current.get("local_port",port))
                return session.call_local(name, args)
        config = load_config(path)
        if config.get("udid") == udid:
            session = self.session(path, config["local_port"])
        elif self.root.setup_manager.config.get("udid") == udid:
            session = self.root
        else:
            raise WDAError("device_not_configured", "Use pua_setup configure for this device.")
        try:
            result = session.call_local(name, args)
        except WDAError as error:
            self.bind_recovery(error.details, udid)
            raise
        self.bind_recovery(result,udid)
        if name in ("pua_ready", "pua_screen", "pua_screen_frame", "pua_screen_action"):
            result["device_id"] = udid
        return result

    @staticmethod
    def bind_recovery(result, udid):
        recovery = result.get("recovery")
        if isinstance(recovery, dict):
            for key in ("arguments", "next_arguments", "status_arguments"):
                if isinstance(recovery.get(key), dict):
                    recovery[key]["device"] = udid

    @staticmethod
    def free_port(port):
        with socket.socket() as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return True
            except OSError:
                return False

    def session(self, path, port):
        with self.lock:
            current = self.sessions.get(path)
            if current is not None and current.setup_manager.port != port:
                current.close()
                current = None
            if current is None:
                current = type(self.root)(path, f"http://127.0.0.1:{port}", routed=False)
                self.sessions[path] = current
            return current

    def close(self):
        for runtime in self.sessions.values():
            runtime.close()
