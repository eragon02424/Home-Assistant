"""MCP Video-Auswertung für Home Assistant v0.3.0

Wertet ein Video per Link aus und liefert Beschreibung, Transkript mit
Zeitstempeln und Bilder im festen Raster von 500 Millisekunden. Die Auswertung läuft als Job im
Hintergrund (dauert Minuten), das Ergebnis bleibt RetentionDays Tage erhalten.

MCP-Tools:
- StartVideoAnalysis(Url, ForceNew)  legt einen Job an und gibt die JobId zurück
- GetVideoResult(JobId)     Status bzw. fertiges Ergebnis
- ListVideoJobs(ActiveOnly) aktive (oder alle gespeicherten) Jobs
- GetVideoFrames(JobId, ...) Standbilder zum Ansehen (inkl. Abruf-URL für Grocy)

HTTP-Abrufe (Heimnetz, ohne Anmeldung):
- /jobs/<JobId>/frames/<Name>.jpg   einzelnes Bild
- /jobs/<JobId>/frames.zip          alle Bilder als ZIP (wird beim Abruf erzeugt)
- /jobs/<JobId>/transcript.txt      Transkript als Textdatei mit [mm:ss]

Ablauf je Job: Speicherprüfung -> yt-dlp (Download + Beschreibung) -> ffmpeg (alle
500 ms ein Bild) -> YouTube-Untertitel, sonst Ton -> Sprechpausen-Teilung ->
HA-Cloud-Spracherkennung über den Supervisor-Proxy.
"""

import array
import asyncio
import contextlib
import glob
import html
import json
import os
import re
import shutil
import sys
import tempfile
import time
import uuid
import wave
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import aiohttp
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.mcpserver.utilities.types import Image as McpImage
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, Response
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
MaxDurationMinutes = int(os.environ.get("MaxDurationMinutes", "30"))
MinFreeGb = float(os.environ.get("MinFreeGb", "20"))
# Adresse, unter der andere Add-ons (z. B. Grocy-Bild-Worker) die Bilder abrufen
FrameBaseUrl = os.environ.get("FrameBaseUrl", "http://192.168.178.76:8771").rstrip("/")

FrameIntervalSeconds = 0.5  # festes Raster: ein Bild alle 500 Millisekunden
FrameWidth = 640
FrameBytesEstimate = 60 * 1024  # vorsichtige Schätzung je Bild (gemessen 20 bis 50 KB)
SegmentMaxSeconds = 12      # längste Sprachstücke für die Erkennung
SupervisorWs = "ws://supervisor/core/websocket"
SupervisorApi = "http://supervisor/core/api"
NotifyId = "mcp_video_analysis_speicher"

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


def FrameUrl(JobId, Frame):
    """Abrufadresse eines Standbilds (für Grocy-Bild-Jobs: endet auf .jpg)."""
    return f"{FrameBaseUrl}/jobs/{JobId}/{Frame['File']}"


def HasSpeech(Words, Duration):
    """Sprache gilt nur als erkannt, wenn genug Wörter pro Minute zusammenkommen."""
    return Words >= max(5, (Duration or 0) / 60 * 15)


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


# ── Speicherschutz ──────────────────────────────────────────

def FreeGb():
    return shutil.disk_usage(DataDir).free / 1024 ** 3


def DirSizeBytes(Path_):
    Total = 0
    for Root, _, Files in os.walk(Path_):
        for Name in Files:
            with contextlib.suppress(OSError):
                Total += os.path.getsize(os.path.join(Root, Name))
    return Total


async def NotifyHa(Title, Message):
    """Meldung in Home Assistant (Benachrichtigung in der Seitenleiste)."""
    Token = os.environ.get("SUPERVISOR_TOKEN")
    if not Token:
        return
    try:
        async with aiohttp.ClientSession() as Session:
            await Session.post(
                f"{SupervisorApi}/services/persistent_notification/create",
                headers={"Authorization": f"Bearer {Token}"},
                json={"title": Title, "message": Message, "notification_id": NotifyId},
                timeout=aiohttp.ClientTimeout(total=10),
            )
    except Exception as Error:  # noqa: BLE001
        print(f"[Notify] fehlgeschlagen: {Error}", flush=True)


def DeleteJob(Job):
    shutil.rmtree(JobDir(Job["JobId"]), ignore_errors=True)


def FinishedJobsOldestFirst(ExceptJobId=None):
    Jobs = [J for J in AllJobs()
            if J["Status"] in ("done", "failed") and J["JobId"] != ExceptJobId]
    Jobs.sort(key=lambda J: J.get("FinishedAt") or J.get("CreatedAt", 0))
    return Jobs


