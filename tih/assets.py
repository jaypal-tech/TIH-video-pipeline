import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

SAFE_SUFFIX = re.compile(r"^\.[A-Za-z0-9]{1,8}$")


@dataclass(frozen=True)
class ShotRef:
    episode: int | None = None
    clip: int | None = None
    shot: int | str | None = None

    def parts(self) -> list[str]:
        parts = ["season" if self.episode is None else f"ep{self.episode:02d}"]
        if self.clip is not None:
            parts.append(f"clip{self.clip:02d}")
        if self.shot is not None:
            parts.append("shot" + re.sub(r"[^A-Za-z0-9_-]", "_", str(self.shot)))
        return parts


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def save_asset(url: str, ctx: Any, shot_ref: ShotRef) -> Path:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        raise ValueError("Assets can only be downloaded over https")
    directory = ctx.paths.root / ctx.paths.series / "assets"
    directory = directory.joinpath(*shot_ref.parts())
    directory.mkdir(parents=True, exist_ok=True)
    suffix = PurePosixPath(parsed.path).suffix
    suffix = suffix if SAFE_SUFFIX.match(suffix) else ""
    temp = directory / f"{uuid.uuid4().hex}.tmp"
    try:
        ctx.transport.download(url, temp)
        final = directory / f"{_sha256(temp)}{suffix}"
        os.replace(temp, final)
    finally:
        temp.unlink(missing_ok=True)
    return final
