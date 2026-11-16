"""MCP Video-Auswertung für Home Assistant v0.1.1

Wertet ein Video per Link aus und liefert Beschreibung, Transkript mit
Zeitstempeln und Bilder bei Szenenwechsel. Die Auswertung läuft als Job im
Hintergrund (dauert Minuten), das Ergebnis bleibt RetentionDays Tage erhalten.

MCP-Tools:
- StartVideoAnalysis(Url)   legt einen Job an und gibt die JobId zurück
- GetVideoResult(JobId)     Status bzw. fertiges Ergebnis
- ListVideoJobs(ActiveOnly) aktive (oder alle gespeicherten) Jobs

Ablauf je Job: yt-dlp (Download + Beschreibung) -> ffmpeg (Bilder, Ton) ->
Sprechpausen-Teilung -> HA-Cloud-Spracherkennung über den Supervisor-Proxy.
"""

import array
import asyncio
import contextlib
import glob
import json
import os
import re
import shutil
import sys
import time
import uuid
import wave
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

try:
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)  # audioop ist ab 3.11 veraltet
        import audioop

    def Rms(Chunk):
        return audioop.rms(Chunk, 2)
except ImportError:  # Python >= 3.13
    import math

    def Rms(Chunk):
        Samples = array.array("h")
        Samples.frombytes(Chunk)
        return math.sqrt(sum(X * X for X in Samples) / max(len(Samples), 1))


# ── Einstellungen ───────────────────────────────────────────

DataDir = Path("/data/jobs")
SttPipelineName = os.environ.get("SttPipelineName", "Home Assistant Cloud")
RetentionDays = int(os.environ.get("RetentionDays", "10"))
SceneThreshold = float(os.environ.get("SceneThreshold", "0.15"))
MaxDurationMinutes = int(os.environ.get("MaxDurationMinutes", "30"))

FrameMinGapSeconds = 3      # Mindestabstand zwischen zwei Szenenwechsel-Bildern
FrameFallbackSeconds = 20   # spätestens alle X Sekunden ein Bild
FrameWidth = 640
SegmentMaxSeconds = 12      # längste Sprachstücke für die Erkennung
SupervisorWs = "ws://supervisor/core/websocket"

JobQueue = None  # asyncio.Queue, wird im Lifespan angelegt


# ── Hilfsfunktionen ─────────────────────────────────────────

def Iso(Timestamp):
    if not Timestamp:
        return None
    return datetime.fromtimestamp(Timestamp, timezone.utc).isoformat(timespec="seconds")


def Clock(Seconds):
    Seconds = int(Seconds)
    return f"{Seconds // 60:02d}:{Seconds % 60:02d}"


def JobDir(JobId):
    return DataDir / JobId