def FreeUpSpace(NeededGb, ExceptJobId=None):
    """Löscht fertige Jobs von alt nach neu, bis NeededGb frei sind.
    Rückgabe: True, wenn genug frei ist."""
    for Job in FinishedJobsOldestFirst(ExceptJobId):
        if FreeGb() >= NeededGb:
            break
        DeleteJob(Job)
        print(f"[Speicher] Job {Job['JobId']} gelöscht (Platz)", flush=True)
    return FreeGb() >= NeededGb


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
    """Ein Bild alle FrameIntervalSeconds, Dateiname = Zeitstempel in Sekunden."""
    FramesDir = Dir / "frames"
    FramesDir.mkdir(exist_ok=True)
    Filter = "fps=%s,scale=%d:-2,showinfo" % (1 / FrameIntervalSeconds, FrameWidth)
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


def ParseSrt(Text):
    """Liest eine SRT-Datei: [{Start, End, Text}] mit Sekunden, ohne Formatierungs-Tags
    und ohne die doppelten Zeilen, die automatische Untertitel gern enthalten."""
    Segments = []
    for Block in re.split(r"\n\s*\n", Text.replace("\r\n", "\n").strip()):
        Lines = Block.split("\n")
        TimeLine = next((L for L in Lines if "-->" in L), None)
        if TimeLine is None:
            continue
        Match = re.match(r"\s*(\d+):(\d+):(\d+)[,.](\d+)\s*-->\s*(\d+):(\d+):(\d+)[,.](\d+)", TimeLine)
        if not Match:
            continue
        Parts = Match.groups()
        Start = int(Parts[0]) * 3600 + int(Parts[1]) * 60 + int(Parts[2]) + int(Parts[3]) / 10 ** len(Parts[3])
        End = int(Parts[4]) * 3600 + int(Parts[5]) * 60 + int(Parts[6]) + int(Parts[7]) / 10 ** len(Parts[7])
        Body = " ".join(L.strip() for L in Lines[Lines.index(TimeLine) + 1:] if L.strip())
        Body = html.unescape(re.sub(r"<[^>]+>", "", Body)).strip()
        Previous = Segments[-1]["Text"] if Segments else ""
        if not Body or Body == Previous or Previous.endswith(Body):
            continue
        Segments.append({"Start": round(Start, 1), "End": round(End, 1), "Text": Body})
    return Segments


def MergeSegments(Segments, MaxSeconds=10):
    """Fasst kurze Untertitelzeilen zu Blöcken von höchstens MaxSeconds zusammen."""
    Merged = []
    for Segment in Segments:
        if Merged and Segment["End"] - Merged[-1]["Start"] <= MaxSeconds:
            Merged[-1]["Text"] += " " + Segment["Text"]
            Merged[-1]["End"] = Segment["End"]
        else:
            Merged.append(dict(Segment))
    return Merged


async def DownloadSubtitles(Job, Dir):
    """Holt die Untertitelspur (manuell oder automatisch) per yt-dlp.
    Rückgabe: (Segmente, Sprachkürzel) oder ([], None). Fehler sind unkritisch,
    ohne Untertitel läuft die Spracherkennung als Rückfall."""
    Cmd = [
        sys.executable, "-m", "yt_dlp", "-q", "--no-warnings", "--no-playlist",
        "--skip-download", "--write-subs", "--write-auto-subs",
        "--sub-langs", "de.*,en.*", "--sub-format", "srt/vtt/best", "--convert-subs", "srt",
        "-o", str(Dir / "subs.%(ext)s"), "--", Job["Url"],
    ]
    try:
        await RunProcess(Cmd, Timeout=300)
    except RuntimeError:
        return [], None
    Files = {P.name.split(".")[-2]: P for P in Dir.glob("subs.*.srt")}
    # Die Originalsprache (…-orig) zuerst, damit keine maschinelle Übersetzung gewählt wird
    Order = ["de-orig", "en-orig", "de", "en"] + sorted(Files)
    Lang = next((L for L in Order if L in Files), None)
    if Lang is None:
        return [], None
    Segments = ParseSrt(Files[Lang].read_text(encoding="utf-8", errors="replace"))
    for File in Files.values():
        with contextlib.suppress(OSError):
            File.unlink()
    return MergeSegments(Segments), Lang


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


class StorageError(RuntimeError):
    """Zu wenig freier Speicher: Auftrag wird abgebrochen, seine Dateien werden gelöscht."""


