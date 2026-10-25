"""Runtime that owns the raw-TCP listener the tracker dials into.

This is the part of the old standalone bridge that has to be a *server*: it runs
an ``asyncio.start_server`` inside Home Assistant's event loop, decodes the Vega
binary and Wialon IPS streams, keeps the latest telemetry snapshot, and (only
when explicitly enabled) sends direct commands back to the vehicle.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import struct
import time
from typing import Any, Callable

from homeassistant.core import HomeAssistant

from . import protocol
from .const import DEFAULT_IMEI, ONLINE_TIMEOUT, SENSOR_KEY_MAP

_LOGGER = logging.getLogger(__name__)


# Sensors whose raw polarity is opposite the HA device_class meaning.
# central_lock: raw 1 == locked; HA binary_sensor LOCK uses on=unlocked.
_INVERT_BOOL = {"central_lock"}


def demux_protocol(prefix: bytes) -> str:
    """Classify an inbound connection by its first bytes.

    Wialon Combine starts 0x24 0x24; TLS 0x16; Wialon IPS '#'; everything else is
    treated as the native Vega binary protocol (whose frames begin with a length
    byte). Combine is checked first because a Vega frame may also begin with 0x24.
    """
    if prefix[:2] == b"\x24\x24":
        return "combine"
    if prefix[:1] == b"\x16":
        return "tls"
    if prefix[:1] == b"#":
        return "wialon"
    return "vega"


class CommandError(Exception):
    """Raised when a command cannot be dispatched to the vehicle."""


class VegaSession:
    """One authenticated Vega-binary connection and its pending request map."""

    def __init__(self, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        self.peer = writer.get_extra_info("peername")
        self.authenticated = False
        self.connected_at = time.time()
        self.last_seen = time.time()
        self.req_counter = 1000
        self.pending_requests: dict[int, asyncio.Future] = {}
        self.retried_pin = False

    def get_next_req_id(self) -> int:
        self.req_counter = (self.req_counter + 1) & 0x7FFFFFFF
        return self.req_counter

    async def send_raw(self, data: bytes) -> None:
        self.writer.write(data)
        await self.writer.drain()

    async def _request(self, frame: bytes, req_id: int, timeout: float = 6.0) -> dict:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self.pending_requests[req_id] = future
        await self.send_raw(frame)
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending_requests.pop(req_id, None)

    async def query_sensor(self, sensor_id: int, sub_id: int = 0, timeout: float = 6.0) -> dict | None:
        req_id = self.get_next_req_id()
        frame = protocol.build_query_sensor_frame(req_id, sensor_id, sub_id)
        try:
            res = await self._request(frame, req_id, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return res.get("sensor")

    async def set_setting(self, setting_id: int, sub_id: int, val: bytes, timeout: float = 6.0) -> dict:
        req_id = self.get_next_req_id()
        frame = protocol.build_set_setting_frame(req_id, setting_id, sub_id, val)
        t0 = time.time()
        try:
            res = await self._request(frame, req_id, timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": "timeout", "elapsed_ms": round((time.time() - t0) * 1000, 1)}
        data = res.get("data", b"")
        res_code = data[0] if len(data) > 0 else res.get("result_code", -1)
        accepted = res_code == 0
        return {
            "success": accepted,
            "setting_id": setting_id,
            "sub_id": sub_id,
            "result_code": res_code,
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
        }


class InjectionSession:
    """Injects queries/commands into a cloud-authenticated relayed session.

    In proxy mode the tracker is already authenticated by the OEM cloud, so HA can
    write frames straight into the tracker side of the relay (no PIN needed) and
    match the replies by request id as they pass by in the tracker->cloud pipe.
    A lock serialises writes to the shared tracker writer.
    """

    def __init__(self, writer: asyncio.StreamWriter, lock: asyncio.Lock) -> None:
        self.writer = writer
        self.lock = lock
        self.authenticated = True  # piggybacks the cloud's auth on this session
        self.authenticated_event = asyncio.Event()
        self.last_seen = time.time()
        self.req_counter = 0x50000000
        self.pending_requests: dict[int, asyncio.Future] = {}

    def get_next_req_id(self) -> int:
        self.req_counter = 0x50000000 | ((self.req_counter + 1) & 0x0FFFFFFF)
        return self.req_counter

    async def _request(self, frame: bytes, req_id: int, timeout: float = 6.0) -> dict:
        loop = asyncio.get_event_loop()
        future: asyncio.Future = loop.create_future()
        self.pending_requests[req_id] = future
        async with self.lock:
            self.writer.write(frame)
            await self.writer.drain()
        try:
            return await asyncio.wait_for(future, timeout=timeout)
        finally:
            self.pending_requests.pop(req_id, None)

    async def query_sensor(self, sensor_id: int, sub_id: int = 0, timeout: float = 6.0) -> dict | None:
        req_id = self.get_next_req_id()
        frame = protocol.build_query_sensor_frame(req_id, sensor_id, sub_id)
        try:
            res = await self._request(frame, req_id, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return res.get("sensor")

    async def set_setting(self, setting_id: int, sub_id: int, val: bytes, timeout: float = 6.0) -> dict:
        req_id = self.get_next_req_id()
        frame = protocol.build_set_setting_frame(req_id, setting_id, sub_id, val)
        try:
            res = await self._request(frame, req_id, timeout=timeout)
        except asyncio.TimeoutError:
            return {"success": False, "error": "timeout"}
        data = res.get("data", b"")
        res_code = data[0] if len(data) > 0 else res.get("result_code", -1)
        return {"success": res_code == 0, "setting_id": setting_id, "sub_id": sub_id, "result_code": res_code}


class VegaRuntime:
    """Owns the listener, the telemetry snapshot and the command path."""

    def __init__(
        self,
        hass: HomeAssistant,
        imei: str,
        port: int,
        pin: str,
        pin2: str,
        poll_interval: int,
        proxy_mode: bool = False,
        direct_control: bool = False,
        upstream: tuple[str, int] | None = None,
        learned_pin: str | None = None,
        learned_imei: str = "",
        persist_cb: Callable[[dict], None] | None = None,
    ) -> None:
        self.hass = hass
        self.imei = imei
        self.port = port
        self.pin = pin
        self.pin2 = pin2
        self.poll_interval = poll_interval
        # Two independent switches:
        #   proxy_mode     -> relay Vega connections to the cloud + learn PIN/IMEI
        #   direct_control -> terminate locally and allow commands (SET_SETTING)
        self.proxy_mode = proxy_mode
        self.direct_control = direct_control
        self.upstream = upstream
        self.learned_pin = learned_pin
        self.learned_imei = learned_imei or ""
        self._persist_cb = persist_cb

        self._server: asyncio.AbstractServer | None = None
        self._poll_task: asyncio.Task | None = None
        self._update_cb: Callable[[dict], None] | None = None

        self._vega_sessions: dict[Any, VegaSession] = {}
        self._wialon_last_seen: float = 0.0
        self._wialon_imei: str = learned_imei or ""

        self._sensors: dict[str, Any] = {}  # state_key -> value
        self._position: dict[str, Any] = {}  # latitude/longitude/... from Wialon
        self._last_remote_state: Any = None
        self._inject_session: InjectionSession | None = None  # active proxy injection
        self._can_scripts: list[dict] = []  # decoded Setting 5502 slots
        self._slot_names: dict[int, str] = {}  # slot index -> name (for button labels)

    @property
    def _auth_pin(self) -> str:
        """PIN HA presents to the tracker in direct mode: learned one wins."""
        return self.learned_pin or self.pin

    # --- lifecycle -----------------------------------------------------------

    def set_update_callback(self, cb: Callable[[dict], None]) -> None:
        self._update_cb = cb

    async def start(self) -> None:
        self.load_scripts()  # restore slot names/scripts from disk for labels
        self._server = await asyncio.start_server(self._handle_connection, "0.0.0.0", self.port)
        self._poll_task = self.hass.async_create_background_task(
            self._poll_loop(), name=f"vega_poll_{self.imei}"
        )
        _LOGGER.info(
            "Vega listener bound on 0.0.0.0:%s (proxy_mode=%s direct_control=%s)",
            self.port, self.proxy_mode, self.direct_control,
        )

    async def stop(self) -> None:
        if self._poll_task:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None
        if self._server:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            self._server = None

    # --- snapshot ------------------------------------------------------------

    def _active_vega_session(self) -> VegaSession | None:
        now = time.time()
        latest: VegaSession | None = None
        for s in list(self._vega_sessions.values()):
            if s.authenticated and (now - s.last_seen) < 120:
                if latest is None or s.last_seen > latest.last_seen:
                    latest = s
        return latest

    def snapshot(self) -> dict:
        now = time.time()
        last_vega = max((s.last_seen for s in self._vega_sessions.values()), default=0.0)
        last_seen = max(last_vega, self._wialon_last_seen)
        is_online = (now - last_seen) < ONLINE_TIMEOUT if last_seen else False

        data: dict[str, Any] = {}
        data.update(self._sensors)
        data.update(self._position)
        data["is_online"] = is_online
        speed = data.get("speed")
        data["is_moving"] = bool(is_online and isinstance(speed, (int, float)) and speed > 0)
        if last_seen:
            data["last_online"] = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(last_seen))
        # onboarding diagnostics
        data["learned_imei"] = self.imei if (self.imei and self.imei != DEFAULT_IMEI) else (self._wialon_imei or self.learned_imei or self.imei)
        data["pin_learned"] = bool(self.learned_pin)
        if self.proxy_mode and self.direct_control:
            data["mode"] = "proxy+direct"
        elif self.direct_control:
            data["mode"] = "direct"
        elif self.proxy_mode:
            data["mode"] = "proxy"
        else:
            data["mode"] = "idle"
        return data

    def _publish(self) -> None:
        if self._update_cb is None:
            return
        # An entity-update error (e.g. a bad value for one sensor) must never
        # propagate up and tear down the tracker connection.
        try:
            self._update_cb(self.snapshot())
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vega: publish/update error ignored (session kept): %s", err)

    def _ingest_sensor(self, sensor: dict) -> None:
        name = sensor.get("name")
        key = SENSOR_KEY_MAP.get(name)
        if key is not None:
            value = sensor.get("value")
            # Polarity fix: raw central_lock 1 == locked, but HA LOCK uses
            # on=unlocked, so invert.
            if name in _INVERT_BOOL and isinstance(value, bool):
                value = not value
            self._sensors[key] = value

    # --- connection demux ----------------------------------------------------

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        try:
            initial = await asyncio.wait_for(reader.read(1), timeout=1.5)
        except asyncio.TimeoutError:
            initial = b""
        # Wialon Combine opens with 0x24 0x24, but a Vega binary frame can also
        # start with 0x24 (payload length 36), so read the second byte to tell
        # them apart before classifying.
        if initial[:1] == b"\x24":
            try:
                initial += await asyncio.wait_for(reader.read(1), timeout=1.5)
            except asyncio.TimeoutError:
                pass

        kind = demux_protocol(initial)
        if kind == "tls":
            # A TLS ClientHello: this integration does not terminate TLS, drop it.
            _LOGGER.debug("Vega: dropping TLS connection from %s (not supported)", peer)
            writer.close()
            return
        if kind == "combine":
            await self._handle_combine(reader, writer, initial)
        elif kind == "wialon":
            await self._handle_wialon(reader, writer, initial)
        elif self.proxy_mode:
            # Relay this Vega connection to the cloud (keeps the OEM cloud/app
            # online) and learn PIN/IMEI. If direct_control is also on, HA injects
            # its own queries/commands into the same (cloud-authenticated) session
            # in parallel — the cloud relay keeps running.
            await self._handle_vega_proxy(reader, writer, initial)
        else:
            # No proxy: terminate locally (direct-only tracker). Commands need
            # direct_control and the (learned) PIN.
            await self._handle_vega(reader, writer, initial)

    async def _handle_combine(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial: bytes) -> None:
        """Stub for Wialon Combine (0x24 0x24).

        Combine is not decoded here yet (the integration expects a Wialon IPS +
        Vega binary tracker). This branch exists only to recognise Combine and
        keep it off the Vega binary path, where its bytes would be mis-parsed. It
        drains the connection quietly (no ACKs) instead of corrupting a session.
        """
        peer = writer.get_extra_info("peername")
        _LOGGER.warning(
            "Vega: Wialon Combine connection from %s is not supported (stub); draining. "
            "Point the tracker's telemetry slot at Wialon IPS (0x02) instead.", peer
        )
        try:
            while True:
                if not await reader.read(4096):
                    break
        except Exception:  # noqa: BLE001
            pass
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    # --- Vega binary ---------------------------------------------------------

    async def _handle_vega(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial: bytes) -> None:
        session = VegaSession(writer)
        peer = session.peer
        self._vega_sessions[peer] = session
        _LOGGER.info("Vega binary session from %s", peer)
        try:
            await session.send_raw(protocol.build_auth_frame(self._auth_pin))
            if not self.imei or self.imei == DEFAULT_IMEI:
                await session.send_raw(protocol.build_frame(protocol.CMD_GET_DEV_INF, b""))
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Vega: error sending auth to %s: %s", peer, err)

        buf = bytearray(initial)
        try:
            while True:
                chunk = await reader.read(2048)
                if not chunk:
                    break
                session.last_seen = time.time()
                buf.extend(chunk)
                for cmd, payload in protocol.iter_frames(buf):
                    await self._process_vega_frame(session, cmd, payload)
                self._publish()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Vega: connection error from %s: %s", peer, err)
        finally:
            self._vega_sessions.pop(peer, None)
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _process_vega_frame(self, session: VegaSession, cmd: int, payload: bytes) -> None:
        if cmd == protocol.CMD_AUTORIZATION:
            await session.send_raw(protocol.build_auth_ack_frame(True))
            session.authenticated = True
        elif cmd == protocol.CMD_AUTORIZATION_ANS:
            status = payload[0] if payload else 0
            if status == 0x01:
                session.authenticated = True
                _LOGGER.info("Vega: tracker %s authorized", session.peer)
            elif not session.retried_pin:
                session.retried_pin = True
                await session.send_raw(protocol.build_auth_frame(self.pin2))
            else:
                _LOGGER.warning("Vega: tracker %s auth rejected with both PINs", session.peer)
        elif cmd == protocol.CMD_COMMAND_ANS:
            parsed = protocol.parse_command_ans(payload)
            if parsed is None:
                return
            fut = session.pending_requests.get(parsed["req_id"])
            if fut is not None and not fut.done():
                fut.set_result(parsed)
            if "sensor" in parsed:
                self._ingest_sensor(parsed["sensor"])
        elif cmd == protocol.CMD_COMMAND:
            # Unsolicited telemetry/command frame from the tracker: ACK it.
            if len(payload) >= 4:
                req_id = struct.unpack("<I", payload[:4])[0]
                ans = struct.pack("<IBB", req_id, 0x00, 0)
                await session.send_raw(protocol.build_frame(protocol.CMD_COMMAND_ANS, ans))
        elif cmd == protocol.CMD_GET_DEV_INF_ANS:
            imei = protocol.parse_imei(payload)
            if imei:
                self._learn(imei=imei)
        elif cmd == protocol.CMD_GET_FILE:
            if len(payload) >= 4:
                req_id = struct.unpack("<I", payload[:4])[0]
                ans = struct.pack("<IB", req_id, 0x01)
                await session.send_raw(protocol.build_frame(protocol.CMD_GET_FILE_ANS, ans))

    # --- proxy / onboarding (MITM relay + learn PIN/IMEI) --------------------

    async def _handle_vega_proxy(self, reader, writer, initial: bytes) -> None:
        """Transparently relay a Vega connection to the cloud and learn PIN/IMEI.

        The tracker keeps talking to its real server through us; we only observe.
        The per-slot PIN (cloud->tracker CMD_AUTORIZATION) and the IMEI
        (tracker->cloud GET_DEV_INF_ANS) are captured so the user can then flip to
        direct control with a known PIN. Any CMD_COMMAND_ANS seen is also decoded
        so telemetry already appears during onboarding.
        """
        peer = writer.get_extra_info("peername")
        if not self.upstream:
            _LOGGER.warning("Vega proxy: no upstream configured, draining %s", peer)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(*self.upstream), timeout=8
            )
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Vega proxy: upstream %s unreachable: %s", self.upstream, err)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass
            return
        _LOGGER.info(
            "Vega proxy: relaying %s <-> %s:%s (direct_control=%s)",
            peer, *self.upstream, self.direct_control,
        )
        if initial:
            up_writer.write(initial)
            await up_writer.drain()

        # Injection runs in parallel with the relay: HA writes its own frames to
        # the tracker (shared writer, locked) and matches replies in the t2c pipe.
        tracker_lock = asyncio.Lock()
        inject = InjectionSession(writer, tracker_lock)
        self._inject_session = inject
        poll_task = self.hass.async_create_background_task(
            self._inject_poll_loop(inject), name="vega_inject_poll"
        )

        async def t2c() -> None:  # tracker -> cloud (relay + capture + resolve inject)
            pbuf = bytearray()
            try:
                while True:
                    chunk = await reader.read(4096)
                    if not chunk:
                        break
                    up_writer.write(chunk)
                    await up_writer.drain()
                    self._wialon_last_seen = time.time()
                    pbuf.extend(chunk)
                    for cmd, payload in protocol.iter_frames(pbuf):
                        self._capture("t2c", cmd, payload, inject)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    up_writer.close()
                except Exception:  # noqa: BLE001
                    pass

        async def c2t() -> None:  # cloud -> tracker (locked; shares writer with inject)
            pbuf = bytearray()
            try:
                while True:
                    chunk = await up_reader.read(4096)
                    if not chunk:
                        break
                    async with tracker_lock:
                        writer.write(chunk)
                        await writer.drain()
                    pbuf.extend(chunk)
                    for cmd, payload in protocol.iter_frames(pbuf):
                        self._capture("c2t", cmd, payload, inject)
            except Exception:  # noqa: BLE001
                pass
            finally:
                try:
                    writer.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await asyncio.gather(t2c(), c2t())
        finally:
            if poll_task:
                poll_task.cancel()
            if self._inject_session is inject:
                self._inject_session = None

    async def _inject_poll_loop(self, inject: "InjectionSession") -> None:
        """Poll sensors by injecting queries into the relayed (cloud-authed) session."""
        try:
            # Wait for cloud authorization before injecting queries
            await asyncio.wait_for(inject.authenticated_event.wait(), timeout=15.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return
        await asyncio.sleep(2.0)
        while True:
            try:
                changed = False
                for sid in protocol.SENSOR_DEFS:
                    sensor = await inject.query_sensor(sid, 0)
                    if sensor is not None:
                        self._ingest_sensor(sensor)
                        changed = True
                if changed:
                    self._publish()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                break
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Vega inject poll error: %s", err)
                await asyncio.sleep(self.poll_interval)

    def _capture(self, direction: str, cmd: int, payload: bytes, inject: "InjectionSession | None" = None) -> None:
        if direction == "c2t" and cmd == protocol.CMD_AUTORIZATION:
            pin = protocol.parse_authorization_pin(payload)
            if pin and pin != self.learned_pin:
                self._learn(pin=pin)
        elif direction == "t2c" and cmd == protocol.CMD_AUTORIZATION_ANS:
            status = payload[0] if payload else 0
            if status == 0x01 and inject is not None:
                inject.authenticated_event.set()
        elif direction == "t2c" and cmd == protocol.CMD_GET_DEV_INF_ANS:
            imei = protocol.parse_imei(payload)
            if imei:
                self._learn(imei=imei)
        elif cmd == protocol.CMD_COMMAND_ANS:
            parsed = protocol.parse_command_ans(payload)
            if not parsed:
                return
            # resolve an injected request if this reply is one of ours
            if inject is not None:
                fut = inject.pending_requests.get(parsed["req_id"])
                if fut is not None and not fut.done():
                    fut.set_result(parsed)
            if "sensor" in parsed:
                self._ingest_sensor(parsed["sensor"])
                self._publish()

    def _learn(self, pin: str | None = None, imei: str | None = None) -> None:
        changed = {}
        if pin and pin != self.learned_pin:
            self.learned_pin = pin
            changed["learned_pin"] = pin
            _LOGGER.info("Vega proxy: learned PIN for this slot")
        if imei and imei != self.learned_imei and imei != DEFAULT_IMEI:
            self._wialon_imei = imei
            self.imei = imei
            self.learned_imei = imei
            changed["learned_imei"] = imei
            changed["imei"] = imei
            _LOGGER.info("Vega: learned tracker IMEI: %s", imei)
            self.load_scripts()
        if changed and self._persist_cb is not None:
            try:
                self._persist_cb(changed)
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Vega: persist learned data failed: %s", err)
        self._publish()

    # --- Wialon IPS (telemetry/position only, no Traccar) --------------------

    async def _handle_wialon(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, initial: bytes) -> None:
        peer = writer.get_extra_info("peername")
        _LOGGER.info("Wialon IPS session from %s", peer)
        buf = bytearray(initial)
        try:
            while True:
                chunk = await reader.read(2048)
                if not chunk:
                    break
                self._wialon_last_seen = time.time()
                buf.extend(chunk)
                while b"\n" in buf:
                    idx = buf.find(b"\n")
                    line_bytes = bytes(buf[: idx + 1])
                    del buf[: idx + 1]
                    line = line_bytes.decode("utf-8", errors="replace").strip()
                    if line:
                        await self._process_wialon_line(writer, line)
                self._publish()
        except Exception as err:  # noqa: BLE001
            _LOGGER.warning("Wialon: connection error from %s: %s", peer, err)
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _process_wialon_line(self, writer: asyncio.StreamWriter, line: str) -> None:
        async def _send(data: bytes) -> None:
            writer.write(data)
            await writer.drain()

        if line.startswith("#L#"):
            payload = line[3:].rstrip("#")
            parts = payload.split(";")
            if len(parts) >= 2 and parts[0] in ("2.0", "1.1"):
                self._wialon_imei = parts[1]
            else:
                self._wialon_imei = parts[0] if parts else ""
            await _send(b"#AL#1\r\n")
            if self._wialon_imei and self._wialon_imei != DEFAULT_IMEI:
                self._learn(imei=self._wialon_imei)
            return
        if line.startswith("#P#"):
            await _send(b"#AP#\r\n")
            return

        if line.startswith("#D#") or line.startswith("#SD#") or line.startswith("#B#"):
            parsed = protocol.parse_wialon_data(line)
            if parsed:
                params = parsed.pop("params", None)
                for key, value in parsed.items():
                    if value is not None:
                        self._position[key] = value
                if isinstance(params, dict):
                    self._position.setdefault("_params", {}).update(params)
            if line.startswith("#D#"):
                await _send(b"#AD#1\r\n")
            elif line.startswith("#SD#"):
                await _send(b"#AD#1\r\n")
            else:
                cnt = max(1, line.count("|"))
                await _send(f"#AB#{cnt}\r\n".encode("ascii"))

    # --- poll loop + command path -------------------------------------------

    async def _poll_loop(self) -> None:
        await asyncio.sleep(1.0)
        while True:
            try:
                session = self._active_vega_session()
                if session is not None:
                    changed = False
                    for sid in protocol.SENSOR_DEFS:
                        sensor = await session.query_sensor(sid, 0)
                        if sensor is not None:
                            self._ingest_sensor(sensor)
                            changed = True
                    if self.direct_control:
                        await self._remote_state_watchdog(session)
                    if changed:
                        self._publish()
                await asyncio.sleep(self.poll_interval)
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001
                _LOGGER.debug("Vega poll loop error: %s", err)
                await asyncio.sleep(self.poll_interval)

    async def _remote_state_watchdog(self, session: VegaSession) -> None:
        """Clear the levelled output 1341 when a Remote session ends by timer.

        This is a *write* to the vehicle, so it only runs when commands are
        enabled by the owner. 3 -> not-3 means the car left Remote on its own.
        """
        sensor = await session.query_sensor(protocol.REMOTE_STATE_SENSOR, 0)
        cur = sensor.get("value") if sensor else None
        prev = self._last_remote_state
        self._last_remote_state = cur
        if prev == 3 and cur is not None and cur != 3:
            _LOGGER.info("Vega: Remote session ended (%s -> %s), clearing output 1341", prev, cur)
            await session.set_setting(protocol.REMOTE_LEVEL_SETTING, 0, bytes([0]))

    async def async_poll_once(self) -> None:
        """One-shot sensor poll (used for a quick post-command confirmation)."""
        session = self._inject_session or self._active_vega_session()
        if session is None:
            return
        changed = False
        for sid in protocol.SENSOR_DEFS:
            try:
                sensor = await session.query_sensor(sid, 0)
            except Exception:  # noqa: BLE001
                sensor = None
            if sensor is not None:
                self._ingest_sensor(sensor)
                changed = True
        if changed:
            self._publish()

    # --- CAN scripts (Setting 5502 slots): read / view / write ---------------

    def slot_name(self, slot: int) -> str | None:
        return self._slot_names.get(slot)

    @property
    def can_scripts(self) -> list[dict]:
        return self._can_scripts

    def _scripts_path(self) -> str:
        return os.path.join(self.hass.config.path("vega"), f"can_scripts_{self.imei}.json")

    def load_scripts(self) -> None:
        """Load the last-saved scripts from disk so labels survive a restart."""
        try:
            path = self._scripts_path()
            if os.path.exists(path):
                with open(path, encoding="utf-8") as f:
                    self._can_scripts = json.load(f)
                for s in self._can_scripts:
                    if not s.get("summary"):
                        s["summary"] = protocol.explain_can_script(s)
                    for f_item in s.get("frames", []):
                        if not f_item.get("description"):
                            f_item["description"] = protocol.explain_can_frame(
                                f_item.get("bus", 0), f_item.get("can_id", 0), f_item.get("data", "")
                            )
                self._slot_names = {
                    int(s["slot"]): s["name"] for s in self._can_scripts if s.get("name")
                }
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vega: load scripts failed: %s", err)

    async def _persist_scripts(self) -> None:
        path = self._scripts_path()
        data = self._can_scripts

        def _write() -> None:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)

        try:
            await self.hass.async_add_executor_job(_write)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Vega: persist scripts failed: %s", err)

    async def async_read_can_scripts(self, slots: int = 8) -> list[dict]:
        """Read Setting 5502 slots from the tracker, decode, store and persist."""
        session = self._inject_session or self._active_vega_session()
        if session is None:
            raise CommandError("Vehicle is offline (no active Vega session)")
        scripts: list[dict] = []
        names: dict[int, str] = {}
        for slot in range(slots):
            sensor = await session.query_sensor(5502, slot)
            hx = (sensor or {}).get("raw_hex", "")
            if not hx:
                continue
            buf = bytes.fromhex(hx)
            if len(buf) < 82:
                continue  # empty slot
            dec = protocol.decode_can_script(buf)
            dec["slot"] = slot
            scripts.append(dec)
            if dec.get("name"):
                names[slot] = dec["name"]
        self._can_scripts = scripts
        self._slot_names = names
        await self._persist_scripts()
        self._publish()
        _LOGGER.info("Vega: read %d CAN script slot(s)", len(scripts))
        return scripts

    async def async_write_can_script(self, slot: int, spec: dict) -> dict:
        """Build a 5502 buffer from a decoded-shape dict and write it to the tracker."""
        if not self.direct_control:
            raise CommandError("Direct control is off; enable it before writing scripts.")
        session = self._inject_session or self._active_vega_session()
        if session is None:
            raise CommandError("Vehicle is offline (no active Vega session)")
        buf = protocol.build_can_script_from_dict(spec)
        if len(buf) != 82:
            raise CommandError(f"built buffer has wrong length {len(buf)} (expected 82)")
        res = await session.set_setting(5502, int(slot), buf)
        if not res.get("success"):
            raise CommandError(f"Writing slot {slot} was not accepted: {res}")
        _LOGGER.info("Vega: wrote CAN script slot %s (%s)", slot, spec.get("name"))
        return res

    async def async_send_command(self, action: str) -> dict:
        if not self.direct_control:
            raise CommandError("Direct control is off; enable the 'Direct control' switch first.")
        spec = protocol.COMMAND_MAP.get(action)
        if spec is None:
            raise CommandError(f"Unknown Vega command '{action}'")
        # Prefer the proxy injection session (commands ride the cloud-authed relay);
        # otherwise use a locally-terminated authenticated session.
        session = self._inject_session or self._active_vega_session()
        if session is None:
            raise CommandError("Vehicle is offline (no active Vega session)")

        sid, sub, val = spec["setting_id"], spec["sub_id"], bytes([spec["val"]])
        # The car reacts to a 0->1 edge, not a level. If 1341 is already armed the
        # command is accepted (res=0) yet silently ignored, so pre-clear it.
        if sid == protocol.REMOTE_LEVEL_SETTING and spec["val"] == 1:
            await session.set_setting(protocol.REMOTE_LEVEL_SETTING, 0, bytes([0]))
            await asyncio.sleep(1.0)
        result = await session.set_setting(sid, sub, val)
        if not result.get("success"):
            raise CommandError(f"Vega command '{action}' was not accepted: {result}")
        return result