def LoadJob(JobId):
    Path_ = JobDir(JobId) / "job.json"
    if not Path_.is_file():
        return None
    try:
        return json.loads(Path_.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def SaveJob(Job):
    Dir = JobDir(Job["JobId"])
    Dir.mkdir(parents=True, exist_ok=True)
    Tmp = Dir / "job.json.tmp"
    Tmp.write_text(json.dumps(Job, ensure_ascii=False, indent=1), encoding="utf-8")
    Tmp.replace(Dir / "job.json")


def AllJobs():
    Jobs = []
    if DataDir.is_dir():
        for Entry in DataDir.iterdir():
            Job = LoadJob(Entry.name)
            if Job:
                Jobs.append(Job)
    Jobs.sort(key=lambda J: J.get("CreatedAt", 0), reverse=True)
    return Jobs


def Update(Job, **Fields):
    Job.update(Fields)
    SaveJob(Job)


async def RunProcess(Cmd, Timeout=1800):
    Proc = await asyncio.create_subprocess_exec(
        *Cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        Out, Err = await asyncio.wait_for(Proc.communicate(), Timeout)
    except asyncio.TimeoutError:
        Proc.kill()
        raise RuntimeError(f"Zeitüberschreitung bei {Cmd[0]}")
    return Proc.returncode, Out.decode(errors="replace"), Err.decode(errors="replace")


# ── Pipeline-Schritte ───────────────────────────────────────

async def Download(Job, Dir):
    """Lädt das Video (max. 480p) samt Infodatei mit Beschreibung."""
    Cmd = [
        sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
        "-f", "bv*[height<=480]+ba/b", "--merge-output-format", "mp4",
        "--match-filter", f"duration<=?{MaxDurationMinutes * 60}",
        "--write-info-json", "-o", str(Dir / "video.%(ext)s"),
        "--", Job["Url"],
    ]
    Code, _, Err = await RunProcess(Cmd, Timeout=1800)
    Videos = [P for P in Dir.glob("video.*") if P.suffix != ".json"]
    if Code != 0 or not Videos:
        Reason = Err.strip().splitlines()[-1] if Err.strip() else "kein Video geladen (zu lang oder gesperrt?)"
        raise RuntimeError(f"Download fehlgeschlagen: {Reason}")
    Info = {}
    InfoPath = Dir / "video.info.json"
    if InfoPath.is_file():
        Info = json.loads(InfoPath.read_text(encoding="utf-8"))
        InfoPath.unlink()
    Update(
        Job,
        Title=Info.get("title"),
        Uploader=Info.get("uploader"),
        Duration=Info.get("duration"),
        Description=(Info.get("description") or "")[:20000],
    )
    return Videos[0]


async def ExtractFrames(Video, Dir):
    """Bilder bei Szenenwechsel, Dateiname = Zeitstempel in Sekunden."""
    FramesDir = Dir / "frames"
    FramesDir.mkdir(exist_ok=True)
    Filter = (
        "select='isnan(prev_selected_t)+gte(t-prev_selected_t,%d)"
        "+gt(scene,%s)*gte(t-prev_selected_t,%d)',scale=%d:-2,showinfo"
        % (FrameFallbackSeconds, SceneThreshold, FrameMinGapSeconds, FrameWidth)
    )
    Code, _, Err = await RunProcess([
        "ffmpeg", "-v", "info", "-y", "-i", str(Video), "-vf", Filter,
        "-fps_mode", "vfr", "-q:v", "4", str(FramesDir / "tmp_%05d.jpg"),
    ])
    if Code != 0:
        raise RuntimeError("Bilder konnten nicht extrahiert werden")
    Times = [float(T) for T in re.findall(r"Parsed_showinfo.*?pts_time:([0-9.]+)", Err)]
    Files = sorted(FramesDir.glob("tmp_*.jpg"))
    Frames = []
    for File, Time in zip(Files, Times):
        Name = f"t_{Time:08.2f}.jpg"
        File.rename(FramesDir / Name)
        Frames.append({"Time": round(Time, 2), "File": f"frames/{Name}"})
    return Frames


async def ExtractAudio(Video, Dir):
    Wav = Dir / "audio.wav"
    Code, _, _ = await RunProcess([
        "ffmpeg", "-v", "error", "-y", "-i", str(Video),
        "-vn", "-ac", "1", "-ar", "16000", str(Wav),
    ])
    if Code != 0 or not Wav.is_file():
        raise RuntimeError("Video enthält keine Tonspur")
    return Wav


def SplitSpeech(WavPath):
    """Teilt den Ton an Sprechpausen (die Cloud-Erkennung liefert sonst nur
    den ersten Satz bis zur ersten Pause). Rückgabe: [(Startsekunde, PCM)]."""
    with wave.open(str(WavPath), "rb") as Wav:
        Data = Wav.readframes(Wav.getnframes())
    FrameBytes = int(16000 * 0.03) * 2
    Count = len(Data) // FrameBytes
    Levels = [Rms(Data[I * FrameBytes:(I + 1) * FrameBytes]) for I in range(Count)]
    if not Levels:
        return []
    Ordered = sorted(Levels)
    Threshold = max(150, Ordered[int(len(Ordered) * 0.25)] * 1.5)
    MinPause = int(0.35 / 0.03)
    MaxLen = int(SegmentMaxSeconds / 0.03)
    MinLen = int(1.0 / 0.03)
    Cuts = [0]
    Run = 0
    for I, Level in enumerate(Levels):
        Run = Run + 1 if Level < Threshold else 0
        if Run == MinPause and I - Cuts[-1] > MinLen:
            Cuts.append(I - MinPause // 2)
        elif I - Cuts[-1] >= MaxLen and Run > 0:
            Cuts.append(I)
    Cuts.append(Count)
    Segments = []
    for A, B in zip(Cuts, Cuts[1:]):
        if B - A < 8:
            continue
        Segments.append((A * 0.03, Data[A * FrameBytes:B * FrameBytes]))
    return Segments


async def Transcribe(Job, Segments):
    """Spracherkennung über die HA-Pipeline (Supervisor-WebSocket)."""
    Token = os.environ.get("SUPERVISOR_TOKEN")
    if not Token:
        raise RuntimeError("SUPERVISOR_TOKEN fehlt (homeassistant_api nicht aktiv?)")
    Result = []
    Errors = 0
    async with aiohttp.ClientSession() as Session:
        async with Session.ws_connect(SupervisorWs, max_msg_size=0) as Ws:
            await Ws.receive_json()
            await Ws.send_json({"type": "auth", "access_token": Token})
            if (await Ws.receive_json())["type"] != "auth_ok":
                raise RuntimeError("Anmeldung am HA-WebSocket fehlgeschlagen")
            await Ws.send_json({"id": 1, "type": "assist_pipeline/pipeline/list"})
            Pipelines = (await Ws.receive_json())["result"]["pipelines"]
            Pipeline = next((P for P in Pipelines if P["name"] == SttPipelineName), None)
            if Pipeline is None:
                Pipeline = next((P for P in Pipelines if P.get("stt_engine") == "stt.home_assistant_cloud"), None)
            if Pipeline is None:
                raise RuntimeError(f"Pipeline '{SttPipelineName}' nicht gefunden")
            MsgId = 10
            for Index, (Start, Pcm) in enumerate(Segments):
                MsgId += 1
                Text = await RecognizeSegment(Ws, MsgId, Pipeline["id"], Pcm)
                if Text is None:
                    Errors += 1
                    Text = ""
                if Text:
                    Result.append({
                        "Start": round(Start, 1),
                        "End": round(Start + len(Pcm) / 32000, 1),
                        "Text": Text,
                    })
                Update(Job, Progress=f"Sprache {Index + 1}/{len(Segments)}")
    if Segments and Errors > len(Segments) / 2:
        raise RuntimeError("Spracherkennung lieferte überwiegend Fehler")
    return Result


async def RecognizeSegment(Ws, MsgId, PipelineId, Pcm):
    """Ein Sprachstück erkennen. '' = keine Sprache, None = Fehler."""
    await Ws.send_json({
        "id": MsgId, "type": "assist_pipeline/run", "start_stage": "stt",
        "end_stage": "stt", "input": {"sample_rate": 16000}, "pipeline": PipelineId,
    })
    Handler = None
    Text = ""
    while True:
        Msg = await asyncio.wait_for(Ws.receive_json(), 60)
        if Msg.get("id") != MsgId:
            continue
        if Msg["type"] == "result" and not Msg.get("success"):
            return None
        if Msg["type"] != "event":
            continue
        Event = Msg["event"]
        if Event["type"] == "run-start":
            Handler = Event["data"]["runner_data"]["stt_binary_handler_id"]
        elif Event["type"] == "stt-start":
            for I in range(0, len(Pcm), 4096):
                await Ws.send_bytes(bytes([Handler]) + Pcm[I:I + 4096])
            await Ws.send_bytes(bytes([Handler]))
        elif Event["type"] == "stt-end":
            Text = Event["data"]["stt_output"]["text"].strip()
        elif Event["type"] == "error":
            return "" if Event["data"].get("code") == "stt-no-text-recognized" else None
        elif Event["type"] == "run-end":
            return Text


async def RunJob(JobId):
    Job = LoadJob(JobId)
    if not Job or Job["Status"] not in ("queued", "running"):
        return
    Dir = JobDir(JobId)
    Update(Job, Status="running", Stage="download", Progress="Download", StartedAt=time.time())
    try:
        Video = await Download(Job, Dir)
        Update(Job, Stage="frames", Progress="Bilder")
        Frames = await ExtractFrames(Video, Dir)
        Update(Job, Stage="audio", Progress="Ton")
        Wav = await ExtractAudio(Video, Dir)
        Segments = await asyncio.to_thread(SplitSpeech, Wav)
        Update(Job, Stage="speech", Progress=f"Sprache 0/{len(Segments)}")
        Transcript = await Transcribe(Job, Segments)
        Text = "\n".join(f"[{Clock(S['Start'])}] {S['Text']}" for S in Transcript)
        (Dir / "result.json").write_text(
            json.dumps({"Frames": Frames, "Transcript": Transcript, "TranscriptText": Text},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        Update(Job, Status="done", Stage="done", Progress="fertig", FinishedAt=time.time(),
               FrameCount=len(Frames), SegmentCount=len(Transcript))
    except Exception as Error:  # noqa: BLE001
        Update(Job, Status="failed", Stage="failed", Progress="Fehler",
               Error=str(Error) or type(Error).__name__, FinishedAt=time.time())
    finally:
        # Große Zwischendateien entfernen, nur Ergebnis und Bilder bleiben
        for Pattern in ("video.*", "audio.wav"):
            for File in glob.glob(str(Dir / Pattern)):
                with contextlib.suppress(OSError):
                    os.remove(File)


async def Worker():
    while True:
        JobId = await JobQueue.get()
        try:
            await RunJob(JobId)
        except Exception as Error:  # noqa: BLE001
            print(f"[Worker] Fehler bei {JobId}: {Error}", flush=True)


async def CleanupLoop():
    """Löscht Jobs nach RetentionDays Tagen, damit der Speicher nicht vollläuft."""
    while True:
        Limit = time.time() - RetentionDays * 86400
        for Job in AllJobs():
            Reference = Job.get("FinishedAt") or Job.get("CreatedAt", 0)
            if Job["Status"] in ("done", "failed") and Reference < Limit:
                shutil.rmtree(JobDir(Job["JobId"]), ignore_errors=True)
                print(f"[Cleanup] Job {Job['JobId']} gelöscht", flush=True)
        await asyncio.sleep(3600)


# ── MCP-Tools ───────────────────────────────────────────────

mcp = MCPServer(
    version="0.1.1",
    name="MCP Video-Auswertung",
    instructions=(
        "Analyses a video from a link (YouTube etc.). Evaluation takes minutes.\n"
        "1. StartVideoAnalysis(Url) -> JobId (same URL within the retention period reuses the job).\n"
        "2. ListVideoJobs() shows active jobs; GetVideoResult(JobId) shows progress or the result.\n"
        "Result: Description (the written text of the uploader, often contains amounts/steps),\n"
        "TranscriptText (spoken text with [mm:ss] timestamps, speech recognition errors are possible,\n"
        "especially technical terms) and Frames (timestamps of extracted images).\n"
        f"Results are kept for {RetentionDays} days."
    ),
)


# ── MCP SDK 2.x: Fehlermeldungen von Tools wie in 1.x an das Modell durchreichen ──
# In 2.x sieht das Modell bei normalen Exceptions nur "Error executing tool <name>".
# Dieser Wrapper macht aus jeder Exception einen ToolError mit der Originalmeldung.
import functools as _functools
from mcp.shared.exceptions import MCPError as _MCPError

_ToolOriginal = mcp.tool


def _VisibleErrors(Function):
    @_functools.wraps(Function)
    async def _Wrapper(*Args, **Kwargs):
        try:
            return await Function(*Args, **Kwargs)
        except (ToolError, _MCPError):
            raise
        except Exception as Error:  # noqa: BLE001
            raise ToolError(str(Error) or type(Error).__name__) from Error
    return _Wrapper


def _ToolDecorator(*Args, **Kwargs):
    Decorator = _ToolOriginal(*Args, **Kwargs)
    return lambda Function: Decorator(_VisibleErrors(Function))


mcp.tool = _ToolDecorator


@mcp.tool()
async def StartVideoAnalysis(Url: str) -> dict:
    """Start the analysis of a video link as a background job and return its JobId.
    Poll GetVideoResult(JobId) afterwards. Only http(s) links are accepted."""
    Url = Url.strip()
    if not re.match(r"^https?://", Url):
        raise ToolError("Nur http(s)-Links sind erlaubt")
    for Existing in AllJobs():
        if Existing["Url"] == Url and Existing["Status"] in ("queued", "running", "done"):
            return {"JobId": Existing["JobId"], "Status": Existing["Status"], "Reused": True}
    Job = {"JobId": uuid.uuid4().hex[:12], "Url": Url, "Status": "queued",
           "Stage": "queued", "Progress": "wartet", "CreatedAt": time.time()}
    SaveJob(Job)
    await JobQueue.put(Job["JobId"])
    return {"JobId": Job["JobId"], "Status": "queued", "Reused": False,
            "QueuePosition": JobQueue.qsize()}


@mcp.tool()
async def GetVideoResult(JobId: str, IncludeDescription: bool = True) -> dict:
    """Return status/progress of a job, or the full result when it is done
    (Title, Description, TranscriptText with [mm:ss], Transcript segments, Frames)."""
    Job = LoadJob(re.sub(r"[^0-9a-f]", "", JobId))
    if not Job:
        raise ToolError("Job unbekannt oder abgelaufen")
    Info = {"JobId": Job["JobId"], "Url": Job["Url"], "Status": Job["Status"],
            "Stage": Job.get("Stage"), "Progress": Job.get("Progress")}
    if Job["Status"] == "failed":
        Info["Error"] = Job.get("Error")
        return Info
    if Job["Status"] != "done":
        return Info
    Result = json.loads((JobDir(Job["JobId"]) / "result.json").read_text(encoding="utf-8"))
    Info.update({
        "Title": Job.get("Title"), "Uploader": Job.get("Uploader"),
        "DurationSeconds": Job.get("Duration"),
        "ExpiresAt": Iso((Job.get("FinishedAt") or 0) + RetentionDays * 86400),
        "TranscriptText": Result["TranscriptText"],
        "Transcript": Result["Transcript"],
        "Frames": Result["Frames"],
    })
    if IncludeDescription:
        Info["Description"] = Job.get("Description", "")
    return Info


@mcp.tool()
async def ListVideoJobs(ActiveOnly: bool = True) -> dict:
    """List video jobs. ActiveOnly=True shows queued/running jobs only,
    False also shows finished/failed jobs that are still stored."""
    Items = []
    for Job in AllJobs():
        Active = Job["Status"] in ("queued", "running")
        if ActiveOnly and not Active:
            continue
        Items.append({
            "JobId": Job["JobId"], "Url": Job["Url"], "Title": Job.get("Title"),
            "Status": Job["Status"], "Progress": Job.get("Progress"),
            "CreatedAt": Iso(Job.get("CreatedAt")), "FinishedAt": Iso(Job.get("FinishedAt")),
        })
    return {"Count": len(Items), "Jobs": Items}


# ── App ─────────────────────────────────────────────────────

async def Health(Request):
    return JSONResponse({"status": "ok", "queued": JobQueue.qsize() if JobQueue else 0})


McpApp = mcp.streamable_http_app(
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="0.0.0.0",
)


@contextlib.asynccontextmanager
async def Lifespan(App):
    global JobQueue
    JobQueue = asyncio.Queue()
    DataDir.mkdir(parents=True, exist_ok=True)
    # Unterbrochene Jobs (Neustart) von vorn starten
    for Job in sorted(AllJobs(), key=lambda J: J.get("CreatedAt", 0)):
        if Job["Status"] in ("queued", "running"):
            Update(Job, Status="queued", Stage="queued", Progress="wartet")
            await JobQueue.put(Job["JobId"])
    async with McpApp.router.lifespan_context(App):
        Tasks = [asyncio.create_task(Worker()), asyncio.create_task(CleanupLoop())]
        try:
            yield
        finally:
            for Task in Tasks:
                Task.cancel()


app = Starlette(routes=[Route("/health", Health)] + McpApp.routes, lifespan=Lifespan)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8771)