def EstimateNeededGb(DurationSeconds):
    """Geschätzter Bedarf: Bilder im 500-ms-Raster plus Ton als WAV (16 kHz, mono, 16 Bit)."""
    Bytes = DurationSeconds / FrameIntervalSeconds * FrameBytesEstimate + DurationSeconds * 32000
    return Bytes / 1024 ** 3


async def EnsureSpace(Job, DurationSeconds=0):
    """Stellt sicher, dass nach dem Auftrag noch MinFreeGb frei bleiben. Reicht der Platz nicht,
    werden fertige Analysen von alt nach neu gelöscht. Hilft das nicht, bricht der Auftrag ab."""
    Needed = MinFreeGb + EstimateNeededGb(DurationSeconds)
    if FreeGb() >= Needed:
        return
    if FreeUpSpace(Needed, ExceptJobId=Job["JobId"]):
        return
    Message = (f"Zu wenig Speicher: {FreeGb():.1f} GB frei, nötig sind {Needed:.1f} GB "
               f"(Reserve {MinFreeGb:g} GB plus geschätzter Bedarf der Analyse). "
               "Alle älteren Analysen sind bereits gelöscht.")
    print(f"[Speicher] {Message} Job {Job['JobId']} wird abgebrochen.", flush=True)
    await NotifyHa(
        "Video-Auswertung: zu wenig Speicher",
        f"{Message}\n\nDer Auftrag {Job['JobId']} wurde abgebrochen und seine Dateien gelöscht.\n{Job['Url']}",
    )
    raise StorageError(Message)


