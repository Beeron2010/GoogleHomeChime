import asyncio, ipaddress, json, os, socket, sqlite3, subprocess, tempfile, threading, time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

import pychromecast
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

DATA = Path(os.getenv("DATA_DIR", "/data")); AUDIO = DATA / "audio"; DB = DATA / "chime.db"
APP_VERSION = "0.7.2"
AUDIO.mkdir(parents=True, exist_ok=True)
protect_task = None
protect_status = {"connected": False, "message": "Noch nicht verbunden"}
last_ring_at = 0.0
audio_fetches = []

def db():
    c = sqlite3.connect(DB); c.row_factory = sqlite3.Row; return c

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS devices(uuid TEXT PRIMARY KEY,name TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 1,volume REAL NOT NULL DEFAULT .65);
        CREATE TABLE IF NOT EXISTS quiet_rules(id INTEGER PRIMARY KEY AUTOINCREMENT,device_uuid TEXT NOT NULL,days TEXT NOT NULL,start TEXT NOT NULL,end TEXT NOT NULL,label TEXT NOT NULL DEFAULT 'Ruhezeit',enabled INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS log(id INTEGER PRIMARY KEY AUTOINCREMENT,at TEXT NOT NULL,event TEXT NOT NULL,detail TEXT NOT NULL);
        """)

def setting(key, default=""):
    with db() as c:
        r=c.execute("SELECT value FROM settings WHERE key=?",(key,)).fetchone(); return r[0] if r else default

def set_settings(values):
    with db() as c:
        c.executemany("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", values.items())

def parse_protect_address(value):
    value=str(value or "").strip().rstrip("/")
    parsed=urlparse(value if "://" in value else "https://"+value)
    if not parsed.hostname: raise ValueError("Ungültige Protect-Adresse")
    return parsed.hostname, parsed.port or 443

def local_ipv4():
    sock=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8",53)); return sock.getsockname()[0]
    finally: sock.close()

def log(event, detail):
        with db() as c: c.execute("INSERT INTO log(at,event,detail) VALUES(?,?,?)",(datetime.now().isoformat(timespec="seconds"),event,detail))

def is_ring_message(msg):
    event_type=str(msg.changed_data.get("type","")).lower()
    if msg.new_obj is not None: event_type=str(getattr(msg.new_obj,"type",event_type)).lower()
    return msg.action.value == "add" and "ring" in event_type

def is_quiet(uuid, now=None):
    now=now or datetime.now(); day=now.weekday(); current=now.strftime("%H:%M")
    with db() as c: rules=c.execute("SELECT * FROM quiet_rules WHERE enabled=1 AND device_uuid=?",(uuid,)).fetchall()
    for r in rules:
        days=json.loads(r["days"]); start,end=r["start"],r["end"]
        if start <= end and day in days and start <= current < end: return True
        if start > end and ((day in days and current >= start) or ((day-1)%7 in days and current < end)): return True
    return False

def cast_one(uuid, volume):
    casts, browser = pychromecast.get_listed_chromecasts(uuids=[UUID(uuid)], discovery_timeout=6)
    try:
        if not casts: raise RuntimeError("Gerät nicht gefunden")
        cc=casts[0]; cc.wait(timeout=8); cc.set_volume(volume)
        base=setting("base_url", os.getenv("APP_BASE_URL", "")).rstrip("/")
        if not base: raise RuntimeError("Serveradresse fehlt – bitte im GUI speichern")
        audio_file=AUDIO/"chime.mp3"
        if not audio_file.exists(): raise RuntimeError("Kein Klingelton gespeichert")
        media_url=base+f"/chime.mp3?v={audio_file.stat().st_mtime_ns}"; mc=cc.media_controller
        mc.play_media(media_url,"audio/mpeg",title="Haustürklingel",autoplay=True,stream_type="BUFFERED")
        deadline=time.monotonic()+10; confirmed=False
        while time.monotonic()<deadline:
            mc.update_status(); time.sleep(.25)
            status=mc.status
            if status.content_id==media_url and status.player_is_playing: confirmed=True; break
            if status.idle_reason=="ERROR": raise RuntimeError("Google Home meldet einen Fehler beim Laden der MP3")
        if not confirmed: raise RuntimeError("Google Home hat die Wiedergabe innerhalb von 10 Sekunden nicht bestätigt")
        try: duration=float(setting("audio_duration","5"))
        except ValueError: duration=5
        end_at=time.monotonic()+min(max(duration+1,2),30)
        while time.monotonic()<end_at:
            mc.update_status(); time.sleep(.25)
            if mc.status.content_id==media_url and mc.status.player_is_idle: break
        try: mc.stop()
        except Exception: pass
        time.sleep(.2)
        try: cc.quit_app(timeout=3)
        except Exception: pass
    finally:
        if casts:
            try: casts[0].disconnect(timeout=2)
            except Exception: pass
        pychromecast.discovery.stop_discovery(browser)

async def ring(source="UniFi Protect"):
    fetch_start=len(audio_fetches)
    with db() as c: devices=c.execute("SELECT * FROM devices WHERE enabled=1").fetchall()
    played=[]; muted=[]; failed=[]; active=[]
    for d in devices:
        if is_quiet(d["uuid"]): muted.append(d["name"])
        else: active.append(d)
    results=await asyncio.gather(*(asyncio.to_thread(cast_one,d["uuid"],d["volume"]) for d in active),return_exceptions=True)
    for d,result in zip(active,results):
        if isinstance(result,Exception): failed.append(f'{d["name"]}: {result}')
        else: played.append(d["name"])
    log("Klingeln",json.dumps({"source":source,"played":played,"quiet":muted,"failed":failed},ensure_ascii=False))
    fetched=audio_fetches[fetch_start:]
    if played and not fetched: failed.append("Cast reagiert, aber kein Google Home hat /chime.mp3 vom Server abgerufen")
    return {"played":played,"quiet":muted,"failed":failed,"audio_fetches":fetched}

async def protect_loop():
    global protect_status, last_ring_at
    while True:
        try:
            import aiohttp
            host_value=setting("protect_host"); key=setting("protect_api_key")
            if not host_value or not key:
                protect_status={"connected":False,"message":"Protect-Zugangsdaten fehlen"}; await asyncio.sleep(10); continue
            host,port=parse_protect_address(host_value)
            ssl_check=setting("verify_ssl","false")=="true"
            base=f"https://{host}"+(f":{port}" if port!=443 else "")+"/proxy/protect/integration/v1"
            headers={"X-API-KEY":key}; timeout=aiohttp.ClientTimeout(total=15,connect=5)
            async with aiohttp.ClientSession(headers=headers,timeout=timeout) as session:
                async with session.get(base+"/meta/info",ssl=ssl_check) as response:
                    response.raise_for_status(); meta=await response.json(content_type=None)
                version=meta.get("applicationVersion","")
                protect_status={"connected":False,"message":f"Protect {version}: Ereignisverbindung wird aufgebaut …"}
                ws_url=base.replace("https://","wss://",1)+"/subscribe/events"
                async with session.ws_connect(ws_url,ssl=ssl_check,heartbeat=20,receive_timeout=90) as ws:
                    protect_status={"connected":True,"message":f"Mit UniFi Protect {version} verbunden"}
                    async for message in ws:
                        if message.type is aiohttp.WSMsgType.TEXT:
                            payload=json.loads(message.data); item=payload.get("item") or {}
                            now=time.monotonic()
                            if payload.get("type")=="add" and item.get("modelKey")=="event" and item.get("type")=="ring" and now-last_ring_at>2:
                                last_ring_at=now; asyncio.create_task(ring("UniFi Protect"))
                        elif message.type in (aiohttp.WSMsgType.CLOSED,aiohttp.WSMsgType.ERROR): break
        except asyncio.CancelledError: break
        except Exception as e:
            protect_status={"connected":False,"message":str(e)}; await asyncio.sleep(15)

@asynccontextmanager
async def lifespan(app):
    global protect_task
    init_db(); protect_task=asyncio.create_task(protect_loop()); yield
    protect_task.cancel()

app=FastAPI(title="Protect Chime",lifespan=lifespan)
app.mount("/static",StaticFiles(directory="static"),name="static")

@app.middleware("http")
async def disable_frontend_cache(request, call_next):
    response=await call_next(request)
    if request.url.path=="/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"]="no-store, no-cache, must-revalidate, max-age=0"
    response.headers["X-Protect-Chime-Version"]=APP_VERSION
    return response

@app.get("/")
def index(): return FileResponse("static/index.html")
@app.get("/chime.mp3")
def chime(request:Request):
    p=AUDIO/"chime.mp3"
    if not p.exists(): raise HTTPException(404,"Noch kein Klingelton hochgeladen")
    audio_fetches.append({"at":datetime.now().isoformat(timespec="seconds"),"client":request.client.host if request.client else "unbekannt","user_agent":request.headers.get("user-agent","")[:120]})
    del audio_fetches[:-20]
    return FileResponse(p,media_type="audio/mpeg",headers={"Cache-Control":"no-store, max-age=0"})
@app.get("/api/state")
def state():
    with db() as c:
        return {"version":APP_VERSION,"protect":protect_status,"audio_fetches":audio_fetches[-10:],"settings":{"protect_host":setting("protect_host"),"verify_ssl":setting("verify_ssl","false"),"has_key":bool(setting("protect_api_key")),"base_url":setting("base_url",os.getenv("APP_BASE_URL",""))},"devices":[dict(x) for x in c.execute("SELECT * FROM devices ORDER BY name")],"rules":[dict(x)|{"days":json.loads(x["days"])} for x in c.execute("SELECT * FROM quiet_rules ORDER BY start")],"logs":[dict(x) for x in c.execute("SELECT * FROM log ORDER BY id DESC LIMIT 20")]}
@app.post("/api/settings")
async def settings(body:dict):
    global protect_task
    protect_host=str(body.get("protect_host","")).strip()
    try: parse_protect_address(protect_host)
    except ValueError as e: raise HTTPException(400,str(e)) from e
    vals={"protect_host":protect_host,"base_url":str(body.get("base_url","")).strip().rstrip("/"),"verify_ssl":"true" if body.get("verify_ssl") else "false"}
    if body.get("protect_api_key"): vals["protect_api_key"]=str(body["protect_api_key"]).strip()
    set_settings(vals)
    if protect_task: protect_task.cancel()
    protect_task=asyncio.create_task(protect_loop())
    return {"ok":True,"message":"Gespeichert; Verbindung wird automatisch neu aufgebaut"}
@app.post("/api/discover-protect")
async def discover_protect(body:dict, request:Request):
    import aiohttp
    key=str(body.get("api_key") or setting("protect_api_key")).strip()
    if not key: raise HTTPException(400,"Bitte zuerst den Protect API-Schlüssel eingeben")
    seeds=[]
    if request.client: seeds.append(request.client.host)
    try: seeds.append(urlparse(str(body.get("base_url","") or setting("base_url"))).hostname)
    except Exception: pass
    try: seeds.append(local_ipv4())
    except OSError: pass
    lan_ip=None
    for seed in seeds:
        try:
            parsed_ip=ipaddress.ip_address(seed)
            if parsed_ip.version==4 and parsed_ip.is_private and not parsed_ip.is_loopback: lan_ip=str(parsed_ip); break
        except (ValueError,TypeError): pass
    if not lan_ip: raise HTTPException(500,"Lokales IPv4-Netz nicht ermittelbar; bitte die Synology-IP als Serveradresse speichern")
    network=ipaddress.ip_network(lan_ip+"/24",strict=False)
    configured=setting("protect_host")
    preferred=[]
    if configured:
        try: preferred.append(parse_protect_address(configured)[0])
        except ValueError: pass
    preferred.extend([str(network.network_address+1),str(network.network_address+254)])
    candidates=list(dict.fromkeys(preferred+[str(ip) for ip in network.hosts()]))
    semaphore=asyncio.Semaphore(48); found=[]
    timeout=aiohttp.ClientTimeout(total=1.8,connect=.7)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async def probe(host):
            async with semaphore:
                try:
                    url=f"https://{host}/proxy/protect/integration/v1/meta/info"
                    async with session.get(url,headers={"X-API-KEY":key},ssl=False) as response:
                        if response.status==200:
                            data=await response.json(content_type=None)
                            found.append({"host":host,"version":data.get("applicationVersion","")})
                except Exception: pass
        await asyncio.gather(*(probe(host) for host in candidates))
    if not found: raise HTTPException(404,"Kein Protect-System mit diesem API-Schlüssel im lokalen Netzwerk gefunden")
    return {"devices":found}
@app.post("/api/audio")
async def audio(file:UploadFile=File(...)):
    content=await file.read()
    if not content: raise HTTPException(400,"Die Audiodatei ist leer")
    if len(content)>10_000_000: raise HTTPException(400,"Maximal 10 MB")
    suffix=Path(file.filename or "audio").suffix[:10] or ".audio"
    with tempfile.NamedTemporaryFile(dir=DATA,suffix=suffix,delete=False) as temp:
        temp.write(content); source=Path(temp.name)
    target=AUDIO/"chime.mp3"; converted=AUDIO/"chime.new.mp3"
    try:
        result=await asyncio.to_thread(subprocess.run,["ffmpeg","-hide_banner","-loglevel","error","-y","-i",str(source),"-af","adelay=250:all=1,apad=pad_dur=0.5,loudnorm=I=-16:LRA=11:TP=-1.5","-ar","44100","-ac","2","-codec:a","libmp3lame","-b:a","192k",str(converted)],capture_output=True,text=True,timeout=30)
        if result.returncode!=0 or not converted.exists(): raise HTTPException(400,"Audiodatei konnte nicht verarbeitet werden: "+result.stderr[-300:])
        converted.replace(target)
        probe=await asyncio.to_thread(subprocess.run,["ffprobe","-v","error","-show_entries","format=duration","-of","default=noprint_wrappers=1:nokey=1",str(target)],capture_output=True,text=True,timeout=10)
        try: duration=max(.5,min(float(probe.stdout.strip()),30.0))
        except ValueError: duration=5.0
        set_settings({"audio_duration":str(duration)})
    except subprocess.TimeoutExpired as e: raise HTTPException(400,"Audioverarbeitung dauerte zu lange") from e
    finally:
        source.unlink(missing_ok=True); converted.unlink(missing_ok=True)
    return {"ok":True,"message":f"Klingelton optimiert ({duration:.1f} Sekunden)"}
@app.post("/api/discover")
async def discover():
    def scan():
        casts,browser=pychromecast.get_chromecasts(timeout=8)
        try: return [{"uuid":str(x.uuid),"name":x.name} for x in casts]
        finally: pychromecast.discovery.stop_discovery(browser)
    found=await asyncio.to_thread(scan)
    with db() as c:
        for d in found: c.execute("INSERT INTO devices(uuid,name) VALUES(?,?) ON CONFLICT(uuid) DO UPDATE SET name=excluded.name",(d["uuid"],d["name"]))
    return {"devices":found}
@app.put("/api/devices/{uuid}")
async def device(uuid:str,body:dict):
    with db() as c: c.execute("UPDATE devices SET enabled=?,volume=? WHERE uuid=?",(1 if body.get("enabled") else 0,float(body.get("volume",.65)),uuid))
    return {"ok":True}
@app.post("/api/rules")
async def add_rule(body:dict):
    with db() as c: c.execute("INSERT INTO quiet_rules(device_uuid,days,start,end,label) VALUES(?,?,?,?,?)",(body["device_uuid"],json.dumps(body["days"]),body["start"],body["end"],body.get("label","Ruhezeit")))
    return {"ok":True}
@app.delete("/api/rules/{id}")
def del_rule(id:int):
    with db() as c: c.execute("DELETE FROM quiet_rules WHERE id=?",(id,))
    return {"ok":True}
@app.post("/api/test")
async def test(): return await ring("Test aus GUI")
