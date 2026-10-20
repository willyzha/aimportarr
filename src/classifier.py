from enum import Enum
from typing import List, Optional

class ErrorCategory(str, Enum):
    ID_MATCH = "id_match"
    UNKNOWN_MEDIA = "unknown_media"
    UNKNOWN_EPISODE = "unknown_episode"
    SAMPLE_OR_EXTRA = "sample_or_extra"
    QUALITY_OR_LANGUAGE = "quality_or_language"
    OTHER = "other"  # Fallback / Catch-all category

def classify_issue(
    messages: List[str],
    has_media_info: bool = True,
    manual_rejections: Optional[List[str]] = None
) -> ErrorCategory:
    """
    Classify a Sonarr/Radarr queue warning or import failure into a configured ErrorCategory.
    Analyzes status messages from the queue and rejection reasons from /manualimport.
    """
    combined_texts = [m.lower() for m in messages]
    if manual_rejections:
        combined_texts.extend([r.lower() for r in manual_rejections])
    all_text = " ".join(combined_texts)

    # 1. ID Match guardrail (Release matched by ID, blocked from automatic import)
    if "matched to series by id" in all_text or "matched to movie by id" in all_text:
        return ErrorCategory.ID_MATCH

    # 2. Unknown Media (Unknown series or unknown movie)
    if not has_media_info or any(phrase in all_text for phrase in [
        "unknown series",
        "unknown movie",
        "no series was found",
        "no movie was found",
        "not matched to any series",
        "not matched to any movie"
    ]):
        return ErrorCategory.UNKNOWN_MEDIA

    # 3. Unknown Episode (Series is known, but episode could not be parsed)
    if any(phrase in all_text for phrase in [
        "unknown episode",
        "unable to parse episode",
        "failed to map episode",
        "episode was not found",
        "does not match any existing episode"
    ]):
        return ErrorCategory.UNKNOWN_EPISODE

    # 4. Sample or Extra (File flagged as a short video/sample)
    if any(phrase in all_text for phrase in [
        "sample file",
        "is a sample",
        "short video",
        "trailer",
        "extra detected"
    ]):
        return ErrorCategory.SAMPLE_OR_EXTRA

    # 5. Quality or Language (Rejected due to quality cutoff or language profile)
    if any(phrase in all_text for phrase in [
        "language not wanted",
        "quality not wanted",
        "custom format",
        "cutoff unmet",
        "higher quality",
        "equal or higher quality"
    ]):
        return ErrorCategory.QUALITY_OR_LANGUAGE

    # 6. Fallback / Catch-all for any other error or warning status
    return ErrorCategory.OTHER