async def RunJob(JobId):
    Job = LoadJob(JobId)
    if not Job or Job["Status"] not in ("queued", "running"):
        return
    Dir = JobDir(JobId)
    Update(Job, Status="running", Stage="download", Progress="Download", StartedAt=time.time())
    try:
        await EnsureSpace(Job)  # Reserve prüfen, bevor etwas geladen wird
        Video = await Download(Job, Dir)
        await EnsureSpace(Job, Job.get("Duration") or 0)  # jetzt ist die Länge bekannt
        Update(Job, Stage="frames", Progress="Bilder")
        Frames = await ExtractFrames(Video, Dir)
        await EnsureSpace(Job)  # nach dem Schneiden die Reserve erneut prüfen
        Update(Job, Stage="subtitles", Progress="Untertitel")
        Transcript, Lang = await DownloadSubtitles(Job, Dir)
        TranscriptSource = f"YouTube-Untertitel ({Lang})" if Transcript else "Spracherkennung"
        if not Transcript:
            Update(Job, Stage="audio", Progress="Ton")
            Wav = await ExtractAudio(Video, Dir)
            Segments = await asyncio.to_thread(SplitSpeech, Wav)
            Update(Job, Stage="speech", Progress=f"Sprache 0/{len(Segments)}")
            Transcript = await Transcribe(Job, Segments)
        Text = "\n".join(f"[{Clock(S['Start'])}] {S['Text']}" for S in Transcript)
        # Bei Musik liefert die Erkennung vereinzelte Fantasiewörter: Sprache gilt nur
        # als erkannt, wenn genug Wörter pro Minute zusammenkommen
        Words = sum(len(S["Text"].split()) for S in Transcript)
        SpeechDetected = HasSpeech(Words, Job.get("Duration"))
        (Dir / "result.json").write_text(
            json.dumps({"Frames": Frames, "Transcript": Transcript, "TranscriptText": Text},
                       ensure_ascii=False, indent=1), encoding="utf-8")
        Update(Job, Status="done", Stage="done", Progress="fertig", FinishedAt=time.time(),
               FrameCount=len(Frames), SegmentCount=len(Transcript), WordCount=Words,
               SpeechDetected=SpeechDetected, TranscriptSource=TranscriptSource,
               FrameIntervalSeconds=FrameIntervalSeconds,
               FramesBytes=DirSizeBytes(Dir / "frames"))
    except Exception as Error:  # noqa: BLE001
        Update(Job, Status="failed", Stage="failed", Progress="Fehler",
               Error=str(Error) or type(Error).__name__, FinishedAt=time.time())
        if isinstance(Error, StorageError):
            # Alles löschen, nur job.json mit der Fehlermeldung bleibt
            for Item in Dir.iterdir():
                if Item.name == "job.json":
                    continue
                if Item.is_dir():
                    shutil.rmtree(Item, ignore_errors=True)
                else:
                    with contextlib.suppress(OSError):
                        Item.unlink()
    finally:
        # Große Zwischendateien entfernen, nur Ergebnis und Bilder bleiben
        for Pattern in ("video.*", "audio.wav", "subs.*"):
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
    version="0.3.0",
    name="MCP Video-Auswertung",
    instructions=(
        "Analyses a video from a link (YouTube etc.). Evaluation takes minutes.\n"
        "1. StartVideoAnalysis(Url) -> JobId (same URL within the retention period reuses the job).\n"
        "2. ListVideoJobs() shows active jobs; GetVideoResult(JobId) shows progress or the result.\n"
        "Result: Description (the written text of the uploader, often contains amounts/steps),\n"
        "TranscriptText (spoken text with [mm:ss] timestamps, speech recognition errors are possible,\n"
        "SpeechDetected=false means mostly music: do not trust the transcript,\n"
        "especially technical terms; TranscriptSource says whether it comes from YouTube subtitles\n"
        "or speech recognition), FrameCount (one still every 0.5 s, fixed grid), TranscriptUrl\n"
        "(plain text download) and FramesZipUrl (all stills as one ZIP, for tools that can fetch HTTP links).\n"
        "StartVideoAnalysis(Url, ForceNew=true) discards a finished job of the same URL and analyses it again.\n"
        "GetVideoFrames(JobId, FromSeconds, ToSeconds, MaxCount) shows stills so you can look at them;\n"
        "each still has a Url usable as image_url for Grocy recipe pictures.\n"
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
async def StartVideoAnalysis(Url: str, ForceNew: bool = False) -> dict:
    """Start the analysis of a video link as a background job and return its JobId.
    Poll GetVideoResult(JobId) afterwards. Only http(s) links are accepted.
    ForceNew=True deletes a finished job of the same URL and analyses it again."""
    Url = Url.strip()
    if not re.match(r"^https?://", Url):
        raise ToolError("Nur http(s)-Links sind erlaubt")
    for Existing in AllJobs():
        if Existing["Url"] != Url:
            continue
        if Existing["Status"] in ("queued", "running"):
            return {"JobId": Existing["JobId"], "Status": Existing["Status"], "Reused": True}
        if Existing["Status"] == "done":
            if not ForceNew:
                return {"JobId": Existing["JobId"], "Status": Existing["Status"], "Reused": True}
            DeleteJob(Existing)
    Job = {"JobId": uuid.uuid4().hex[:12], "Url": Url, "Status": "queued",
           "Stage": "queued", "Progress": "wartet", "CreatedAt": time.time()}
    SaveJob(Job)
    await JobQueue.put(Job["JobId"])
    return {"JobId": Job["JobId"], "Status": "queued", "Reused": False,
            "QueuePosition": JobQueue.qsize()}


@mcp.tool()
async def GetVideoResult(JobId: str, IncludeDescription: bool = True, IncludeSegments: bool = False,
                         IncludeFrameList: bool = False) -> dict:
    """Return status/progress of a job, or the full result when it is done
    (Title, Description, TranscriptText with [mm:ss], TranscriptSource, TranscriptUrl,
    FrameCount, FramesZipUrl, SpeechDetected).
    IncludeSegments=True adds the transcript as segments with Start/End seconds.
    IncludeFrameList=True adds Time and Url of every single still (can be thousands).
    If SpeechDetected is false the video has hardly any speech (music): rely on
    the Description and the Frames instead of the transcript."""
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
        "SpeechDetected": Job["SpeechDetected"] if "SpeechDetected" in Job else HasSpeech(
            sum(len(S["Text"].split()) for S in Result["Transcript"]), Job.get("Duration")),
        "TranscriptText": Result["TranscriptText"],
        "TranscriptSource": Job.get("TranscriptSource", "Spracherkennung"),
        "TranscriptUrl": f"{FrameBaseUrl}/jobs/{Job['JobId']}/transcript.txt",
        "FrameCount": len(Result["Frames"]),
        "FrameIntervalSeconds": Job.get("FrameIntervalSeconds"),
        "FramesSizeMB": round(Job.get("FramesBytes", 0) / 1024 ** 2, 1),
        "FramesZipUrl": f"{FrameBaseUrl}/jobs/{Job['JobId']}/frames.zip",
    })
    if not Info["SpeechDetected"]:
        Info["Hint"] = "Kaum Sprache erkannt (vermutlich Musik): Text ist unzuverlässig, Beschreibung und Bilder nutzen"
    if IncludeSegments:
        Info["Transcript"] = Result["Transcript"]
    if IncludeFrameList:
        Info["Frames"] = [{"Time": F["Time"], "Url": FrameUrl(Job["JobId"], F)} for F in Result["Frames"]]
    if IncludeDescription:
        Info["Description"] = Job.get("Description", "")
    return Info


