#!/usr/bin/env python3
"""
gps_launcher.py

Real-time iPhone GPS location simulator launcher.
Manages tunneld daemon, device discovery (USB + Wi-Fi), and HTTP API.

Author  : Aroha Lin <https://github.com/ArohaLin>
Repo    : https://github.com/ArohaLin/iphone-gps-controller
License : MIT License
Copyright (c) 2026 Aroha Lin
"""

import asyncio, json, logging, signal, sys, time
from urllib.request import urlopen
from urllib.error import URLError
from aiohttp import web
from aiohttp.web_middlewares import middleware

META_PORT        = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
SCAN_SEC         = 6
TUNNELD_HOST     = '127.0.0.1'
TUNNELD_PORT     = 49151
TUNNELD_BOOT_WAIT = 3

# ── Logging ──────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s  %(levelname)-7s  %(message)s',
    datefmt='%H:%M:%S',
)
log = logging.getLogger('launcher')
logging.getLogger('aiohttp.access').setLevel(logging.WARNING)
logging.getLogger('aiohttp.server').setLevel(logging.WARNING)

# ── Command types ─────────────────────────────────────────────────────
class _SetCmd:
    __slots__ = ('lat', 'lon')
    def __init__(self, lat, lon): self.lat = lat; self.lon = lon

class _ClearCmd: pass

# ── Single-slot mailbox for latest command ────────────────────────────
class _Mailbox:
    def __init__(self):
        self._cmd   = None
        self._event = asyncio.Event()
    def post(self, cmd):
        self._cmd = cmd
        self._event.set()
    async def wait(self):
        await self._event.wait()
        self._event.clear()
        cmd, self._cmd = self._cmd, None
        return cmd

# ── Per-device context ────────────────────────────────────────────────
class DeviceCtx:
    def __init__(self, idx, udid):
        self.idx         = idx
        self.udid        = udid
        self.name        = f'iPhone ({udid[-4:]})'
        self.ios         = '?'
        self.rsd_host    = None
        self.rsd_port    = None
        self._mailbox    = _Mailbox()
        self._start_t    = time.time()
        self.state = {
            'connected': False, 'simulating': False,
            'last_lat': None, 'last_lon': None,
            'set_count': 0, 'error': None,
        }

    def to_dict(self):
        return {
            'idx': self.idx, 'udid': self.udid,
            'name': self.name, 'ios': self.ios,
            **self.state,
            'uptime_sec': int(time.time() - self._start_t),
            'tunnel_ok':  self.rsd_host is not None,
        }

_devices: dict = {}
_tunneld_proc = None

