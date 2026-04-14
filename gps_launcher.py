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

import asyncio, json, logging, math, os, random, signal, sys, time
from urllib.request import urlopen
from urllib.error import URLError
from aiohttp import web

META_PORT        = int(sys.argv[1]) if len(sys.argv) > 1 else 8090
SCAN_SEC         = 6
TUNNELD_HOST     = '127.0.0.1'
TUNNELD_PORT     = 49151
TUNNELD_BOOT_WAIT = 3
HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         'gps_map.html')

# ── GPS jitter (Ornstein-Uhlenbeck process) ─────────────────────────
# Models realistic iPhone GPS drift. Without jitter, the pushed track is
# mathematically perfect and trivially distinguishable from real GPS.
JITTER_ENABLED_DEFAULT = True
JITTER_TAU             = 8.0    # seconds, correlation time
JITTER_SIGMA           = 2.0    # meters, steady-state stddev
JITTER_OUT_PROB        = 0.01   # per-tick multipath outlier probability
JITTER_OUT_MULT        = 4.0
JITTER_TICK_SEC        = 1.0    # 1 Hz, matches real iPhone sampling

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
        self.target      = None
        self._worker_task = None
        self.jitter_on    = JITTER_ENABLED_DEFAULT
        self.jitter_sigma = JITTER_SIGMA
        self._nE = 0.0
        self._nN = 0.0
        self.state = {
            'connected': False, 'simulating': False,
            'last_lat': None, 'last_lon': None,
            'set_count': 0, 'error': None,
        }

    def to_dict(self):
        tlat = tlon = None
        if self.target is not None:
            tlat, tlon = self.target
        return {
            'idx': self.idx, 'udid': self.udid,
            'name': self.name, 'ios': self.ios,
            **self.state,
            'uptime_sec': int(time.time() - self._start_t),
            'tunnel_ok':  self.rsd_host is not None,
            'jitter_on': self.jitter_on,
            'jitter_sigma': self.jitter_sigma,
            'target_lat': tlat, 'target_lon': tlon,
        }

_devices: dict = {}
_tunneld_proc = None

# ── tunneld daemon lifecycle ─────────────────────────────────────────
async def start_tunneld():
    """Spawn pymobiledevice3 tunneld once. Idempotent: if a tunneld is
    already listening on 49151 we reuse it. Skips sudo wrapper when we're
    already root (so we keep direct control over the child for clean
    signal delivery)."""
    global _tunneld_proc
    if _probe_tunneld():
        log.info('Tunneld already running, reusing')
        return
    if os.geteuid() == 0:
        cmd = [sys.executable, '-m', 'pymobiledevice3', 'remote', 'tunneld']
        log.info('🚀 Starting tunneld daemon...')
    else:
        cmd = ['sudo', sys.executable, '-m', 'pymobiledevice3', 'remote', 'tunneld']
        log.info('🚀 Starting tunneld daemon (may prompt for sudo password)...')
    _tunneld_proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
        start_new_session=True,
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
    """Wait for tunneld (already asked to /shutdown) to exit, then SIGKILL
    its whole process group if still alive."""
    proc = _tunneld_proc
    if proc is None or proc.returncode is not None:
        return

    # If /shutdown worked, the process exits on its own within ~1s
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        return
    except asyncio.TimeoutError:
        pass

    # Fallback: kill the whole process group (covers sudo + child)
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), timeout=1.0)
    except asyncio.TimeoutError:
        log.warning('tunneld did not exit even after SIGKILL')

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

