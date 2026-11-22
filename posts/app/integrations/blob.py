"""File storage for rendered clips, thumbnails, banners and overlays.

With a Railway bucket configured (BUCKET_* variables), files go there and the returned URL is our own
{PUBLIC_API_URL}/media/<key>, which redirects to a short-lived signed link (Railway buckets are private; their
egress is free). Without one, files go to Vercel Blob as before (its Hobby store was blocked on 22 Nov 2026)."""
import json
import secrets
import urllib.parse
import urllib.request
from functools import lru_cache
from pathlib import Path

from .. import config

API = "https://vercel.com/api/blob/"
SIGNED_SECS = 6 * 24 * 3600  # S3 signed links last at most 7 days; /media signs a fresh one on every request


def bucket_on() -> bool:
    return bool(config.BUCKET_NAME and config.BUCKET_ENDPOINT and config.BUCKET_ACCESS_KEY_ID
                and config.BUCKET_SECRET_ACCESS_KEY)


@lru_cache(maxsize=1)
def _s3():
    import boto3
    from botocore.config import Config
    return boto3.client("s3", endpoint_url=config.BUCKET_ENDPOINT, region_name=config.BUCKET_REGION or "auto",
                        aws_access_key_id=config.BUCKET_ACCESS_KEY_ID,
                        aws_secret_access_key=config.BUCKET_SECRET_ACCESS_KEY,
                        config=Config(signature_version="s3v4", s3={"addressing_style": config.BUCKET_URL_STYLE}))


def suffixed(pathname: str) -> str:
    """clips/a-1.mp4 -> clips/a-1-<random>.mp4, so a new file never reuses an old URL (browser and CDN caches)."""
    p = Path(pathname)
    return str(p.with_name(f"{p.stem}-{secrets.token_hex(8)}{p.suffix}"))


def media_url(key: str) -> str:
    return f"{config.PUBLIC_API_URL}/media/{urllib.parse.quote(key)}"


def signed(key: str) -> str:
    return _s3().generate_presigned_url("get_object", Params={"Bucket": config.BUCKET_NAME, "Key": key},
                                        ExpiresIn=SIGNED_SECS)


def upload(path: Path, pathname: str, content_type: str) -> str:
    if bucket_on():
        key = suffixed(pathname)
        with open(path, "rb") as f:
            _s3().put_object(Bucket=config.BUCKET_NAME, Key=key, Body=f, ContentType=content_type,
                             CacheControl="public, max-age=31536000, immutable")
        return media_url(key)
    token = config.BLOB_READ_WRITE_TOKEN
    if not token:
        raise RuntimeError("no file storage: set the BUCKET_* variables (Railway bucket) or BLOB_READ_WRITE_TOKEN")
    store_id = token.split("_")[3]
    data = Path(path).read_bytes()
    req = urllib.request.Request(API + "?" + urllib.parse.urlencode({"pathname": pathname}), data=data, method="PUT",
                                 headers={"authorization": f"Bearer {token}", "x-api-version": "13",
                                          "x-vercel-blob-store-id": store_id, "x-vercel-blob-access": "public",
                                          "x-content-type": content_type, "x-add-random-suffix": "1",
                                          "x-content-length": str(len(data))})
    with urllib.request.urlopen(req, timeout=600) as r:
        return json.load(r)["url"]
