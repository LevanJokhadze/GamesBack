"""Vercel Blob upload over its REST API (public store). Returns a permanent public URL."""
import json
import urllib.parse
import urllib.request
from pathlib import Path

from .. import config

API = "https://vercel.com/api/blob/"


def upload(path: Path, pathname: str, content_type: str) -> str:
    token = config.BLOB_READ_WRITE_TOKEN
    if not token:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN is not set")
    store_id = token.split("_")[3]
    data = Path(path).read_bytes()
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode({"pathname": pathname}), data=data, method="PUT",
                                 headers={"authorization": f"Bearer {token}", "x-api-version": "13",
                                          "x-vercel-blob-store-id": store_id, "x-vercel-blob-access": "public",
                                          "x-content-type": content_type, "x-add-random-suffix": "1",
                                          "x-content-length": str(len(data))})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["url"]