# ── GPS jitter helpers ────────────────────────────────────────────────
def _ou_step(nE: float, nN: float, sigma: float, dt: float, tau: float):
    """Advance Ornstein-Uhlenbeck noise one step. Exact discrete update."""
    decay = math.exp(-dt / tau)
    kick  = math.sqrt(1.0 - decay*decay) * sigma
    nE = nE*decay + kick*random.gauss(0.0, 1.0)
    nN = nN*decay + kick*random.gauss(0.0, 1.0)
    if random.random() < JITTER_OUT_PROB:
        nE += sigma * JITTER_OUT_MULT * random.gauss(0.0, 1.0)
        nN += sigma * JITTER_OUT_MULT * random.gauss(0.0, 1.0)
    return nE, nN

def _offset_latlon(lat: float, lon: float, dE: float, dN: float):
    """Apply east/north meter offset to WGS84 lat/lon."""
    R = 6378137.0
    return (lat + (dN / R) * (180.0/math.pi),
            lon + (dE / (R*math.cos(lat*math.pi/180.0))) * (180.0/math.pi))

async def _tick_once(loc, ctx: DeviceCtx):
    """Advance jitter (if enabled) and push the (possibly jittered) target."""
    if ctx.target is None:
        return
    tlat, tlon = ctx.target
    if ctx.jitter_on:
        ctx._nE, ctx._nN = _ou_step(ctx._nE, ctx._nN, ctx.jitter_sigma,
                                    JITTER_TICK_SEC, JITTER_TAU)
        lat, lon = _offset_latlon(tlat, tlon, ctx._nE, ctx._nN)
    else:
        lat, lon = tlat, tlon
    await loc.set(lat, lon)
    ctx.state['simulating'] = True
    ctx.state['last_lat']   = lat
    ctx.state['last_lon']   = lon
    ctx.state['set_count'] += 1
    log.debug(f'[{ctx.name}] 📍 {lat:.6f},{lon:.6f}')

# ── GPS worker ────────────────────────────────────────────────────────
async def gps_worker(ctx: DeviceCtx):
    from pymobiledevice3.remote.remote_service_discovery import RemoteServiceDiscoveryService
    from pymobiledevice3.services.dvt.instruments.dvt_provider import DvtProvider
    from pymobiledevice3.services.dvt.instruments.location_simulation import LocationSimulation

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

                        if ctx.target is not None:
                            await _tick_once(loc, ctx)
                            log.info(f'[{ctx.name}] ↩ Restored last position')

                        while True:
                            try:
                                cmd = await asyncio.wait_for(
                                    ctx._mailbox.wait(), timeout=JITTER_TICK_SEC)
                            except asyncio.TimeoutError:
                                # Periodic jitter tick (skip if jitter is off;
                                # static target doesn't need re-pushing)
                                if ctx.jitter_on:
                                    await _tick_once(loc, ctx)
                                continue

                            if isinstance(cmd, _ClearCmd):
                                await loc.clear()
                                ctx.target = None
                                ctx._nE = ctx._nN = 0.0
                                ctx.state.update(simulating=False, last_lat=None, last_lon=None)
                                log.info(f'[{ctx.name}] 🔴 GPS cleared')
                            elif isinstance(cmd, _SetCmd):
                                ctx.target = (cmd.lat, cmd.lon)
                                # Push immediately (with jitter if enabled)
                                await _tick_once(loc, ctx)
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

        # Mark gone devices offline; cancel their stuck workers
        for udid, ctx in _devices.items():
            if udid not in found and ctx.rsd_host is not None:
                ctx.rsd_host = None
                ctx.rsd_port = None
                ctx.state['connected'] = False
                log.info(f'[{ctx.name}] Device offline (idx={ctx.idx})')
                if ctx._worker_task and not ctx._worker_task.done():
                    ctx._worker_task.cancel()

        # Add new / update existing / revive offline devices
        for udid, (host, port) in found.items():
            if udid not in _devices:
                ctx = DeviceCtx(idx_counter, udid)
                idx_counter += 1
                ctx.rsd_host, ctx.rsd_port = host, port
                _devices[udid] = ctx
                log.info(f'Device found: {udid[-8:]}  idx={ctx.idx}  tunnel={host}:{port}')
                ctx._worker_task = asyncio.create_task(gps_worker(ctx))
            else:
                ctx = _devices[udid]
                was_offline = ctx.rsd_host is None
                if (ctx.rsd_host, ctx.rsd_port) != (host, port):
                    ctx.rsd_host, ctx.rsd_port = host, port
                    if not was_offline:
                        log.info(f'[{ctx.name}] Tunnel updated: {host}:{port}')
                if was_offline:
                    log.info(f'[{ctx.name}] Back online  tunnel={host}:{port}')
                    ctx._worker_task = asyncio.create_task(gps_worker(ctx))
                elif ctx._worker_task is None or ctx._worker_task.done():
                    ctx._worker_task = asyncio.create_task(gps_worker(ctx))

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

