#!/usr/bin/env python3
"""
concat_video.py - Ajoute une intro et/ou une outro à une vidéo.

Le script inspecte la vidéo principale, adapte uniquement les segments ajoutés
à son codec et à ses caractéristiques, puis conserve les pistes annexes et
leur synchronisation. Les traitements HDR, Dolby Vision et HDR10+ ne sont
activés que lorsque la source porte ces métadonnées.
"""

import os
import sys
import json
import shutil
import tempfile
import subprocess
import builtins
from xml.sax.saxutils import escape as xml_escape
from pathlib import Path

# Surcharge globale de print pour forcer le flush immédiat de la console
def print(*args, **kwargs):
    kwargs.setdefault('flush', True)
    builtins.print(*args, **kwargs)

# --- CONFIGURATION DES CHEMINS DES OUTILS EXTERNES (OPTIONNEL) ---
# Ordre de résolution : chemin configuré ci-dessous, PATH système, puis
# <racine du projet>/tools/<nom>. Sous Windows, le dernier emplacement est
# aussi testé avec l'extension .exe. Laissez à None pour cette détection
# automatique ; un chemin absolu force l'utilisation de l'exécutable choisi.
# Muxiveo reste opt-in : il n'est utilisé que lorsque MUXIVEO_PATH est défini.
FFMPEG_PATH = None
FFPROBE_PATH = None
DOVI_TOOL_PATH = None
HDR10PLUS_TOOL_PATH = None
MEDIAINFO_PATH = None
MKVMERGE_PATH = None
MUXIVEO_PATH = None

# --- CONFIGURATION DU DOSSIER DE TRAVAIL TEMPORAIRE (OPTIONNEL) ---
# Spécifiez ici le chemin absolu du dossier temporaire de travail.
# Si None, le script garde son comportement par défaut : %temp% sous Windows, dossier de sortie sous Linux.
DEFAULT_WORKDIR = None

# Résolution des outils système et locaux
def find_tool(name, override_path=None):
    if override_path:
        return str(override_path)
    # Cherche dans le PATH système
    path_tool = shutil.which(name)
    if path_tool:
        return path_tool
    # Cherche dans un dossier local "tools" à la racine du script
    root_dir = Path(__file__).parent.parent
    local_tool = root_dir / "tools" / name
    if local_tool.exists():
        return str(local_tool)
    if sys.platform == "win32":
        local_tool_exe = root_dir / "tools" / f"{name}.exe"
        if local_tool_exe.exists():
            return str(local_tool_exe)
    return name

def is_tool_available(tool_path):
    if not tool_path:
        return False
    if os.path.exists(tool_path):
        return True
    return shutil.which(tool_path) is not None

FFMPEG = find_tool("ffmpeg", FFMPEG_PATH)
FFPROBE = find_tool("ffprobe", FFPROBE_PATH)
DOVI_TOOL = find_tool("dovi_tool", DOVI_TOOL_PATH)
HDR10PLUS_TOOL = find_tool("hdr10plus_tool", HDR10PLUS_TOOL_PATH)
MEDIAINFO = find_tool("mediainfo", MEDIAINFO_PATH)
MKVMERGE = find_tool("mkvmerge", MKVMERGE_PATH)
MUXIVEO = find_tool("muxiveo", MUXIVEO_PATH)

def parse_fraction(val):
    if not val:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if "/" in val:
        try:
            num, den = val.split("/")
            return float(num) / float(den)
        except ValueError:
            return 0.0
    try:
        return float(val)
    except ValueError:
        return 0.0

def get_audio_details_mediainfo(file_path):
    """Retourne debit et indicateur Atmos issus des metadonnees de bitstream."""
    details = []
    try:
        result = subprocess.run(
            [MEDIAINFO, "--output=JSON", file_path],
            capture_output=True,
            text=True,
            check=True
        )
        for track in json.loads(result.stdout).get("media", {}).get("track", []):
            if track.get("@type") != "Audio":
                continue
            atmosphere_fields = (
                track.get("Format_Commercial_IfAny", ""),
                track.get("Format_AdditionalFeatures", ""),
                track.get("Format_Profile", ""),
                track.get("Format", "")
            )
            marker_text = " ".join(str(value) for value in atmosphere_fields).lower()
            try:
                bitrate = int(track["BitRate"]) if track.get("BitRate") else None
            except (TypeError, ValueError):
                bitrate = None
            details.append({
                "bitrate": bitrate,
                "is_atmos": (
                    "dolby atmos" in marker_text
                    or "joint object coding" in marker_text
                    or "joc" in marker_text
                )
            })
    except Exception:
        pass
    return details

def get_all_audio_tracks_mkvmerge(mkvmerge_path, file_path):
    try:
        cmd = [mkvmerge_path, "-J", file_path]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        
        audio_details = get_audio_details_mediainfo(file_path)
        
        audio_tracks = []
        video_track_id = None
        subtitle_track_ids = []
        
        audio_idx = 0
        for track in data.get("tracks", []):
            t_id = track.get("id")
            t_type = track.get("type")
            props = track.get("properties", {})
            if t_type == "video" and video_track_id is None:
                video_track_id = t_id
            elif t_type == "audio":
                raw_codec = track.get("codec", "").lower()
                codec = "aac"
                if "e-ac-3" in raw_codec or "eac3" in raw_codec:
                    codec = "eac3"
                elif "ac-3" in raw_codec or "ac3" in raw_codec:
                    codec = "ac3"
                elif "truehd" in raw_codec:
                    codec = "truehd"
                elif "flac" in raw_codec:
                    codec = "flac"
                elif "dts" in raw_codec:
                    codec = "dts"
                elif "opus" in raw_codec:
                    codec = "opus"
                elif "mp3" in raw_codec or "mpeg-1 layer 3" in raw_codec:
                    codec = "mp3"
                
                audio_tracks.append({
                    "track_id": t_id,
                    "stream_idx": audio_idx,
                    "codec": codec,
                    "channels": props.get("audio_channels", 2),
                    "sample_rate": props.get("audio_sampling_frequency", 48000),
                    "name": props.get("track_name", f"Audio {audio_idx + 1}"),
                    "language": props.get("language", "und"),
                    "default_track": props.get("default_track", False),
                    "forced_track": props.get("forced_track", False),
                    "bitrate": audio_details[audio_idx]["bitrate"] if audio_idx < len(audio_details) else None,
                    "is_atmos": audio_details[audio_idx]["is_atmos"] if audio_idx < len(audio_details) else False
                })
                audio_idx += 1
            elif t_type == "subtitles":
                subtitle_track_ids.append(t_id)
                
        return video_track_id, audio_tracks, subtitle_track_ids
    except Exception as e:
        print(f"   -> [Warning] Impossible d'analyser les pistes détaillées via mkvmerge -J : {e}")
        return None, [], []

def get_all_audio_tracks_ffprobe(ffprobe_path, file_path):
    try:
        cmd = [
            ffprobe_path, "-v", "error",
            "-show_entries", "stream=index,codec_name,codec_type,channels,sample_rate:stream_tags=language,title,handler_name:disposition=default,forced",
            "-of", "json", file_path
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)
        
        audio_tracks = []
        video_track_id = None
        subtitle_track_ids = []
        
        audio_idx = 0
        for stream in data.get("streams", []):
            s_type = stream.get("codec_type")
            s_id = stream.get("index")
            tags = stream.get("tags", {})
            disp = stream.get("disposition", {})
            
            if s_type == "video" and video_track_id is None:
                video_track_id = s_id
            elif s_type == "audio":
                raw_codec = stream.get("codec_name", "").lower()
                codec = "aac"
                if "e-ac-3" in raw_codec or "eac3" in raw_codec:
                    codec = "eac3"
                elif "ac-3" in raw_codec or "ac3" in raw_codec:
                    codec = "ac3"
                elif "truehd" in raw_codec:
                    codec = "truehd"
                elif "flac" in raw_codec:
                    codec = "flac"
                elif "dts" in raw_codec:
                    codec = "dts"
                elif "opus" in raw_codec:
                    codec = "opus"
                elif "mp3" in raw_codec:
                    codec = "mp3"
                    
                audio_tracks.append({
                    "track_id": s_id,
                    "stream_idx": audio_idx,
                    "codec": codec,
                    "channels": int(stream.get("channels", 2)),
                    "sample_rate": int(stream.get("sample_rate", 48000)),
                    "name": tags.get("title") or tags.get("handler_name") or f"Audio {audio_idx + 1}",
                    "language": tags.get("language", "und"),
                    "default_track": bool(disp.get("default", False)),
                    "forced_track": bool(disp.get("forced", False))
                })
                audio_idx += 1
            elif s_type == "subtitles":
                subtitle_track_ids.append(s_id)
                
        # MediaInfo expose le debit et les indicateurs Atmos du bitstream.
        for index, details in enumerate(get_audio_details_mediainfo(file_path)):
            if index >= len(audio_tracks):
                break
            audio_tracks[index]["bitrate"] = details["bitrate"]
            audio_tracks[index]["is_atmos"] = details["is_atmos"]
            
        return video_track_id, audio_tracks, subtitle_track_ids
    except Exception as e:
        print(f"   -> [Warning] Impossible d'analyser les pistes détaillées via ffprobe : {e}")
        return None, [], []

