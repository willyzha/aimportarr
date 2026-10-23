import asyncio
import logging
import os
import shutil
from pathlib import Path
from typing import List, Optional, Dict, Any
from pydantic import BaseModel

logger = logging.getLogger("aimportarr.extractor")

ARCHIVE_EXTENSIONS = {".7z", ".zip", ".rar", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".iso"}
VIDEO_EXTENSIONS = {".mkv", ".mp4", ".avi", ".ts", ".m4v", ".mov", ".wmv"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".sub", ".idx", ".vtt", ".sup"}
ALLOWED_EXTRACT_EXTENSIONS = VIDEO_EXTENSIONS | SUBTITLE_EXTENSIONS
SUSPICIOUS_EXTENSIONS = {".exe", ".scr", ".bat", ".cmd", ".vbs", ".msi", ".ps1", ".lnk", ".pif", ".com", ".crypted"}


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
    common_mounts = [
        "/data/downloads",
        "/downloads",
        "/data",
        "/media/downloads",
        "/media",
        "/mnt/user/Media/downloads",
        "/mnt/user/downloads",
    ]
    for common_dir in common_mounts:
        p_common = Path(common_dir)
        if not p_common.exists():
            continue

        # 1. Direct match: common_dir / file_name
        candidate = p_common / file_name
        if candidate.exists():
            logger.info(f"Auto-discovered local path '{candidate}' for remote path '{remote_path}'")
            return candidate

        # 2. Category subfolder match (e.g. tv-sonarr/, radarr/, complete/)
        if p_common.is_dir():
            try:
                for sub in p_common.iterdir():
                    if sub.is_dir():
                        sub_candidate = sub / file_name
                        if sub_candidate.exists():
                            logger.info(f"Auto-discovered local path in subfolder '{sub_candidate}' for remote path '{remote_path}'")
                            return sub_candidate
            except Exception:
                pass

    return p


class ArchiveFileInfo(BaseModel):
    path: str
    size: int = 0
    is_dir: bool = False
    is_symlink: bool = False


class ArchiveInspectionResult(BaseModel):
    archive_path: Path
    files: List[ArchiveFileInfo]
    video_files: List[ArchiveFileInfo]
    suspicious_files: List[ArchiveFileInfo]
    has_suspicious_files: bool
    has_video_files: bool
    total_uncompressed_size: int


def _is_unsafe_entry_path(entry_path: str) -> bool:
    """Detect path traversal or absolute path entries inside an archive manifest."""
    normalized = entry_path.replace("\\", "/")
    if normalized.startswith("/") or ":" in normalized:
        return True
    parts = [p for p in normalized.split("/") if p]
    return ".." in parts


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


def _build_entry(current_entry: Dict[str, str]) -> ArchiveFileInfo:
    path_val = current_entry.get("Path", "")
    size_val = int(current_entry.get("Size", 0)) if current_entry.get("Size", "0").isdigit() else 0
    attrs = current_entry.get("Attributes", "")
    mode = current_entry.get("Mode", "")
    sym_link = current_entry.get("Symbolic Link", "")
    is_dir = current_entry.get("Folder", "") == "+" or attrs.startswith("D")
    is_symlink = bool(sym_link) or "l" in mode.lower() or "L" in attrs
    return ArchiveFileInfo(path=path_val, size=size_val, is_dir=is_dir, is_symlink=is_symlink)