async def route_index(request):
    return web.FileResponse(HTML_PATH)

async def route_jitter(request):
    ctx = _get_ctx(request)
    if not ctx:
        return web.json_response({'ok': False, 'error': 'device not found'}, status=404)
    try:
        data = await request.json()
    except Exception as e:
        return web.json_response({'ok': False, 'error': str(e)}, status=400)
    if 'enabled' in data:
        ctx.jitter_on = bool(data['enabled'])
        if not ctx.jitter_on:
            ctx._nE = ctx._nN = 0.0
    if 'sigma' in data:
        try:
            s = float(data['sigma'])
        except (TypeError, ValueError):
            return web.json_response({'ok': False, 'error': 'invalid sigma'}, status=400)
        ctx.jitter_sigma = max(0.0, min(20.0, s))
    return web.json_response({
        'ok': True,
        'jitter_on': ctx.jitter_on,
        'jitter_sigma': ctx.jitter_sigma,
    })

# ── Entry point ───────────────────────────────────────────────────────
async def _request_tunneld_shutdown():
    """Ask tunneld to shut itself down via its REST endpoint."""
    def _hit():
        try:
            with urlopen(f'http://{TUNNELD_HOST}:{TUNNELD_PORT}/shutdown',
                         timeout=1) as r:
                r.read()
        except Exception:
            pass
    await asyncio.get_running_loop().run_in_executor(None, _hit)

async def on_startup(app):
    await start_tunneld()
    app['scanner_task'] = asyncio.create_task(device_scanner())
    log.info(f'🚀 GPS Launcher  port={META_PORT}')
    log.info(f'   🌐 UI   http://localhost:{META_PORT}/')
    log.info(f'   GET  http://localhost:{META_PORT}/devices')
    log.info(f'   POST http://localhost:{META_PORT}/device/{{idx}}/set')
    log.info(f'   POST http://localhost:{META_PORT}/device/{{idx}}/clear')
    log.info('Scanning devices via tunneld...')

async def on_cleanup(app):
    log.info('Shutting down...')
    tasks = []
    scanner = app.get('scanner_task')
    if scanner and not scanner.done():
        scanner.cancel()
        tasks.append(scanner)
    for ctx in _devices.values():
        t = ctx._worker_task
        if t and not t.done():
            t.cancel()
            tasks.append(t)
    if tasks:
        try:
            await asyncio.wait_for(
                asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
        except asyncio.TimeoutError:
            log.warning('Some tasks did not stop within 3s')
    await _request_tunneld_shutdown()
    await stop_tunneld()
    log.info('Stopped')

def main():
    app = web.Application()
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)

    app.router.add_get ('/',                    route_index)
    app.router.add_get ('/devices',             route_devices)
    app.router.add_post('/device/{idx}/set',    route_set)
    app.router.add_post('/device/{idx}/clear',  route_clear)
    app.router.add_post('/device/{idx}/jitter', route_jitter)
    app.router.add_get ('/device/{idx}/status', route_device_status)

    web.run_app(app, host='127.0.0.1', port=META_PORT,
                print=None, access_log=None)

if __name__ == '__main__':
    main()