# ── tunneld daemon lifecycle ─────────────────────────────────────────
async def start_tunneld():
    """Spawn `sudo pymobiledevice3 remote tunneld` once. Idempotent: if a
    tunneld is already listening on 49151 we reuse it."""
    global _tunneld_proc
    if _probe_tunneld():
        log.info('Tunneld already running, reusing')
        return
    log.info('🚀 Starting tunneld daemon (may prompt for sudo password)...')
    _tunneld_proc = await asyncio.create_subprocess_exec(
        'sudo', sys.executable, '-m', 'pymobiledevice3', 'remote', 'tunneld',
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    # Wait for HTTP endpoint to come up
    for _ in range(TUNNELD_BOOT_WAIT * 5):
        await asyncio.sleep(0.2)
        if _probe_tunneld():
            log.info('✅ Tunneld ready')
            return
    log.warning('Tunneld did not respond within boot window; continuing anyway')

def _probe_tunneld() -> bool:
    try:
        with urlopen(f'http://{TUNNELD_HOST}:{TUNNELD_PORT}/hello', timeout=1) as r:
            return r.status == 200
    except (URLError, OSError):
        return False

async def stop_tunneld():
    proc = _tunneld_proc
    if proc is None or proc.returncode is not None:
        return
    try:
        proc.terminate()
        await asyncio.wait_for(proc.wait(), timeout=4.0)
    except asyncio.TimeoutError:
        try: proc.kill()
        except (ProcessLookupError, OSError): pass
        try: await asyncio.wait_for(proc.wait(), timeout=2.0)
        except asyncio.TimeoutError: pass
    except (ProcessLookupError, OSError):
        pass

# ── Scan devices via tunneld HTTP API ────────────────────────────────
async def scan_tunneld_devices() -> dict:
    """Return {udid: (tunnel_address, tunnel_port)}. Prefers non-USB (Wi-Fi)
    interface when a device has multiple tunnels active."""
    def _fetch():
        try:
            with urlopen(f'http://{TUNNELD_HOST}:{TUNNELD_PORT}/', timeout=2) as r:
                return json.loads(r.read().decode())
        except (URLError, OSError, ValueError):
            return {}
    data = await asyncio.get_running_loop().run_in_executor(None, _fetch)
    result = {}
    for udid, tunnels in data.items():
        if not tunnels:
            continue
        # Any tunnel works; first entry is fine
        t = tunnels[0]
        result[udid] = (t['tunnel-address'], t['tunnel-port'])
    return result

# ── GPS worker ────────────────────────────────────────────────────────
async def gps_worker(ctx: DeviceCtx):
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

    last_target = None

    while True:
        if not ctx.rsd_host:
            await asyncio.sleep(2)
            continue
        try:
            log.info(f'[{ctx.name}] Connecting GPS... ({ctx.rsd_host}:{ctx.rsd_port})')
            async with RemoteServiceDiscoveryService((ctx.rsd_host, ctx.rsd_port)) as rsd:
                # Populate device info from peer handshake
                try:
                    props = rsd.peer_info.get('Properties', {})
                    ctx.ios = props.get('OSVersion', ctx.ios)
                    ctx.name = props.get('ProductType', ctx.name)
                except Exception:
                    pass

                async with DvtProvider(rsd) as dvt:
                    async with LocationSimulation(dvt) as loc:
                        ctx.state['connected'] = True
                        ctx.state['error']     = None
                        log.info(f'[{ctx.name}] ✅ GPS connected')

                        if last_target:
                            await loc.set(last_target.lat, last_target.lon)
                            log.info(f'[{ctx.name}] ↩ Restored last position')

                        while True:
                            cmd = await ctx._mailbox.wait()
                            if isinstance(cmd, _ClearCmd):
                                await loc.clear()
                                last_target = None
                                ctx.state.update(simulating=False, last_lat=None, last_lon=None)
                                log.info(f'[{ctx.name}] 🔴 GPS cleared')
                            elif isinstance(cmd, _SetCmd):
                                await loc.set(cmd.lat, cmd.lon)
                                last_target = cmd
                                ctx.state['simulating'] = True
                                ctx.state['last_lat']   = cmd.lat
                                ctx.state['last_lon']   = cmd.lon
                                ctx.state['set_count'] += 1
                                log.debug(f'[{ctx.name}] 📍 {cmd.lat:.5f},{cmd.lon:.5f}')
        except Exception as e:
            ctx.state['connected']  = False
            ctx.state['simulating'] = False
            ctx.state['error']      = str(e)
            log.error(f'[{ctx.name}] Disconnected: {e}, retrying in 5s...')
            await asyncio.sleep(5)

# ── Device scanner ────────────────────────────────────────────────────
async def device_scanner():
    idx_counter = 0
    while True:
        found = await scan_tunneld_devices()

        # Remove gone devices
        for udid in list(_devices.keys()):
            if udid not in found:
                ctx = _devices.pop(udid)
                ctx.rsd_host = None
                ctx.rsd_port = None
                log.info(f'Device removed: {ctx.name}')

        # Add / refresh current devices
        for udid, (host, port) in found.items():
            if udid not in _devices:
                ctx = DeviceCtx(idx_counter, udid)
                idx_counter += 1
                ctx.rsd_host, ctx.rsd_port = host, port
                _devices[udid] = ctx
                log.info(f'Device found: {udid[-8:]}  tunnel={host}:{port}')
                asyncio.create_task(gps_worker(ctx))
            else:
                ctx = _devices[udid]
                if (ctx.rsd_host, ctx.rsd_port) != (host, port):
                    ctx.rsd_host, ctx.rsd_port = host, port
                    log.info(f'[{ctx.name}] Tunnel updated: {host}:{port}')

        await asyncio.sleep(SCAN_SEC)

# ── HTTP API ──────────────────────────────────────────────────────────
def _get_ctx(request):
    idx = int(request.match_info.get('idx', -1))
    for ctx in _devices.values():
        if ctx.idx == idx: return ctx
    return None

async def route_devices(request):
    return web.json_response([c.to_dict() for c in _devices.values()])

async def route_set(request):
    ctx = _get_ctx(request)
    if not ctx:
        return web.json_response({'ok': False, 'error': 'device not found'}, status=404)
    if not ctx.state['connected']:
        return web.json_response({'ok': False, 'error': 'GPS not connected'}, status=503)
    try:
        data = await request.json()
        lat, lon = float(data['lat']), float(data['lon'])
    except Exception as e:
        return web.json_response({'ok': False, 'error': str(e)}, status=400)
    ctx._mailbox.post(_SetCmd(lat, lon))
    return web.json_response({'ok': True, 'lat': lat, 'lon': lon})

async def route_clear(request):
    ctx = _get_ctx(request)
    if not ctx:
        return web.json_response({'ok': False, 'error': 'device not found'}, status=404)
    if not ctx.state['connected']:
        return web.json_response({'ok': False, 'error': 'GPS not connected'}, status=503)
    ctx._mailbox.post(_ClearCmd())
    return web.json_response({'ok': True})

async def route_device_status(request):
    ctx = _get_ctx(request)
    if not ctx:
        return web.json_response({'ok': False, 'error': 'device not found'}, status=404)
    return web.json_response(ctx.to_dict())

@middleware
async def cors_middleware(request, handler):
    if request.method == 'OPTIONS':
        return web.Response(headers={
            'Access-Control-Allow-Origin':  '*',
            'Access-Control-Allow-Methods': 'GET,POST,OPTIONS',
            'Access-Control-Allow-Headers': 'Content-Type',
        })
    resp = await handler(request)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    return resp

# ── Entry point ───────────────────────────────────────────────────────
async def main():
    await start_tunneld()
    asyncio.create_task(device_scanner())

    app = web.Application(middlewares=[cors_middleware])
    app.router.add_get ('/devices',             route_devices)
    app.router.add_post('/device/{idx}/set',    route_set)
    app.router.add_post('/device/{idx}/clear',  route_clear)
    app.router.add_get ('/device/{idx}/status', route_device_status)
    for path in ['/device/{idx}/set', '/device/{idx}/clear']:
        app.router.add_route('OPTIONS', path, lambda r: web.Response())

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, '127.0.0.1', META_PORT).start()

    log.info(f'🚀 GPS Launcher  port={META_PORT}')
    log.info(f'   GET  http://localhost:{META_PORT}/devices')
    log.info(f'   POST http://localhost:{META_PORT}/device/{{idx}}/set')
    log.info(f'   POST http://localhost:{META_PORT}/device/{{idx}}/clear')
    log.info('Scanning devices via tunneld...')

    await asyncio.Event().wait()

if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info('Stopping...')
    finally:
        try:
            asyncio.run(stop_tunneld())
        except Exception:
            pass
        log.info('Stopped')
