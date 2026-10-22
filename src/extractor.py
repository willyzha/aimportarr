import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field

logger = logging.getLogger("aimportarr.extractor")

ARCHIVE_EXTENSIONS = {".7z", ".zip", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".iso"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv"}
SUSPICIOUS_EXTENSIONS = {".exe", ".scr", ".bat", ".cmd", ".vbs", ".msi", ".pif", ".com", ".crypted"}

def resolve_local_path(remote_path: str, path_mappings: Optional[List[Any]] = None) -> Path:
    """
    Resolve a remote path reported by Sonarr/Radarr API to a local path inside AImportarr container.
    1. Checks configured path_mappings (e.g. from /remote/downloads to /data/downloads).
    2. Checks direct 1:1 path match.
    3. Auto-discovers against common mount points (/data/downloads, /downloads, /data).
    """
    if not remote_path:
        return Path("")

    if path_mappings:
        for mapping in path_mappings:
            from_p = getattr(mapping, "from_path", None) or (mapping.get("from") if isinstance(mapping, dict) else None)
            to_p = getattr(mapping, "to_path", None) or (mapping.get("to") if isinstance(mapping, dict) else None)
            if from_p and to_p and remote_path.startswith(from_p):
                translated = remote_path.replace(from_p, to_p, 1)
                p_trans = Path(translated)
                if p_trans.exists():
                    return p_trans

    p = Path(remote_path)
    if p.exists():
        return p

    # Auto-discovery fallback: check standard container mount points for the same file/folder name
    file_name = p.name
    for common_dir in ["/data/downloads", "/downloads", "/data"]:
        candidate = Path(common_dir) / file_name
        if candidate.exists():
            logger.debug(f"Auto-discovered local path '{candidate}' for remote path '{remote_path}'")
            return candidate

    return p

class ArchiveFileInfo(BaseModel):
    path: str
    size: int = 0
    is_dir: bool = False

class ArchiveInspectionResult(BaseModel):
    archive_path: Path
    files: List[ArchiveFileInfo]
    video_files: List[ArchiveFileInfo]
    suspicious_files: List[ArchiveFileInfo]
    has_suspicious_files: bool
    has_video_files: bool
    total_uncompressed_size: int

def find_archive_file(path: Path) -> Optional[Path]:
    """
    Locate an archive file given a path (either pointing directly to an archive file or a directory).
    For multi-part archives (e.g. .part01.rar), prioritizes the first volume.
    """
    try:
        if not path.exists():
            return None

        if path.is_file():
            if path.suffix.lower() in ARCHIVE_EXTENSIONS:
                return path
            return None

        if path.is_dir():
            # Check for multi-part scene RAR archives first (e.g. *.part01.rar, *.part1.rar)
            for ext in (".part01.rar", ".part1.rar", ".part001.rar", ".part01.7z"):
                for f in path.rglob(f"*{ext}"):
                    if f.is_file():
                        return f

            # Search for any archive files
            for f in path.rglob("*"):
                if f.is_file() and f.suffix.lower() in ARCHIVE_EXTENSIONS:
                    # Ignore secondary parts of multi-part archives like .part02.rar, .r01, etc.
                    stem_lower = f.stem.lower()
                    if any(stem_lower.endswith(f".part{n:02d}") for n in range(2, 100)):
                        continue
                    if any(stem_lower.endswith(f".part{n}") for n in range(2, 100)):
                        continue
                    return f
    except Exception as e:
        logger.debug(f"Error scanning for archives in '{path}': {e}")

    return None

async def inspect_archive(archive_path: Path) -> Optional[ArchiveInspectionResult]:
    """
    Inspect the file table/manifest of an archive in memory using 7-Zip WITHOUT extracting anything to disk.
    Parses file names, uncompressed sizes, and detects any suspicious executables or video files.
    """
    seven_z = shutil.which("7z") or shutil.which("7za")
    if not seven_z:
        logger.warning("7z utility not found in system PATH. Cannot inspect archive.")
        return None

    try:
        proc = await asyncio.create_subprocess_exec(
            seven_z, "l", "-slt", str(archive_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"Failed to list archive '{archive_path}': {stderr.decode(errors='replace')}")
            return None

        lines = stdout.decode("utf-8", errors="replace").splitlines()
        entries: List[ArchiveFileInfo] = []
        current_entry: Dict[str, str] = {}

        for line in lines:
            line = line.strip()
            if not line:
                if "Path" in current_entry and current_entry["Path"] != str(archive_path):
                    path_val = current_entry.get("Path", "")
                    size_val = int(current_entry.get("Size", 0)) if current_entry.get("Size", "0").isdigit() else 0
                    is_dir = current_entry.get("Folder", "") == "+" or current_entry.get("Attributes", "").startswith("D")
                    entries.append(ArchiveFileInfo(path=path_val, size=size_val, is_dir=is_dir))
                current_entry = {}
                continue

            if "=" in line:
                key, _, val = line.partition("=")
                current_entry[key.strip()] = val.strip()

        if "Path" in current_entry and current_entry["Path"] != str(archive_path):
            path_val = current_entry.get("Path", "")
            size_val = int(current_entry.get("Size", 0)) if current_entry.get("Size", "0").isdigit() else 0
            is_dir = current_entry.get("Folder", "") == "+" or current_entry.get("Attributes", "").startswith("D")
            entries.append(ArchiveFileInfo(path=path_val, size=size_val, is_dir=is_dir))

        video_files = [
            e for e in entries
            if not e.is_dir and Path(e.path).suffix.lower() in VIDEO_EXTENSIONS
        ]
        suspicious_files = [
            e for e in entries
            if not e.is_dir and (
                Path(e.path).suffix.lower() in SUSPICIOUS_EXTENSIONS
                or "_crypted" in e.path.lower()
            )
        ]

        return ArchiveInspectionResult(
            archive_path=archive_path,
            files=entries,
            video_files=video_files,
            suspicious_files=suspicious_files,
            has_suspicious_files=bool(suspicious_files),
            has_video_files=bool(video_files),
            total_uncompressed_size=sum(e.size for e in entries)
        )
    except Exception as e:
        logger.error(f"Error inspecting archive '{archive_path}': {e}")
        return None

async def extract_archive(
    archive_path: Path,
    target_dir: Path
) -> bool:
    """
    Safely extract an archive into target_dir with strict Zip-Slip path traversal protection.
    """
    seven_z = shutil.which("7z") or shutil.which("7za")
    if not seven_z:
        logger.error("7z utility not found in system PATH. Cannot extract archive.")
        return False

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_target = target_dir.resolve()

        proc = await asyncio.create_subprocess_exec(
            seven_z, "x", "-y", f"-o{str(target_dir)}", str(archive_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"Extraction failed for '{archive_path}': {stderr.decode(errors='replace')}")
            return False

        # Strict Zip-Slip path traversal verification: ensure no extracted file escapes target_dir
        for root, _, files in os.walk(target_dir):
            for f in files:
                full_path = (Path(root) / f).resolve()
                if not full_path.is_relative_to(resolved_target):
                    logger.critical(f"Zip-Slip path traversal detected! File '{full_path}' escapes target '{resolved_target}'. Removing extracted files.")
                    shutil.rmtree(target_dir, ignore_errors=True)
                    return False

        return True
    except Exception as e:
        logger.error(f"Error extracting archive '{archive_path}': {e}")
        shutil.rmtree(target_dir, ignore_errors=True)
        return False