def get_video_audio_metadata(file_path):
    """
    Exécute ffprobe pour extraire les paramètres vidéo et audio, 
    y compris l'espace de couleur, HDR10, Dolby Vision et HDR10+.
    """
    cmd = [
        FFPROBE, "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    data = json.loads(result.stdout)
    
    video_meta = None
    audio_meta = None
    
    for stream in data.get("streams", []):
        if stream.get("codec_type") == "video" and not video_meta:
            # Ignorer les images attachées (covers)
            disposition = stream.get("disposition", {})
            if disposition.get("attached_pic", 0) == 1:
                continue
                
            # Détection de Dolby Vision et HDR10+ dans la side_data_list
            side_data_list = stream.get("side_data_list", [])
            has_dovi = False
            has_hdr10plus = False
            dovi_profile = None
            dovi_compatibility = None
            mastering_display = None
            cll = None
            
            for sd in side_data_list:
                sd_type = sd.get("side_data_type")
                if sd_type == "DOVI configuration record":
                    has_dovi = True
                    dovi_profile = sd.get("dv_profile")
                    dovi_compatibility = sd.get("dv_bl_signal_compatibility_id")
                elif sd_type == "HDR Dynamic Metadata SMPTE2094-40 (HDR10+)":
                    has_hdr10plus = True
                elif sd_type == "Mastering display metadata":
                    mastering_display = sd
                elif sd_type == "Content light level metadata":
                    cll = sd

            # Complément via mediainfo qui analyse directement le bitstream
            try:
                cmd_mi = [
                    MEDIAINFO,
                    "--Inform=Video;%HDR_Format%|%HDR_Format_Compatibility%",
                    file_path
                ]
                res_mi = subprocess.run(cmd_mi, capture_output=True, text=True, check=True)
                mi_out = res_mi.stdout.lower()
                if "dolby vision" in mi_out:
                    has_dovi = True
                if "hdr10+" in mi_out or "smpte st 2094" in mi_out or "smpte2094" in mi_out:
                    has_hdr10plus = True
                
                # Extraction du profil Dolby Vision depuis mediainfo si ffprobe n'a rien trouvé
                if has_dovi and dovi_profile is None:
                    import re
                    m = re.search(r'dv[ah]e\.(\d+)', mi_out)
                    if m:
                        dovi_profile = int(m.group(1))
                    else:
                        dovi_profile = 8  # fallback
                    
                    # Remplissage par défaut de la compatibilité
                    if dovi_profile == 8:
                        dovi_compatibility = 1
                    elif dovi_profile == 7:
                        dovi_compatibility = 6
                    else:
                        dovi_compatibility = 1
            except Exception:
                pass
                    
            sar = stream.get("sample_aspect_ratio", "1:1")
            if not sar or sar == "0:1":
                sar = "1:1"
                
            dar = stream.get("display_aspect_ratio")
            if not dar:
                width = stream.get("width")
                height = stream.get("height")
                if width and height:
                    try:
                        sar_parts = sar.split(":")
                        sar_num = int(sar_parts[0])
                        sar_den = int(sar_parts[1]) if len(sar_parts) > 1 else 1
                    except Exception:
                        sar_num, sar_den = 1, 1
                    dar_num = width * sar_num
                    dar_den = height * sar_den
                    dar = f"{dar_num}:{dar_den}"

            fps = stream.get("avg_frame_rate")
            if parse_fraction(fps) <= 0:
                fps = stream.get("r_frame_rate")

            video_meta = {
                "codec": stream.get("codec_name"),
                "width": stream.get("width"),
                "height": stream.get("height"),
                "pix_fmt": stream.get("pix_fmt"),
                "fps": fps,
                "color_space": stream.get("color_space"),
                "color_transfer": stream.get("color_transfer"),
                "color_primaries": stream.get("color_primaries"),
                "has_dovi": has_dovi,
                "dovi_profile": dovi_profile,
                "dovi_compatibility": dovi_compatibility,
                "has_hdr10plus": has_hdr10plus,
                "mastering_display": mastering_display,
                "cll": cll,
                "sar": sar,
                "dar": dar
            }
        elif stream.get("codec_type") == "audio" and not audio_meta:
            audio_meta = {
                "codec": stream.get("codec_name"),
                "sample_rate": stream.get("sample_rate"),
                "channels": stream.get("channels")
            }
            
    # Complément via mediainfo JSON pour le HDR10 statique (Mastering Display et CLL)
    if video_meta:
        try:
            cmd_mi = [MEDIAINFO, "--output=JSON", file_path]
            res_mi = subprocess.run(cmd_mi, capture_output=True, text=True, check=True)
            mi_data = json.loads(res_mi.stdout)
            for track in mi_data.get("media", {}).get("track", []):
                if track.get("@type") == "Video":
                    video_meta["mastering_primaries"] = track.get("MasteringDisplay_ColorPrimaries")
                    video_meta["mastering_min_lum"] = track.get("MasteringDisplay_Luminance_Min")
                    video_meta["mastering_max_lum"] = track.get("MasteringDisplay_Luminance_Max")
                    video_meta["max_cll"] = track.get("MaxCLL")
                    video_meta["max_fall"] = track.get("MaxFALL")
        except Exception as e:
            print(f"   -> [Warning] Échec de la récupération des métadonnées HDR10 statiques via mediainfo : {e}")
            
    return video_meta, audio_meta

def get_exact_frame_count(file_path):
    """Compte le nombre exact de frames vidéo."""
    cmd = [
        FFPROBE, "-v", "error",
        "-select_streams", "v:0",
        "-count_packets",
        "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0",
        file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    try:
        return int(result.stdout.strip())
    except ValueError:
        # Fallback si l'option count_packets est lente ou échoue
        return 0

def get_duration_ms(file_path):
    """Retourne la durée exacte du conteneur, arrondie à la milliseconde."""
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "format=duration",
        "-of", "json", file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    duration = json.loads(result.stdout).get("format", {}).get("duration")
    try:
        return int(round(float(duration) * 1000))
    except (TypeError, ValueError):
        raise ValueError(f"durée introuvable pour {file_path}")

def get_chapters(file_path):
    """Lit les chapitres via FFprobe afin de pouvoir recalculer leurs positions."""
    cmd = [
        FFPROBE, "-v", "error",
        "-show_chapters", "-of", "json", file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    chapters = []
    for chapter in json.loads(result.stdout).get("chapters", []):
        try:
            start_ms = int(round(float(chapter["start_time"]) * 1000))
            end_ms = int(round(float(chapter["end_time"]) * 1000))
        except (KeyError, TypeError, ValueError):
            continue
        chapters.append({
            "start_ms": start_ms,
            "end_ms": end_ms,
            "title": chapter.get("tags", {}).get("title", "")
        })
    return chapters

def get_subtitle_tracks_ffprobe(file_path):
    """Retourne les positions et codecs des sous-titres pour un remux sans conversion."""
    cmd = [
        FFPROBE, "-v", "error",
        "-show_entries", "stream=codec_name,codec_type",
        "-of", "json", file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    tracks = []
    for stream in json.loads(result.stdout).get("streams", []):
        if stream.get("codec_type") == "subtitle":
            tracks.append({"position": len(tracks), "codec": stream.get("codec_name", "")})
    return tracks

def prepare_shifted_subtitle_sources(file_path, subtitle_tracks, offset_ms, temp_dir, temp_files):
    """Remuxe les sous-titres avec un offset, sans modifier leurs données de cue."""
    sources = []
    for track in subtitle_tracks:
        # MOV_TEXT ne peut pas porter de timestamp initial positif dans un MP4
        # isolé. Il est donc converti en SRT dans Matroska. SRT, PGS et les
        # autres codecs sont recopies bit-a-bit dans Matroska.
        is_mov_text = track["codec"] == "mov_text"
        output_path = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False, dir=temp_dir).name
        temp_files.append(output_path)
        cmd = [
            FFMPEG, "-y", "-nostdin", "-hide_banner",
            "-itsoffset", f"{offset_ms / 1000:.6f}", "-copyts", "-i", file_path,
            "-map", f"0:s:{track['position']}", "-c:s", "srt" if is_mov_text else "copy",
            output_path
        ]
        run_command(cmd)
        sources.append(output_path)
    return sources

def build_shifted_chapters(chapters, offset_ms):
    """Construit les chapitres source avec un offset, sans créer de chapitres de segment."""
    return [
        {
            "timecode_s": (chapter["start_ms"] + offset_ms) / 1000,
            "title": chapter["title"]
        }
        for chapter in chapters
    ]

def write_shifted_ffmetadata(chapters, offset_ms, temp_dir, temp_files):
    """Écrit des chapitres FFmetadata recalés pour le fallback FFmpeg."""
    chapter_file = tempfile.NamedTemporaryFile(suffix=".ffmeta", mode="w", delete=False, encoding="utf-8", dir=temp_dir)
    chapter_file.write(";FFMETADATA1\n")
    for chapter in chapters:
        title = chapter["title"].replace("\\", "\\\\").replace("\n", "\\n").replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
        chapter_file.write("[CHAPTER]\nTIMEBASE=1/1000\n")
        chapter_file.write(f"START={chapter['start_ms'] + offset_ms}\n")
        chapter_file.write(f"END={chapter['end_ms'] + offset_ms}\n")
        if title:
            chapter_file.write(f"title={title}\n")
    chapter_file.close()
    temp_files.append(chapter_file.name)
    return chapter_file.name

def write_shifted_matroska_chapters(chapters, offset_ms, temp_dir, temp_files):
    """Écrit une hiérarchie de chapitres Matroska avec timecodes recalculés."""
    def format_timecode(milliseconds):
        hours, remainder = divmod(milliseconds, 3_600_000)
        minutes, remainder = divmod(remainder, 60_000)
        seconds, milliseconds = divmod(remainder, 1000)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds * 1_000_000:09d}"

    chapter_file = tempfile.NamedTemporaryFile(suffix=".chapters.xml", mode="w", delete=False, encoding="utf-8", dir=temp_dir)
    chapter_file.write('<?xml version="1.0" encoding="UTF-8"?>\n<Chapters>\n  <EditionEntry>\n')
    for chapter in chapters:
        chapter_file.write("    <ChapterAtom>\n")
        chapter_file.write(f"      <ChapterTimeStart>{format_timecode(chapter['start_ms'] + offset_ms)}</ChapterTimeStart>\n")
        if chapter["title"]:
            chapter_file.write("      <ChapterDisplay>\n")
            chapter_file.write(f"        <ChapterString>{xml_escape(chapter['title'])}</ChapterString>\n")
            chapter_file.write("        <ChapterLanguage>und</ChapterLanguage>\n      </ChapterDisplay>\n")
        chapter_file.write("    </ChapterAtom>\n")
    chapter_file.write("  </EditionEntry>\n</Chapters>\n")
    chapter_file.close()
    temp_files.append(chapter_file.name)
    return chapter_file.name

def split_rpu_nal_units(rpu_bytes):
    """Sépare les unités NAL RPU Dolby Vision."""
    nals = []
    i = 0
    n = len(rpu_bytes)
    last_idx = 0
    while i < n - 4:
        if rpu_bytes[i:i+4] == b'\x00\x00\x00\x01':
            if last_idx != i:
                nals.append(rpu_bytes[last_idx:i])
            last_idx = i
            i += 4
        elif rpu_bytes[i:i+3] == b'\x00\x00\x01':
            if last_idx != i:
                nals.append(rpu_bytes[last_idx:i])
            last_idx = i
            i += 3
        else:
            i += 1
    if last_idx < n:
        nals.append(rpu_bytes[last_idx:])
    return nals

AV1_OBU_TEMPORAL_DELIMITER = 2
AV1_OBU_METADATA = 5
AV1_METADATA_TYPE_ITUT_T35 = 4

def read_leb128(data, offset):
    """Lit un entier LEB128 AV1 et retourne ``(valeur, position_suivante)``."""
    value = 0
    shift = 0
    while offset < len(data) and shift < 64:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7f) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7
    raise ValueError("LEB128 AV1 invalide ou tronqué")

def parse_av1_obus(data):
    """Parse les OBUs d'un flux AV1 low-overhead avec champs de taille."""
    obus = []
    offset = 0
    while offset < len(data):
        start = offset
        header = data[offset]
        offset += 1
        if header & 0x80:
            raise ValueError("OBU AV1 invalide (forbidden bit positionné)")
        obu_type = (header >> 3) & 0x0f
        extension_flag = bool(header & 0x04)
        has_size_field = bool(header & 0x02)
        if extension_flag:
            if offset >= len(data):
                raise ValueError("extension OBU AV1 tronquée")
            offset += 1
        if not has_size_field:
            raise ValueError("OBU AV1 sans champ de taille non pris en charge")
        payload_size, offset = read_leb128(data, offset)
        payload_start = offset
        end = payload_start + payload_size
        if end > len(data):
            raise ValueError("payload OBU AV1 tronqué")
        obus.append({
            "type": obu_type,
            "start": start,
            "end": end,
            "payload": data[payload_start:end],
            "raw": data[start:end]
        })
        offset = end
    return obus

def split_av1_temporal_units(data):
    """Découpe un flux AV1 en préambule et unités temporelles délimitées."""
    obus = parse_av1_obus(data)
    first_td_index = next(
        (idx for idx, obu in enumerate(obus) if obu["type"] == AV1_OBU_TEMPORAL_DELIMITER),
        None
    )
    if first_td_index is None:
        raise ValueError("le flux AV1 ne contient pas de Temporal Delimiter OBU")

    prefix = data[:obus[first_td_index]["start"]]
    units = []
    current = bytearray()
    for obu in obus[first_td_index:]:
        if obu["type"] == AV1_OBU_TEMPORAL_DELIMITER:
            if current:
                units.append(bytes(current))
            current = bytearray()
        current.extend(obu["raw"])
    if current:
        units.append(bytes(current))
    return prefix, units

def is_itu_t35_metadata_obu(obu):
    if obu["type"] != AV1_OBU_METADATA:
        return False
    try:
        metadata_type, _ = read_leb128(obu["payload"], 0)
    except ValueError:
        return False
    return metadata_type == AV1_METADATA_TYPE_ITUT_T35

def concat_av1_with_dynamic_metadata(segment_paths, main_path, output_path, expected_segment_frames):
    """Ajoute les métadonnées T.35 à chaque image des segments ajoutés.

    Dolby Vision Profile 10 et HDR10+ dans AV1 sont portés dans des OBUs Metadata
    ITU-T T.35. Les outils dovi_tool/hdr10plus_tool ne traitent que le HEVC,
    cette voie recopie donc directement ces OBUs dans le bitstream AV1.
    """
    main_data = Path(main_path).read_bytes()
    main_prefix, main_units = split_av1_temporal_units(main_data)
    if not main_units:
        raise ValueError("le flux AV1 principal ne contient aucune unité temporelle")
    first_unit_obus = parse_av1_obus(main_units[0])
    dynamic_obus = [obu["raw"] for obu in first_unit_obus if is_itu_t35_metadata_obu(obu)]
    if not dynamic_obus:
        raise ValueError("aucune métadonnée ITU-T T.35 dynamique trouvée dans la première image AV1")

    with open(output_path, "wb") as f_out:
        wrote_prefix = False
        def write_dynamic_segment(label):
            nonlocal wrote_prefix
            segment_path = segment_paths.get(label)
            if not segment_path:
                return
            segment_prefix, segment_units = split_av1_temporal_units(Path(segment_path).read_bytes())
            expected_frames = expected_segment_frames.get(label, 0)
            if expected_frames and len(segment_units) != expected_frames:
                raise ValueError(
                    f"nombre d'unités temporelles AV1 inattendu pour {label} ({len(segment_units)} au lieu de {expected_frames})"
                )
            if not wrote_prefix:
                f_out.write(segment_prefix)
                wrote_prefix = True
            for unit in segment_units:
                unit_obus = parse_av1_obus(unit)
                first_obu = unit_obus[0]
                if first_obu["type"] != AV1_OBU_TEMPORAL_DELIMITER:
                    raise ValueError("unité temporelle AV1 invalide sans Temporal Delimiter initial")
                f_out.write(first_obu["raw"])
                for dynamic_obu in dynamic_obus:
                    f_out.write(dynamic_obu)
                f_out.write(unit[first_obu["end"]:])
        write_dynamic_segment("intro")
        if not wrote_prefix:
            f_out.write(main_prefix)
            wrote_prefix = True
        for unit in main_units:
            f_out.write(unit)
        write_dynamic_segment("outro")

def quote_ffconcat_path(path):
    """Protège un chemin pour un fichier de liste du démuxeur concat FFmpeg."""
    return os.path.abspath(path).replace(os.sep, "/").replace("'", r"'\''")

def concat_containerized_video(segment_paths, output_path, temp_dir, temp_files):
    """Concatène des pistes vidéo compatibles sans imposer de bitstream brut."""
    temp_main_video = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False, dir=temp_dir).name
    temp_files.append(temp_main_video)
    run_command([
        FFMPEG, "-y", "-nostdin", "-hide_banner",
        "-i", segment_paths["main"],
        "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn",
        temp_main_video
    ])

    list_file = tempfile.NamedTemporaryFile(suffix=".ffconcat", mode="w", delete=False, encoding="utf-8", dir=temp_dir)
    if segment_paths.get("intro"):
        list_file.write(f"file '{quote_ffconcat_path(segment_paths['intro'])}'\n")
    list_file.write(f"file '{quote_ffconcat_path(temp_main_video)}'\n")
    if segment_paths.get("outro"):
        list_file.write(f"file '{quote_ffconcat_path(segment_paths['outro'])}'\n")
    list_file.close()
    temp_files.append(list_file.name)
    run_command([
        FFMPEG, "-y", "-nostdin", "-hide_banner",
        "-f", "concat", "-safe", "0", "-i", list_file.name,
        "-map", "0:v:0", "-c:v", "copy", "-an", "-sn", "-dn",
        output_path
    ])

