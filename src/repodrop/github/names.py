import re

_OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_URL_PREFIX_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)


def parse_repo(value: str) -> str | None:
    """Normalize `owner/name` or a github.com URL to `owner/name`; None if it isn't one."""
    value = value.strip()
    is_url = bool(_URL_PREFIX_RE.match(value))
    path = _URL_PREFIX_RE.sub("", value)
    path = re.split(r"[?#]", path, maxsplit=1)[0].strip("/")
    parts = path.split("/")
    # URLs may point deeper into the repo (e.g. /releases); bare input must be exactly owner/name.
    if len(parts) < 2 or (not is_url and len(parts) != 2):
        return None
    owner, name = parts[0], parts[1].removesuffix(".git")
    if not _OWNER_RE.match(owner) or not _NAME_RE.match(name) or name in {".", ".."}:
        return None
    return f"{owner}/{name}"