async def inspect_archive(archive_path: Path) -> Optional[ArchiveInspectionResult]:
    """
    Inspect the file table/manifest of an archive in memory using 7-Zip without extracting to disk.
    Parses file names, uncompressed sizes, and detects any suspicious executables, symlinks, traversal paths, or video files.
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
                    entries.append(_build_entry(current_entry))
                current_entry = {}
                continue

            if "=" in line:
                key, _, val = line.partition("=")
                current_entry[key.strip()] = val.strip()

        if "Path" in current_entry and current_entry["Path"] != str(archive_path):
            entries.append(_build_entry(current_entry))

        video_files = [
            e for e in entries
            if not e.is_dir and not e.is_symlink and Path(e.path).suffix.lower() in VIDEO_EXTENSIONS
        ]
        suspicious_files = [
            e for e in entries
            if e.is_symlink
            or _is_unsafe_entry_path(e.path)
            or (
                not e.is_dir and (
                    Path(e.path).suffix.lower() in SUSPICIOUS_EXTENSIONS
                    or "_crypted" in e.path.lower()
                )
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
    Extract media files from an archive into a temporary staging directory.
    Validates archive entries, free disk space, and allowed media extensions before and after unpacking.
    """
    seven_z = shutil.which("7z") or shutil.which("7za")
    if not seven_z:
        logger.error("7z utility not found in system PATH. Cannot extract archive.")
        return False

    try:
        # 1. Pre-flight manifest validation (block traversal paths, symlinks, and disk exhaustion)
        inspection = await inspect_archive(archive_path)
        if inspection:
            for entry in inspection.files:
                if _is_unsafe_entry_path(entry.path) or entry.is_symlink:
                    logger.error(f"Refusing to extract '{archive_path.name}': unsafe path or symlink entry '{entry.path}'")
                    return False

            parent_dir = target_dir.parent if target_dir.parent.exists() else archive_path.parent
            try:
                free_bytes = shutil.disk_usage(parent_dir).free
                # Require at least 256MB buffer above total uncompressed video size
                needed_bytes = sum(v.size for v in inspection.video_files) or inspection.total_uncompressed_size
                if needed_bytes > 0 and needed_bytes + (256 * 1024 * 1024) > free_bytes:
                    logger.error(
                        f"Refusing to extract '{archive_path.name}': uncompressed size ({needed_bytes} bytes) exceeds available disk space ({free_bytes} bytes)."
                    )
                    return False
            except Exception:
                pass

        target_dir.mkdir(parents=True, exist_ok=True)
        resolved_target = target_dir.resolve()

        # 2. Extract only allowed video and subtitle extensions
        include_args = [f"-ir!*{ext}" for ext in sorted(ALLOWED_EXTRACT_EXTENSIONS)]
        proc = await asyncio.create_subprocess_exec(
            seven_z, "x", "-y", f"-o{str(target_dir)}", str(archive_path), *include_args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            logger.error(f"Extraction failed for '{archive_path}': {stderr.decode(errors='replace')}")
            shutil.rmtree(target_dir, ignore_errors=True)
            return False

        # 3. Post-extraction verification: remove any unexpected symlinks/non-media files and strip execute permissions
        for root, dirs, files in os.walk(target_dir):
            root_path = Path(root)
            for d in dirs:
                dp = root_path / d
                if dp.is_symlink() or not dp.resolve().is_relative_to(resolved_target):
                    shutil.rmtree(target_dir, ignore_errors=True)
                    return False
                try:
                    os.chmod(dp, 0o755)
                except Exception:
                    pass
            for f in files:
                fp = root_path / f
                if fp.is_symlink():
                    fp.unlink(missing_ok=True)
                    continue
                full_path = fp.resolve()
                if not full_path.is_relative_to(resolved_target):
                    logger.error(f"Extracted file '{full_path}' escaped staging directory '{resolved_target}'. Cleaning up.")
                    shutil.rmtree(target_dir, ignore_errors=True)
                    return False
                if fp.suffix.lower() not in ALLOWED_EXTRACT_EXTENSIONS:
                    fp.unlink(missing_ok=True)
                    continue
                try:
                    os.chmod(fp, 0o644)
                except Exception:
                    pass

        return True
    except Exception as e:
        logger.error(f"Error extracting archive '{archive_path}': {e}")
        shutil.rmtree(target_dir, ignore_errors=True)
        return False