def calculate_scale_crop_pad(intro_meta, target_meta, mode="crop"):
    """
    Calcule les filtres crop/scale/pad pour adapter l'intro aux dimensions cibles
    sans étirer l'image.
    """
    w_in = intro_meta["width"]
    h_in = intro_meta["height"]
    
    # Parse SAR de l'intro
    sar_in = intro_meta.get("sar", "1:1")
    try:
        parts = sar_in.split(":")
        sar_in_num = int(parts[0])
        sar_in_den = int(parts[1]) if len(parts) > 1 else 1
    except Exception:
        sar_in_num, sar_in_den = 1, 1
        
    # Aspect ratio d'affichage de l'intro
    r_in = (w_in * sar_in_num) / (h_in * sar_in_den)
    
    w_target = target_meta["width"]
    h_target = target_meta["height"]
    
    # Parse SAR de la cible
    sar_target = target_meta.get("sar", "1:1")
    try:
        parts = sar_target.split(":")
        sar_target_num = int(parts[0])
        sar_target_den = int(parts[1]) if len(parts) > 1 else 1
    except Exception:
        sar_target_num, sar_target_den = 1, 1
        
    # Aspect ratio d'affichage de la cible
    r_target = (w_target * sar_target_num) / (h_target * sar_target_den)
    
    filters = []
    
    if mode == "crop":
        # Mode CROP : On remplit tout l'écran en coupant l'excédent (haut/bas ou gauche/droite)
        if r_in > r_target:
            # L'intro est trop large -> crop à gauche et à droite
            w_crop = int(round(h_in * r_target * (sar_in_den / sar_in_num)))
            h_crop = h_in
            filters.append(f"crop={w_crop}:{h_crop}")
        else:
            # L'intro est trop haute -> crop en haut et en bas
            w_crop = w_in
            h_crop = int(round((w_in / r_target) * (sar_in_num / sar_in_den)))
            filters.append(f"crop={w_crop}:{h_crop}")
            
        # Ensuite, mise à l'échelle finale
        filters.append(f"scale={w_target}:{h_target}")
        
    else:
        # Mode PAD : On garde toute l'image de l'intro et on rajoute des bandes noires
        if r_in > r_target:
            # L'intro est trop large -> on scale sur la largeur et on rajoute du noir en haut et en bas
            w_scale = w_target
            h_scale = int(round((w_target / r_in) * (sar_target_num / sar_target_den)))
            filters.append(f"scale={w_scale}:{h_scale}")
        else:
            # L'intro est trop haute -> on scale sur la hauteur et on rajoute du noir à gauche et à droite
            w_scale = int(round(h_target * r_in * (sar_target_den / sar_target_num)))
            h_scale = h_target
            filters.append(f"scale={w_scale}:{h_scale}")
            
        # Rendre les dimensions de mise à l'échelle paires
        w_scale = (w_scale // 2) * 2
        h_scale = (h_scale // 2) * 2
        
        # Ajustement du filtre de mise à l'échelle
        filters[-1] = f"scale={w_scale}:{h_scale}"
        
        # Ajout du rembourrage noir (padding)
        filters.append(f"pad={w_target}:{h_target}:(ow-iw)/2:(oh-ih)/2:black")
        
    return filters

def run_command(cmd, show_output=False):
    """Exécute une commande et capture les erreurs proprement."""
    if show_output:
        result = subprocess.run(cmd, check=False)
    else:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
        
    # mkvmerge retourne 1 en cas d'avertissements, mais l'opération réussit quand même.
    # On autorise donc le code de retour 1 si la commande est ou contient 'mkvmerge'.
    is_mkvmerge = any("mkvmerge" in str(part).lower() for part in cmd)
    allowed_codes = [0, 1] if is_mkvmerge else [0]
    
    if result.returncode not in allowed_codes:
        print(f"\n[Erreur] Commande échouée : {' '.join(cmd)}")
        if not show_output:
            if result.stdout:
                print(f"Stdout:\n{result.stdout}")
            if result.stderr:
                print(f"Stderr:\n{result.stderr}")
        raise subprocess.CalledProcessError(result.returncode, cmd, output=getattr(result, 'stdout', None), stderr=getattr(result, 'stderr', None))
    return result

# Ce script historique dépasse la limite d'analyse de flux de Pyright.
def concat_videos(main_path, output_path, intro_path=None, outro_path=None, workdir=None, mode="crop"):  # pyright: ignore[reportGeneralTypeIssues]
    # Vérification des outils indispensables de base
    for tool_name, tool_path in [("ffmpeg", FFMPEG), ("ffprobe", FFPROBE), ("mediainfo", MEDIAINFO)]:
        if not is_tool_available(tool_path):
            print(f"Erreur : L'outil indispensable de base '{tool_name}' est manquant ou introuvable.")
            print(f"Veuillez l'installer et l'ajouter à votre PATH ou configurer sa variable '{tool_name.upper()}_PATH' au début de ce script.")
            return False

    # Si MUXIVEO_PATH est configuré, on valide MUXIVEO. Sinon on valide MKVMERGE.
    use_muxiveo = MUXIVEO_PATH is not None
    if use_muxiveo:
        if not is_tool_available(MUXIVEO):
            print(f"Erreur : L'outil Muxiveo configuré ('{MUXIVEO}') est manquant ou introuvable.")
            print("Veuillez l'installer et l'ajouter à votre PATH ou corriger la variable 'MUXIVEO_PATH' au début de ce script.")
            return False
    else:
        if not is_tool_available(MKVMERGE):
            print("[Warning] mkvmerge est introuvable : utilisation du fallback FFmpeg.")
            print("           Les pièces jointes et les pistes audio non concaténées ne pourront pas être conservées.")

    temp_files = []

    if not intro_path and not outro_path:
        print("Erreur : fournissez au moins une intro (--intro) ou une outro (--outro).")
        return False
    
    # Détermination du dossier temporaire
    # Priorité : argument CLI (workdir) > variable globale (DEFAULT_WORKDIR) > comportement par défaut par OS
    if workdir:
        temp_dir = workdir
    elif DEFAULT_WORKDIR:
        temp_dir = DEFAULT_WORKDIR
    elif sys.platform == "win32":
        temp_dir = None  # Utilise %temp% sous Windows
    else:
        # Sous Linux/macOS, utilise le dossier du fichier de sortie
        temp_dir = os.path.dirname(os.path.abspath(output_path))
        
    # S'assurer que le dossier temporaire existe
    if temp_dir and not os.path.exists(temp_dir):
        os.makedirs(temp_dir, exist_ok=True)
        
    try:
        print("[1/5] Analyse des vidéos...")
        video_meta, audio_meta = get_video_audio_metadata(main_path)
        if not video_meta:
            print("Erreur : Impossible d'analyser la vidéo principale.")
            return False

        intro_meta = None
        outro_meta = None
        if intro_path:
            intro_meta, _ = get_video_audio_metadata(intro_path)
            if not intro_meta:
                print("Erreur : Impossible d'analyser la vidéo d'intro.")
                return False
        if outro_path:
            outro_meta, _ = get_video_audio_metadata(outro_path)
            if not outro_meta:
                print("Erreur : Impossible d'analyser la vidéo d'outro.")
                return False
            
        print("Vidéo principale détectée :")
        print(f"  - Résolution : {video_meta['width']}x{video_meta['height']} @ {video_meta['fps']} FPS")
        print(f"  - Codec : {video_meta['codec']} ({video_meta['pix_fmt']})")
        print(f"  - Couleurs : Primaries={video_meta['color_primaries']}, Transfer={video_meta['color_transfer']}, Matrix={video_meta['color_space']}")
        print(f"  - Dolby Vision : {'Oui' if video_meta['has_dovi'] else 'Non'}")
        if video_meta['has_dovi']:
            print(f"    - Profil DoVi : {video_meta['dovi_profile']} (Compatibilité : {video_meta['dovi_compatibility']})")
        print(f"  - HDR10+ : {'Oui' if video_meta['has_hdr10plus'] else 'Non'}")
        
        has_dovi = video_meta['has_dovi']
        has_hdr10plus = video_meta['has_hdr10plus']
        video_codec = video_meta["codec"]
        supported_video_codecs = {"h264", "hevc", "vp9", "av1"}
        if video_codec not in supported_video_codecs:
            print(f"Erreur : codec vidéo non pris en charge pour la concaténation : {video_codec!r}")
            print("Codecs pris en charge : H.264, HEVC, VP9 et AV1.")
            return False

        if (has_dovi or has_hdr10plus) and video_codec not in {"hevc", "av1"}:
            dynamic_labels = ", ".join(label for label, enabled in (("Dolby Vision", has_dovi), ("HDR10+", has_hdr10plus)) if enabled)
            print(f"Erreur : {dynamic_labels} n'est pris en charge que pour HEVC ou AV1.")
            return False
        
        # Choix des encodeurs et paramètres
        codec_map = {
            "h264": "libx264",
            "hevc": "libx265",
            "vp9": "libvpx-vp9",
            "av1": "libsvtav1"
        }
        v_encoder = codec_map[video_codec]
        
        if audio_meta:
            print(f"Audio principal détecté : Codec={audio_meta['codec']}, Freq={audio_meta['sample_rate']}Hz, Canaux={audio_meta['channels']}")
        else:
            print("Pas d'audio détecté sur la vidéo principale.")
 
        # Les outils dynamiques historiques ne travaillent que sur HEVC. AV1
        # transporte ses métadonnées dynamiques dans des OBUs T.35, traités plus bas.
        temp_main_hevc = None
        temp_main_av1 = None
        if video_codec == "hevc":
            temp_main_hevc = tempfile.NamedTemporaryFile(suffix=".hevc", delete=False, dir=temp_dir).name
            temp_files.append(temp_main_hevc)
            print("\n[2/5] Extraction du flux vidéo HEVC brut du film principal...")
            run_command([
                FFMPEG, "-y", "-nostdin", "-hide_banner",
                "-i", main_path,
                "-map", "0:v:0", "-c:v", "copy", "-an", "-f", "hevc", temp_main_hevc
            ])
        elif video_codec == "av1" and (has_dovi or has_hdr10plus):
            temp_main_av1 = tempfile.NamedTemporaryFile(suffix=".obu", delete=False, dir=temp_dir).name
            temp_files.append(temp_main_av1)
            print("\n[2/5] Extraction du flux vidéo AV1 et insertion des délimiteurs temporels...")
            run_command([
                FFMPEG, "-y", "-nostdin", "-hide_banner",
                "-i", main_path,
                "-map", "0:v:0", "-c:v", "copy",
                "-bsf:v", "av1_metadata=td=insert", "-an", "-f", "obu", temp_main_av1
            ])
        else:
            print("\n[2/5] Préparation de la concaténation vidéo sans extraction de bitstream dynamique...")
        
        # --- Préparation des métadonnées dynamiques ---
        temp_main_rpu = None
        temp_main_json = None
        
        if has_dovi and video_codec == "hevc":
            if not is_tool_available(DOVI_TOOL):
                print("Erreur : Dolby Vision est détecté, mais l'outil requis 'dovi_tool' est introuvable.")
                print("Veuillez l'installer ou configurer son chemin via la variable 'DOVI_TOOL_PATH' au début de ce script.")
                return False
            print("   -> Dolby Vision détecté. Extraction du RPU du film principal...")
            temp_main_rpu = tempfile.NamedTemporaryFile(suffix=".rpu", delete=False, dir=temp_dir).name
            temp_files.append(temp_main_rpu)
            
            extract_rpu_cmd = [
                DOVI_TOOL, "extract-rpu",
                "-i", temp_main_hevc,
                "-o", temp_main_rpu
            ]
            run_command(extract_rpu_cmd)
            
        if has_hdr10plus and video_codec == "hevc":
            if not is_tool_available(HDR10PLUS_TOOL):
                print("Erreur : HDR10+ est détecté, mais l'outil requis 'hdr10plus_tool' est introuvable.")
                print("Veuillez l'installer ou configurer son chemin via la variable 'HDR10PLUS_TOOL_PATH' au début de ce script.")
                return False
            print("   -> HDR10+ détecté. Extraction des métadonnées HDR10+ du film principal...")
            temp_main_json = tempfile.NamedTemporaryFile(suffix=".json", delete=False, dir=temp_dir).name
            temp_files.append(temp_main_json)
            
            extract_hdr10p_cmd = [
                HDR10PLUS_TOOL, "extract",
                "-o", temp_main_json,
                temp_main_hevc
            ]
            run_command(extract_hdr10p_cmd)

        # Préparation des paramètres d'encodage vidéo pour l'intro (crop/pad pour préserver l'aspect ratio)
        sar_val = video_meta.get("sar", "1:1").replace(":", "/")
        primary_path = intro_path or outro_path
        primary_meta = intro_meta or outro_meta
        video_filters = calculate_scale_crop_pad(primary_meta, video_meta, mode=mode)
        video_filters.append(f"setsar={sar_val}")

        # --- ENCODAGE INTRO (Un seul pass, GOP=1 pour éviter les frame drops) ---
        print("\n[3/5] Encodage de l'intro au format de la vidéo principale...")
        # Matroska donne un conteneur homogène aux quatre codecs, y compris
        # lorsque la source est un MP4 ou un WebM.
        temp_primary_matched = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False, dir=temp_dir).name
        temp_files.append(temp_primary_matched)
        
        intro_cmd = [
            FFMPEG, "-y", "-nostdin", "-hide_banner",
            "-i", primary_path,
            "-c:v", v_encoder,
            "-vf", ",".join(video_filters),
            "-r", video_meta["fps"],
            "-g", "1",  # Uniquement des I-frames
        ]
        if v_encoder in {"libx264", "libx265"}:
            intro_cmd.extend(["-preset", "ultrafast"])
        elif v_encoder == "libvpx-vp9":
            intro_cmd.extend(["-deadline", "realtime", "-cpu-used", "8", "-row-mt", "1"])
        elif v_encoder == "libsvtav1":
            intro_cmd.extend(["-preset", "12"])
            # Les OBUs T.35 Dolby Vision/HDR10+ sont injectés après encodage.
            if has_dovi or has_hdr10plus:
                intro_cmd.extend(["-dolbyvision", "0"])
        if video_meta["pix_fmt"]:
            intro_cmd.extend(["-pix_fmt", video_meta["pix_fmt"]])
        if video_meta["color_primaries"]:
            intro_cmd.extend(["-color_primaries", video_meta["color_primaries"]])
        if video_meta["color_transfer"]:
            intro_cmd.extend(["-color_trc", video_meta["color_transfer"]])
        if video_meta["color_space"]:
            intro_cmd.extend(["-colorspace", video_meta["color_space"]])
            
        if v_encoder == "libx265":
            x265_params = ["keyint=1", "bframes=0", "scenecut=0", "rc-lookahead=0", "open-gop=0"]
            if video_meta.get("color_primaries"):
                x265_params.append(f"colorprim={video_meta['color_primaries']}")
            if video_meta.get("color_transfer"):
                x265_params.append(f"transfer={video_meta['color_transfer']}")
            if video_meta.get("color_space"):
                x265_params.append(f"colormatrix={video_meta['color_space']}")
            # Mastering display et CLL
            md = video_meta.get("mastering_display")
            if md:
                gx = int(parse_fraction(md.get("green_x")) * 50000)
                gy = int(parse_fraction(md.get("green_y")) * 50000)
                bx = int(parse_fraction(md.get("blue_x")) * 50000)
                by = int(parse_fraction(md.get("blue_y")) * 50000)
                rx = int(parse_fraction(md.get("red_x")) * 50000)
                ry = int(parse_fraction(md.get("red_y")) * 50000)
                wpx = int(parse_fraction(md.get("white_point_x")) * 50000)
                wpy = int(parse_fraction(md.get("white_point_y")) * 50000)
                max_lum = int(parse_fraction(md.get("max_luminance")) * 10000)
                min_lum = int(parse_fraction(md.get("min_luminance")) * 10000)
                master_display = f"G({gx},{gy})B({bx},{by})R({rx},{ry})WP({wpx},{wpy})L({max_lum},{min_lum})"
                x265_params.append(f"master-display={master_display}")
            cll = video_meta.get("cll")
            if cll:
                max_cll = cll.get("max_content", 0)
                max_fall = cll.get("max_average", 0)
                x265_params.append(f"max-cll={max_cll},{max_fall}")
            if x265_params:
                x265_params.append("hdr10=1")
                intro_cmd.extend(["-x265-params", ":".join(x265_params)])
                
        # Traitement audio de l'intro
        # Encodage de la vidéo seule pour l'intro (l'audio de l'intro sera traité et concaténé pour chaque piste séparément)
        intro_cmd.append("-an")
            
        intro_cmd.append(temp_primary_matched)
        run_command(intro_cmd, show_output=True)
        print("   -> Segment préparé avec succès.")
        
        primary_frames = get_exact_frame_count(temp_primary_matched)
        temp_intro_matched = temp_primary_matched if intro_path else None
        temp_outro_matched = temp_primary_matched if outro_path else None
        intro_frames = primary_frames if intro_path else 0
        outro_frames = primary_frames if outro_path else 0
        intro_duration_ms = get_duration_ms(temp_intro_matched) if temp_intro_matched else 0
        outro_duration_ms = get_duration_ms(temp_outro_matched) if temp_outro_matched else 0

        if intro_path and outro_path:
            print("   -> Encodage de l'outro au format de la vidéo principale...")
            temp_outro_matched = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False, dir=temp_dir).name
            temp_files.append(temp_outro_matched)
            outro_filters = calculate_scale_crop_pad(outro_meta, video_meta, mode=mode)
            outro_filters.append(f"setsar={sar_val}")
            outro_cmd = intro_cmd.copy()
            outro_cmd[outro_cmd.index("-i") + 1] = outro_path
            outro_cmd[outro_cmd.index("-vf") + 1] = ",".join(outro_filters)
            outro_cmd[-1] = temp_outro_matched
            run_command(outro_cmd, show_output=True)
            outro_frames = get_exact_frame_count(temp_outro_matched)
            outro_duration_ms = get_duration_ms(temp_outro_matched)

        print(f"   -> Images intro : {intro_frames}; images outro : {outro_frames}")

        # Étape 4 : concaténation vidéo, adaptée au codec de la source.
        print("\n[4/5] Concaténation de la piste vidéo...")
        matched_segments = {"intro": temp_intro_matched, "main": main_path, "outro": temp_outro_matched}
        if video_codec == "hevc":
            # dovi_tool et hdr10plus_tool opèrent sur des NAL HEVC Annex-B.
            temp_concat_hevc = tempfile.NamedTemporaryFile(suffix=".hevc", delete=False, dir=temp_dir).name
            temp_files.append(temp_concat_hevc)
            print("   -> Extraction et concaténation des flux HEVC...")
            hevc_parts = []
            for label in ("intro", "outro"):
                segment_path = matched_segments[label]
                if not segment_path:
                    continue
                temp_segment_hevc = tempfile.NamedTemporaryFile(suffix=".hevc", delete=False, dir=temp_dir).name
                temp_files.append(temp_segment_hevc)
                run_command([
                    FFMPEG, "-y", "-nostdin", "-hide_banner",
                    "-i", segment_path,
                    "-map", "0:v:0", "-c:v", "copy", "-f", "hevc", temp_segment_hevc
                ])
                hevc_parts.append((label, temp_segment_hevc))
            with open(temp_concat_hevc, "wb") as f_out:
                ordered_parts = []
                if matched_segments["intro"]:
                    ordered_parts.append(next(path for label, path in hevc_parts if label == "intro"))
                ordered_parts.append(temp_main_hevc)
                if matched_segments["outro"]:
                    ordered_parts.append(next(path for label, path in hevc_parts if label == "outro"))
                for path_in in ordered_parts:
                    with open(path_in, "rb") as f_in:
                        shutil.copyfileobj(f_in, f_out, 1024 * 1024)
            working_hevc = temp_concat_hevc
            working_video_is_raw = True
            for path in [path for _, path in hevc_parts] + [temp_main_hevc]:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        if path in temp_files:
                            temp_files.remove(path)
                    except OSError:
                        pass
        elif video_codec == "av1" and (has_dovi or has_hdr10plus):
            temp_concat_av1 = tempfile.NamedTemporaryFile(suffix=".obu", delete=False, dir=temp_dir).name
            temp_files.append(temp_concat_av1)
            print("   -> Injection des métadonnées AV1 ITU-T T.35 sur l'intro...")
            av1_parts = {}
            for label in ("intro", "outro"):
                segment_path = matched_segments[label]
                if not segment_path:
                    continue
                temp_segment_av1 = tempfile.NamedTemporaryFile(suffix=".obu", delete=False, dir=temp_dir).name
                temp_files.append(temp_segment_av1)
                run_command([
                    FFMPEG, "-y", "-nostdin", "-hide_banner",
                    "-i", segment_path,
                    "-map", "0:v:0", "-c:v", "copy",
                    "-bsf:v", "av1_metadata=td=insert", "-f", "obu", temp_segment_av1
                ])
                av1_parts[label] = temp_segment_av1
            concat_av1_with_dynamic_metadata(av1_parts, temp_main_av1, temp_concat_av1, {"intro": intro_frames, "outro": outro_frames})
            working_hevc = temp_concat_av1
            working_video_is_raw = True
            for path in list(av1_parts.values()) + [temp_main_av1]:
                if os.path.exists(path):
                    try:
                        os.remove(path)
                        if path in temp_files:
                            temp_files.remove(path)
                    except OSError:
                        pass
        else:
            # Le démuxeur concat préserve H.264, VP9 et AV1 sans
            # jamais interpréter leurs flux comme du HEVC.
            temp_concat_video = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False, dir=temp_dir).name
            temp_files.append(temp_concat_video)
            print(f"   -> Concaténation conteneurisée du flux {video_codec.upper()}...")
            concat_containerized_video(matched_segments, temp_concat_video, temp_dir, temp_files)
            working_hevc = temp_concat_video
            working_video_is_raw = False

        # Concaténation de TOUTES les pistes audio compatibles
        concatenated_audio_tracks = []
        delayed_track_ids = []
        
        # Si on utilise Muxiveo, mkvmerge est strictement interdit par politique
        use_muxiveo = MUXIVEO_PATH is not None and is_tool_available(MUXIVEO)
        if use_muxiveo:
            mkvmerge_path = None
        else:
            mkvmerge_path = MKVMERGE if is_tool_available(MKVMERGE) else None
        
        if mkvmerge_path:
            print("   -> Analyse des pistes audio de la vidéo principale via mkvmerge...")
            video_track_id, audio_tracks, subtitle_track_ids = get_all_audio_tracks_mkvmerge(mkvmerge_path, main_path)
        else:
            print("   -> Analyse des pistes audio de la vidéo principale via ffprobe...")
            video_track_id, audio_tracks, subtitle_track_ids = get_all_audio_tracks_ffprobe(FFPROBE, main_path)
            if not audio_tracks and audio_meta:
                audio_tracks.append({
                    "track_id": 1,
                    "stream_idx": 0,
                    "codec": audio_meta["codec"],
                    "channels": audio_meta["channels"],
                    "sample_rate": audio_meta["sample_rate"],
                    "name": "Audio principal"
                })
        
        AUDIO_ENCODERS = {
            "aac": "aac",
            "ac3": "ac3",
            "eac3": "eac3",
            "flac": "flac",
            "opus": "opus",
            "mp3": "libmp3lame",
            "truehd": "truehd",
            "dts": "dts"
        }
        
        intro_duration_sec = intro_duration_ms / 1000
        outro_duration_sec = outro_duration_ms / 1000
        source_chapters = get_chapters(main_path)
        source_subtitle_tracks = get_subtitle_tracks_ffprobe(main_path)
        rewritten_subtitle_files = prepare_shifted_subtitle_sources(
            main_path, source_subtitle_tracks, intro_duration_ms, temp_dir, temp_files
        )
        
        for track in audio_tracks:
            t_id = track["track_id"]
            s_idx = track["stream_idx"]
            codec = track["codec"]
            channels = int(track["channels"])
            sample_rate = int(track["sample_rate"])
            name = track["name"]
            
            encoder = AUDIO_ENCODERS.get(codec)
            if encoder:
                # Atmos est detecte dans les metadonnees MediaInfo du bitstream,
                # jamais a partir du titre configurable de la piste.
                is_atmos = bool(track.get("is_atmos", False))
                
                print(f"   -> Concaténation de la piste audio d'origine {t_id} ({name}, codec: {codec}, canaux: {channels})...")
                temp_main_audio_t = tempfile.NamedTemporaryFile(suffix=".mka", delete=False, dir=temp_dir).name
                temp_concat_audio_t = tempfile.NamedTemporaryFile(suffix=".mka", delete=False, dir=temp_dir).name
                temp_files.extend([temp_main_audio_t, temp_concat_audio_t])
                temp_intro_audio_t = None
                temp_outro_audio_t = None

                temp_atmos_frame_t = None
                if is_atmos:
                    temp_atmos_frame_t = tempfile.NamedTemporaryFile(suffix=".mka", delete=False, dir=temp_dir).name
                    temp_files.append(temp_atmos_frame_t)
                
                # 1. Essai de transcodage de l'audio de l'intro
                transcode_ok = not intro_path
                bitrate = track.get("bitrate")
                
                # Reserve 32 ms pour la premiere trame Atmos originale, afin
                # de conserver l'en-tete JOC au debut de la piste concatenee.
                shortened_sec = max(0.0, intro_duration_sec - 0.032) if is_atmos and intro_path else intro_duration_sec

                if intro_path:
                    temp_intro_audio_t = tempfile.NamedTemporaryFile(suffix=".mka", delete=False, dir=temp_dir).name
                    temp_files.append(temp_intro_audio_t)
                    ffmpeg_intro_audio_cmd = [
                    FFMPEG, "-y", "-nostdin", "-hide_banner",
                    "-i", intro_path,
                    "-vn",
                    "-c:a", encoder,
                    "-ar", str(sample_rate),
                    "-ac", str(channels)
                    ]
                    if bitrate:
                        ffmpeg_intro_audio_cmd.extend(["-b:a", str(bitrate)])
                    ffmpeg_intro_audio_cmd.extend([
                    "-t", f"{shortened_sec:.6f}",
                    temp_intro_audio_t
                    ])
                    try:
                        run_command(ffmpeg_intro_audio_cmd)
                        transcode_ok = True
                    except Exception:
                        # Fallback au silence de la même durée et format
                        try:
                            cl_name = "5.1" if channels == 6 else "7.1" if channels == 8 else "stereo" if channels == 2 else "mono"
                            ffmpeg_silence_cmd = [
                                FFMPEG, "-y", "-nostdin", "-hide_banner",
                                "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={cl_name}",
                                "-t", f"{shortened_sec:.6f}",
                                "-c:a", encoder
                            ]
                            if bitrate:
                                ffmpeg_silence_cmd.extend(["-b:a", str(bitrate)])
                            ffmpeg_silence_cmd.append(temp_intro_audio_t)
                            run_command(ffmpeg_silence_cmd)
                            transcode_ok = True
                        except Exception as e:
                            print(f"      [Warning] Impossible d'adapter l'audio d'intro pour la piste {t_id} : {e}")
                
                # 1b. Extraire la premiere trame du film si Atmos.
                extract_frame_ok = False
                if transcode_ok and is_atmos and intro_path:
                    try:
                        print("      -> Extraction de la première trame Atmos du film pour injection en en-tête (32 ms)...")
                        run_command([
                            FFMPEG, "-y", "-nostdin", "-hide_banner",
                            "-i", main_path,
                            "-map", f"0:a:{s_idx}",
                            "-ss", "00:00:00", "-t", "0.032",
                            "-c:a", "copy", temp_atmos_frame_t
                        ])
                        extract_frame_ok = True
                    except Exception as e:
                        print(f"      [Warning] Échec de l'extraction de la première trame Atmos : {e}")

                # 2. Extraction de la piste du film principal et concaténation
                if transcode_ok:
                    try:
                        run_command([
                            FFMPEG, "-y", "-nostdin", "-hide_banner",
                            "-i", main_path,
                            "-map", f"0:a:{s_idx}",
                            "-c:a", "copy",
                            "-vn", temp_main_audio_t
                        ])
                        
                        list_audio_file = tempfile.NamedTemporaryFile(suffix=".txt", mode="w", delete=False, encoding="utf-8", dir=temp_dir)
                        
                        # Génération de l'audio d'outro avant la liste de concaténation.
                        if outro_path:
                            temp_outro_audio_t = tempfile.NamedTemporaryFile(suffix=".mka", delete=False, dir=temp_dir).name
                            temp_files.append(temp_outro_audio_t)
                            outro_audio_cmd = [
                                FFMPEG, "-y", "-nostdin", "-hide_banner",
                                "-i", outro_path, "-vn", "-c:a", encoder,
                                "-ar", str(sample_rate), "-ac", str(channels)
                            ]
                            if bitrate:
                                outro_audio_cmd.extend(["-b:a", str(bitrate)])
                            outro_audio_cmd.extend(["-t", f"{outro_duration_sec:.6f}", temp_outro_audio_t])
                            try:
                                run_command(outro_audio_cmd)
                            except Exception:
                                cl_name = "5.1" if channels == 6 else "7.1" if channels == 8 else "stereo" if channels == 2 else "mono"
                                silence_outro_cmd = [
                                    FFMPEG, "-y", "-nostdin", "-hide_banner",
                                    "-f", "lavfi", "-i", f"anullsrc=r={sample_rate}:cl={cl_name}",
                                    "-t", f"{outro_duration_sec:.6f}", "-c:a", encoder
                                ]
                                if bitrate:
                                    silence_outro_cmd.extend(["-b:a", str(bitrate)])
                                silence_outro_cmd.append(temp_outro_audio_t)
                                run_command(silence_outro_cmd)

                        # Génération de la liste de concaténation
                        main_audio_path = os.path.abspath(temp_main_audio_t).replace(os.sep, "/")
                        if is_atmos and extract_frame_ok:
                            first_frame_path = os.path.abspath(temp_atmos_frame_t).replace(os.sep, "/")
                            list_audio_file.write(f"file '{first_frame_path}'\n")
                        if temp_intro_audio_t:
                            intro_audio_path = os.path.abspath(temp_intro_audio_t).replace(os.sep, "/")
                            list_audio_file.write(f"file '{intro_audio_path}'\n")
                        list_audio_file.write(f"file '{main_audio_path}'\n")
                        if temp_outro_audio_t:
                            outro_audio_path = os.path.abspath(temp_outro_audio_t).replace(os.sep, "/")
                            list_audio_file.write(f"file '{outro_audio_path}'\n")
                            
                        list_audio_file.close()
                        temp_files.append(list_audio_file.name)
                        
                        run_command([
                            FFMPEG, "-y", "-nostdin", "-hide_banner",
                            "-f", "concat", "-safe", "0",
                            "-i", list_audio_file.name,
                            "-c", "copy",
                            temp_concat_audio_t
                        ])
                        
                        concatenated_audio_tracks.append({
                            "track_id": t_id,
                            "file": temp_concat_audio_t,
                            "name": name,
                            "language": track.get("language", "und"),
                            "default_track": track.get("default_track", False),
                            "forced_track": track.get("forced_track", False)
                        })
                        
                        # Suppression des fichiers intermédiaires
                        paths_to_delete = [temp_main_audio_t, list_audio_file.name]
                        if temp_intro_audio_t:
                            paths_to_delete.append(temp_intro_audio_t)
                        if temp_outro_audio_t:
                            paths_to_delete.append(temp_outro_audio_t)
                        if temp_atmos_frame_t:
                            paths_to_delete.append(temp_atmos_frame_t)
                        for path in paths_to_delete:
                            if os.path.exists(path):
                                os.remove(path)
                                if path in temp_files:
                                    temp_files.remove(path)
                    except Exception as e:
                        print(f"      [Warning] Échec de la concaténation de la piste {t_id}, elle sera décalée : {e}")
                        delayed_track_ids.append(t_id)
            else:
                print(f"   -> Piste audio {t_id} ({name}, codec: {codec}) non gérée en concaténation directe, elle sera décalée.")
                delayed_track_ids.append(t_id)

        # Suppression anticipée de l'intro encodée intermédiaire car la vidéo en a été extraite
        if temp_intro_matched and os.path.exists(temp_intro_matched):
            try:
                os.remove(temp_intro_matched)
                if temp_intro_matched in temp_files:
                    temp_files.remove(temp_intro_matched)
            except OSError:
                pass

        # --- Injection des métadonnées HDR dynamiques (Dolby Vision / HDR10+) ---
        if video_codec == "hevc" and ((has_dovi and is_tool_available(DOVI_TOOL)) or (has_hdr10plus and is_tool_available(HDR10PLUS_TOOL))):
            print("\n[4b/5] Injection des métadonnées HDR dynamiques...")
            
            # 1. Dolby Vision
            if has_dovi and is_tool_available(DOVI_TOOL):
                print("   -> Dolby Vision détecté. Préparation et injection du RPU...")
                temp_combined_rpu = tempfile.NamedTemporaryFile(suffix=".rpu", delete=False, dir=temp_dir).name
                temp_injected_hevc = tempfile.NamedTemporaryFile(suffix=".hevc", delete=False, dir=temp_dir).name
                temp_files.extend([temp_combined_rpu, temp_injected_hevc])
                
                with open(temp_main_rpu, "rb") as f:
                    rpu_bytes = f.read()
                nals = split_rpu_nal_units(rpu_bytes)
                if nals:
                    first_nal = nals[0]
                    intro_rpu = first_nal * intro_frames
                    with open(temp_combined_rpu, "wb") as f_out:
                        f_out.write(intro_rpu)
                        f_out.write(rpu_bytes)
                        f_out.write(first_nal * outro_frames)
                        
                    # Supprimer le RPU principal extrait
                    if os.path.exists(temp_main_rpu):
                        try:
                            os.remove(temp_main_rpu)
                            if temp_main_rpu in temp_files:
                                temp_files.remove(temp_main_rpu)
                        except OSError:
                            pass
                            
                    inject_rpu_cmd = [
                        DOVI_TOOL, "inject-rpu",
                        "-i", working_hevc,
                        "-r", temp_combined_rpu,
                        "-o", temp_injected_hevc
                    ]
                    run_command(inject_rpu_cmd)
                    
                    # Supprimer le combined RPU et le working_hevc précédent pour économiser de la place
                    for path in [temp_combined_rpu, working_hevc]:
                        if os.path.exists(path):
                            try:
                                os.remove(path)
                                if path in temp_files:
                                    temp_files.remove(path)
                            except OSError:
                                pass
                                
                    working_hevc = temp_injected_hevc
                    print("   -> Métadonnées RPU Dolby Vision injectées avec succès.")
                else:
                    print("   -> Erreur : Aucun RPU valide n'a pu être extrait.")
                    
            # 2. HDR10+
            if has_hdr10plus and is_tool_available(HDR10PLUS_TOOL):
                print("   -> HDR10+ détecté. Préparation et injection des métadonnées...")
                temp_combined_json = tempfile.NamedTemporaryFile(suffix=".json", delete=False, dir=temp_dir).name
                temp_injected_hevc = tempfile.NamedTemporaryFile(suffix=".hevc", delete=False, dir=temp_dir).name
                temp_files.extend([temp_combined_json, temp_injected_hevc])
                
                with open(temp_main_json, "r", encoding="utf-8") as f:
                    hdr_data = json.load(f)
                scene_info = hdr_data.get("SceneInfo", [])
                if scene_info:
                    first_frame = scene_info[0]
                    new_frames = []
                    for idx in range(intro_frames):
                        new_frame = first_frame.copy()
                        new_frame["SequenceFrameIndex"] = idx
                        new_frame["SceneId"] = 0
                        new_frame["SceneFrameIndex"] = idx
                        new_frames.append(new_frame)
                        
                    for frame in scene_info:
                        frame["SequenceFrameIndex"] += intro_frames
                        
                    outro_frames_info = []
                    next_index = max((frame.get("SequenceFrameIndex", -1) for frame in scene_info), default=-1) + 1
                    for idx in range(outro_frames):
                        new_frame = first_frame.copy()
                        new_frame["SequenceFrameIndex"] = next_index + idx
                        new_frame["SceneId"] = 0
                        new_frame["SceneFrameIndex"] = idx
                        outro_frames_info.append(new_frame)

                    hdr_data["SceneInfo"] = new_frames + scene_info + outro_frames_info
                    with open(temp_combined_json, "w", encoding="utf-8") as f_out:
                        json.dump(hdr_data, f_out, indent=2)
                        
                    # Supprimer le json principal extrait
                    if os.path.exists(temp_main_json):
                        try:
                            os.remove(temp_main_json)
                            if temp_main_json in temp_files:
                                temp_files.remove(temp_main_json)
                        except OSError:
                            pass
                            
                    inject_hdr10p_cmd = [
                        HDR10PLUS_TOOL, "inject",
                        "-i", working_hevc,
                        "-j", temp_combined_json,
                        "-o", temp_injected_hevc
                    ]
                    run_command(inject_hdr10p_cmd)
                    
                    # Supprimer le combined json et le working_hevc précédent pour économiser de la place
                    for path in [temp_combined_json, working_hevc]:
                        if os.path.exists(path):
                            try:
                                os.remove(path)
                                if path in temp_files:
                                    temp_files.remove(path)
                            except OSError:
                                pass
                                
                    working_hevc = temp_injected_hevc
                    print("   -> Métadonnées HDR10+ injectées avec succès.")
                else:
                    print("   -> Erreur : Aucun JSON HDR10+ valide n'a pu être extrait.")
                    
        # Étape 5 : Réassembler la vidéo        # Déterminer si on utilise Muxiveo ou mkvmerge pour le réassemblage final
        use_muxiveo = MUXIVEO_PATH is not None and is_tool_available(MUXIVEO)
        
        if use_muxiveo:
            print("   -> Utilisation de Muxiveo pour le réassemblage final (génération de la configuration exact-job)...")

            # Muxiveo conserve implicitement certaines pistes d'une source.
            # Fournir une copie sans sous-titres évite de réintroduire les
            # timestamps d'origine à côté des pistes recalculées.
            temp_main_without_subtitles = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False, dir=temp_dir).name
            temp_files.append(temp_main_without_subtitles)
            run_command([
                FFMPEG, "-y", "-nostdin", "-hide_banner",
                "-i", main_path, "-map", "0", "-map", "-0:s", "-c", "copy",
                temp_main_without_subtitles
            ])
            
            # 0. Muxer d'abord la vidéo préparée dans un fichier MKV temporaire.
            temp_video_mkv = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False, dir=temp_dir).name
            temp_files.append(temp_video_mkv)
            
            if mkvmerge_path:
                video_mux_cmd = [
                    mkvmerge_path, "-o", temp_video_mkv
                ]
                if video_meta.get("fps"):
                    video_mux_cmd.extend(["--default-duration", f"0:{video_meta['fps']}fps"])
                    
                # Injecter les métadonnées HDR10 statiques
                if video_meta.get("mastering_primaries") == "Display P3":
                    video_mux_cmd.extend([
                        "--chromaticity-coordinates", "0:0.68,0.32,0.265,0.690,0.15,0.06",
                        "--white-colour-coordinates", "0:0.3127,0.3290"
                    ])
                elif video_meta.get("mastering_primaries") == "BT.2020":
                    video_mux_cmd.extend([
                        "--chromaticity-coordinates", "0:0.708,0.292,0.170,0.797,0.131,0.046",
                        "--white-colour-coordinates", "0:0.3127,0.3290"
                    ])
                    
                if video_meta.get("mastering_min_lum") is not None:
                    video_mux_cmd.extend(["--min-luminance", f"0:{video_meta['mastering_min_lum']}"])
                if video_meta.get("mastering_max_lum") is not None:
                    video_mux_cmd.extend(["--max-luminance", f"0:{video_meta['mastering_max_lum']}"])
                if video_meta.get("max_cll") is not None:
                    video_mux_cmd.extend(["--max-content-light", f"0:{video_meta['max_cll']}"])
                if video_meta.get("max_fall") is not None:
                    video_mux_cmd.extend(["--max-frame-light", f"0:{video_meta['max_fall']}"])
                    
                video_mux_cmd.append(working_hevc)
                print("   -> Muxing de la piste vidéo préparée...")
                run_command(video_mux_cmd)
            else:
                # Fallback ffmpeg
                print("   -> mkvmerge non trouvé pour la vidéo temporaire, utilisation de FFmpeg...")
                video_mux_fallback_cmd = [
                    FFMPEG, "-y", "-nostdin", "-hide_banner",
                ]
                if working_video_is_raw and video_meta.get("fps"):
                    video_mux_fallback_cmd.extend(["-r", video_meta["fps"]])
                video_mux_fallback_cmd.extend([
                    "-i", working_hevc,
                    "-c:v", "copy",
                    temp_video_mkv
                ])
                run_command(video_mux_fallback_cmd)
            
            # Calcul du décalage (delay) de l'intro en millisecondes pour les autres pistes
            delay_ms = intro_duration_ms
            print(f"   -> Décalage calculé pour les pistes d'origine : {delay_ms} ms (intro de {intro_frames} images)")
            
            # 1. Construire la liste des sources
            sources = []
            # Source 0 : La vidéo temporaire
            sources.append({
                "path": os.path.abspath(temp_video_mkv),
                "attachments": "none",
                "copy_tags": False
            })
            
            # Source 1..N : Les pistes audio concaténées
            for track_info in concatenated_audio_tracks:
                sources.append({
                    "path": os.path.abspath(track_info["file"]),
                    "attachments": "none",
                    "copy_tags": False
                })
                
            # Source N+1 : Le film principal d'origine
            main_movie_source_index = len(sources)
            sources.append({
                "path": os.path.abspath(temp_main_without_subtitles),
                "attachments": "all",
                "copy_tags": True
            })

            rewritten_subtitle_source_indices = []
            for subtitle_file in rewritten_subtitle_files:
                rewritten_subtitle_source_indices.append(len(sources))
                sources.append({
                    "path": os.path.abspath(subtitle_file),
                    "attachments": "none",
                    "copy_tags": False
                })
            
            # 2. Construire la liste des pistes
            tracks = []
            
            # Piste vidéo (depuis source 0)
            tracks.append({
                "selector": {"source": 0, "type": "video", "position": 0},
                "enabled": True
            })
            
            # Pistes audio concaténées
            for idx, track_info in enumerate(concatenated_audio_tracks):
                tracks.append({
                    "selector": {"source": 1 + idx, "type": "audio", "position": 0},
                    "enabled": True,
                    "title": track_info["name"],
                    "language": track_info["language"],
                    "default": track_info["default_track"],
                    "forced": track_info["forced_track"]
                })
                
            # Pistes audio d'origine (de la source d'origine) :
            # On active et décale celles qui n'ont pas été concaténées (delayed_track_ids)
            # et on désactive strictement celles qui ont été concaténées !
            if audio_tracks:
                for track in audio_tracks:
                    is_delayed = track["track_id"] in delayed_track_ids
                    tracks.append({
                        "selector": {"source": main_movie_source_index, "type": "audio", "position": track["stream_idx"]},
                        "enabled": is_delayed,
                        "time_shift_ms": delay_ms if is_delayed else 0,
                        "title": track["name"],
                        "language": track["language"],
                        "default": track["default_track"],
                        "forced": track["forced_track"]
                    })
                    
            # Piste vidéo d'origine (de la source d'origine) : STRICTEMENT désactivée
            tracks.append({
                "selector": {"source": main_movie_source_index, "type": "video", "position": 0},
                "enabled": False
            })
            
            # Les timestamps sont réécrits dans des sources temporaires : aucun
            # offset n'est laissé à appliquer par le muxeur final.
            for source_index in rewritten_subtitle_source_indices:
                tracks.append({
                    "selector": {"source": source_index, "type": "subtitle", "position": 0},
                    "enabled": True
                })
                
            # 3. Construire le fichier job JSON de Muxiveo
            job_data = {
                "version": 1,
                "kind": "exact-job",
                "sources": sources,
                "output": os.path.abspath(output_path),
                "tracks": tracks,
                "chapters": ({
                    "source_index": main_movie_source_index,
                    "include_source": False,
                    "add": build_shifted_chapters(source_chapters, delay_ms)
                } if source_chapters else False)
            }
            
            # Écriture du fichier exact-job.json temporaire
            job_file = tempfile.NamedTemporaryFile(suffix=".exact-job.json", mode="w", delete=False, encoding="utf-8", dir=temp_dir)
            json.dump(job_data, job_file, indent=2, ensure_ascii=False)
            job_file.close()
            temp_files.append(job_file.name)
            
            print(f"   -> Configuration exact-job écrite dans : {job_file.name}")
            
            # Exécuter la commande Muxiveo
            muxiveo_cmd = [
                MUXIVEO, "--cli", "run",
                "--config", job_file.name,
                "--force"
            ]
            
            # Transmettre les chemins des outils si définis
            if FFMPEG_PATH:
                muxiveo_cmd.extend(["--ffmpeg", FFMPEG])
            if FFPROBE_PATH:
                muxiveo_cmd.extend(["--ffprobe", FFPROBE])
            if MEDIAINFO_PATH:
                muxiveo_cmd.extend(["--mediainfo", MEDIAINFO])
            if temp_dir:
                muxiveo_cmd.extend(["--work-dir", temp_dir])
                
            # Lancer Muxiveo
            run_command(muxiveo_cmd)
            
        elif mkvmerge_path:
            print("   -> Utilisation de mkvmerge pour conserver toutes les pistes d'origine (audio, sous-titres, chapitres, pièces jointes)...")
            
            # Calcul du décalage (delay) de l'intro en millisecondes pour synchroniser les autres pistes
            delay_ms = intro_duration_ms
            print(f"   -> Décalage calculé pour les pistes d'origine : {delay_ms} ms (intro de {intro_frames} images)")
            
            final_mux_cmd = [
                mkvmerge_path, "-o", output_path
            ]
            if video_meta.get("fps"):
                final_mux_cmd.extend(["--default-duration", f"0:{video_meta['fps']}fps"])
                
            # Injecter les métadonnées HDR10 statiques de la vidéo d'origine (Mastering Display et CLL)
            # sur la piste vidéo finale (Track ID 0 dans final_mux_cmd qui correspond à working_hevc)
            if video_meta.get("mastering_primaries") == "Display P3":
                final_mux_cmd.extend([
                    "--chromaticity-coordinates", "0:0.68,0.32,0.265,0.690,0.15,0.06",
                    "--white-colour-coordinates", "0:0.3127,0.3290"
                ])
            elif video_meta.get("mastering_primaries") == "BT.2020":
                final_mux_cmd.extend([
                    "--chromaticity-coordinates", "0:0.708,0.292,0.170,0.797,0.131,0.046",
                    "--white-colour-coordinates", "0:0.3127,0.3290"
                ])
                
            if video_meta.get("mastering_min_lum") is not None:
                final_mux_cmd.extend(["--min-luminance", f"0:{video_meta['mastering_min_lum']}"])
            if video_meta.get("mastering_max_lum") is not None:
                final_mux_cmd.extend(["--max-luminance", f"0:{video_meta['mastering_max_lum']}"])
            if video_meta.get("max_cll") is not None:
                final_mux_cmd.extend(["--max-content-light", f"0:{video_meta['max_cll']}"])
            if video_meta.get("max_fall") is not None:
                final_mux_cmd.extend(["--max-frame-light", f"0:{video_meta['max_fall']}"])
                
            # 1. Ajouter la vidéo concaténée, avec métadonnées injectées si nécessaire
            final_mux_cmd.append(working_hevc)
            
            # 2. Ajouter toutes les pistes audio concaténées avec leurs métadonnées d'origine
            for track_info in concatenated_audio_tracks:
                final_mux_cmd.extend([
                    "--track-name", f"0:{track_info['name']}",
                    "--language", f"0:{track_info['language']}",
                    "--default-track-flag", f"0:{'yes' if track_info['default_track'] else 'no'}",
                    "--forced-display-flag", f"0:{'yes' if track_info['forced_track'] else 'no'}"
                ])
                final_mux_cmd.append(track_info["file"])

            # Les sous-titres déjà remuxés portent leurs nouveaux timestamps.
            final_mux_cmd.extend(rewritten_subtitle_files)
                
            # 3. Ajouter la vidéo principale avec toutes ses pistes annexes décalées, en excluant sa vidéo d'origine et ses pistes audio concaténées
            main_movie_opts = []
            
            # Désactiver la vidéo d'origine du film
            main_movie_opts.extend(["--no-video", "--no-subtitles", "--no-chapters"])
            
            # Gérer la sélection des pistes audio de la vidéo d'origine
            # Si toutes les pistes audio ont été traitées par concaténation, on désactive l'audio de la source d'origine
            # Sinon, on spécifie explicitement la liste des pistes audio non traitées (delayed_track_ids) pour que mkvmerge ne conserve que celles-là.
            if delayed_track_ids:
                keep_audio_str = ",".join(str(tid) for tid in delayed_track_ids)
                main_movie_opts.extend(["-a", keep_audio_str])
            else:
                main_movie_opts.append("--no-audio")
                
            # Les seules pistes de la source encore décalées sont les audios
            # non concaténables. Sous-titres et chapitres ont été réécrits.
            for tid in delayed_track_ids:
                main_movie_opts.extend(["--sync", f"{tid}:{delay_ms}"])

            if source_chapters:
                chapter_file = write_shifted_matroska_chapters(source_chapters, intro_duration_ms, temp_dir, temp_files)
                final_mux_cmd.extend(["--chapters", chapter_file])
            
            # Ajouter les options de la vidéo d'origine puis le chemin
            final_mux_cmd.extend(main_movie_opts)
            final_mux_cmd.append(main_path)
            
            run_command(final_mux_cmd)
        else:
            print("   -> mkvmerge non trouvé. Fallback sur FFmpeg (attention: Dolby Vision conteneur peut manquer)...")
            temp_video_container = tempfile.NamedTemporaryFile(suffix=".mkv", delete=False, dir=temp_dir).name
            temp_files.append(temp_video_container)
            
            gen_ts_cmd = [
                FFMPEG, "-y", "-nostdin", "-hide_banner",
            ]
            if working_video_is_raw:
                gen_ts_cmd.extend(["-r", video_meta["fps"]])
            gen_ts_cmd.extend(["-i", working_hevc, "-map", "0:v:0", "-c:v", "copy", temp_video_container])
            run_command(gen_ts_cmd)
            
            # Supprimer le working_hevc intermédiaire
            if os.path.exists(working_hevc):
                try:
                    os.remove(working_hevc)
                    if working_hevc in temp_files:
                        temp_files.remove(working_hevc)
                except OSError:
                    pass
            
            final_mux_cmd = [
                FFMPEG, "-y", "-nostdin", "-hide_banner",
                "-i", temp_video_container,
            ]
            for track_info in concatenated_audio_tracks:
                final_mux_cmd.extend(["-i", track_info["file"]])
            subtitle_input_start = len(concatenated_audio_tracks) + 1
            for subtitle_file in rewritten_subtitle_files:
                final_mux_cmd.extend(["-i", subtitle_file])
            chapter_input_index = None
            if source_chapters:
                chapter_file = write_shifted_ffmetadata(source_chapters, intro_duration_ms, temp_dir, temp_files)
                chapter_input_index = subtitle_input_start + len(rewritten_subtitle_files)
                final_mux_cmd.extend(["-i", chapter_file])
            final_mux_cmd.extend(["-map", "0:v:0"])
            for input_index in range(1, len(concatenated_audio_tracks) + 1):
                final_mux_cmd.extend(["-map", f"{input_index}:a:0"])
            for input_index in range(subtitle_input_start, subtitle_input_start + len(rewritten_subtitle_files)):
                final_mux_cmd.extend(["-map", f"{input_index}:s:0"])
            if chapter_input_index is not None:
                final_mux_cmd.extend(["-map_chapters", str(chapter_input_index)])
                
            final_mux_cmd.extend([
                "-c", "copy",
            ])
            if video_meta.get("dar"):
                final_mux_cmd.extend(["-aspect", video_meta["dar"]])
                
            final_mux_cmd.append(output_path)
            run_command(final_mux_cmd)
            
        print(f"\n[Succès !] Fichier final généré : {output_path}")
        return True
        
    except subprocess.CalledProcessError as e:
        print(f"\n[Erreur] Échec lors d'une commande FFmpeg/outil : {e}")
        return False
    except ValueError as e:
        print(f"\n[Erreur] Flux ou métadonnées invalides : {e}")
        return False
    finally:
        # Nettoyage des fichiers temporaires
        print("Nettoyage des fichiers temporaires...")
        for temp_f in temp_files:
            if os.path.exists(temp_f):
                try:
                    os.remove(temp_f)
                except OSError:
                    pass

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Ajoute une intro et/ou une outro à une vidéo.")
    parser.add_argument("paths", nargs="+", help="Syntaxe : main output --intro INTRO --outro OUTRO")
    parser.add_argument("--intro", help="Chemin de la vidéo à ajouter au début")
    parser.add_argument("--outro", help="Chemin de la vidéo à ajouter à la fin")
    parser.add_argument("-w", "--workdir", help="Dossier de travail temporaire (par défaut: dossier de sortie sous Linux, %%temp%% sous Windows)")
    parser.add_argument("-m", "--mode", choices=["crop", "pad"], default="crop", help="Mode d'adaptation de l'intro : crop (recadrer en coupant) ou pad (bandes noires). Par défaut : crop.")
    
    args = parser.parse_args()

    if len(args.paths) == 2:
        main_path, output_path = args.paths
        intro_path = args.intro
    elif len(args.paths) == 3 and not args.intro and not args.outro:
        # Compatibilité avec l'ancienne syntaxe : intro main output.
        intro_path, main_path, output_path = args.paths
        print("[Warning] Syntaxe historique détectée. Préférez : main output --intro INTRO")
    else:
        parser.error("utilisez : main output [--intro INTRO] [--outro OUTRO]")

    if not intro_path and not args.outro:
        parser.error("au moins une option --intro ou --outro est requise")

    concat_videos(main_path, output_path, intro_path=intro_path, outro_path=args.outro, workdir=args.workdir, mode=args.mode)