@mcp.tool()
async def GetVideoFrames(JobId: str, FromSeconds: float = 0, ToSeconds: float = 1000000, MaxCount: int = 6):
    """Show still images of a finished job (one every 0.5 s, fixed grid).
    Only images between FromSeconds and ToSeconds are returned, at most MaxCount (1-12,
    evenly thinned out). The first part is a text listing Time and Url of every image in the
    same order as the images that follow; the Url can be passed to the recipe tool
    queue_recipe_image_job(recipe_id, image_url=...) to use a still as recipe picture."""
    Job = LoadJob(re.sub(r"[^0-9a-f]", "", JobId))
    if not Job:
        raise ToolError("Job unbekannt oder abgelaufen")
    if Job["Status"] != "done":
        raise ToolError(f"Job ist noch nicht fertig (Status {Job['Status']})")
    Result = json.loads((JobDir(Job["JobId"]) / "result.json").read_text(encoding="utf-8"))
    Selected = [F for F in Result["Frames"] if FromSeconds <= F["Time"] <= ToSeconds]
    MaxCount = max(1, min(MaxCount, 12))
    if len(Selected) > MaxCount:
        Step = (len(Selected) - 1) / (MaxCount - 1) if MaxCount > 1 else 0
        Selected = [Selected[round(I * Step)] for I in range(MaxCount)] if MaxCount > 1 else [Selected[len(Selected) // 2]]
    if not Selected:
        raise ToolError("Keine Bilder in diesem Zeitbereich")
    Listing = [{"Time": F["Time"], "Url": FrameUrl(Job["JobId"], F)} for F in Selected]
    Parts = [json.dumps(Listing, ensure_ascii=False)]
    for Frame in Selected:
        Parts.append(McpImage(data=(JobDir(Job["JobId"]) / Frame["File"]).read_bytes(), format="jpeg"))
    return Parts


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


async def FrameFile(Request):
    """Liefert ein Standbild als JPEG (nur Jobs/Bilder mit streng geprüften Namen)."""
    JobId = Request.path_params["JobId"]
    Name = Request.path_params["Name"]
    if not re.fullmatch(r"[0-9a-f]{12}", JobId) or not re.fullmatch(r"t_\d{5,}\.\d{2}\.jpg", Name):
        return Response(status_code=404)
    File = JobDir(JobId) / "frames" / Name
    if not File.is_file():
        return Response(status_code=404)
    return FileResponse(File, media_type="image/jpeg")


async def TranscriptFile(Request):
    """Liefert das Transkript eines fertigen Jobs als Textdatei."""
    JobId = Request.path_params["JobId"]
    if not re.fullmatch(r"[0-9a-f]{12}", JobId):
        return Response(status_code=404)
    Job = LoadJob(JobId)
    if not Job or Job["Status"] != "done":
        return Response(status_code=404)
    Result = json.loads((JobDir(JobId) / "result.json").read_text(encoding="utf-8"))
    return PlainTextResponse(Result["TranscriptText"])


def BuildZip(JobId):
    """Packt alle Bilder eines Jobs in eine temporäre ZIP-Datei (JPEG, daher ohne Kompression)."""
    Tmp = tempfile.NamedTemporaryFile(prefix="frames_", suffix=".zip", delete=False)
    Tmp.close()
    with zipfile.ZipFile(Tmp.name, "w", zipfile.ZIP_STORED) as Zip:
        for File in sorted((JobDir(JobId) / "frames").glob("t_*.jpg")):
            Zip.write(File, File.name)
    return Tmp.name


async def FramesZip(Request):
    """Liefert alle Bilder eines fertigen Jobs als ein ZIP; die Datei wird nach dem Abruf gelöscht."""
    JobId = Request.path_params["JobId"]
    if not re.fullmatch(r"[0-9a-f]{12}", JobId):
        return Response(status_code=404)
    Job = LoadJob(JobId)
    if not Job or Job["Status"] != "done" or not (JobDir(JobId) / "frames").is_dir():
        return Response(status_code=404)
    Name = await asyncio.to_thread(BuildZip, JobId)
    return FileResponse(Name, media_type="application/zip", filename=f"frames_{JobId}.zip",
                        background=BackgroundTask(os.remove, Name))


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
    for Job in sorted(AllJobs(), key=lambda J: J.get("CreatedAt", 0), reverse=False):
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


app = Starlette(
    routes=[
        Route("/health", Health),
        Route("/jobs/{JobId}/frames/{Name}", FrameFile),
        Route("/jobs/{JobId}/frames.zip", FramesZip),
        Route("/jobs/{JobId}/transcript.txt", TranscriptFile),
    ] + McpApp.routes,
    lifespan=Lifespan,
)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=8771)
