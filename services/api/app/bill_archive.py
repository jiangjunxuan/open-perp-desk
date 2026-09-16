import asyncio
import csv
import gzip
import io
import ipaddress
import os
import socket
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import PurePosixPath
from urllib.parse import urlsplit

import httpx

from .historical_ledger import DAY_MS, day_ms
from .http_transport import proxy_cleanup_trace


MAX_DOWNLOAD_BYTES = 32 * 1024 * 1024
MAX_CSV_BYTES = 64 * 1024 * 1024
MAX_ROWS = 100_000


class BillArchiveError(ValueError):
    """A safe error code, never a signed download URL or provider response."""


def quarter_days(year: int, quarter: str, *, now: datetime | None = None) -> list[date]:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None or type(year) is not int or not 2021 <= year <= now.year or quarter not in {"Q1", "Q2", "Q3", "Q4"}:
        raise BillArchiveError("archive_quarter_invalid")
    month = 1 + (int(quarter[1]) - 1) * 3
    begin = max(date(year, month, 1), date(2021, 2, 1))
    end = date(year + (month == 10), 1 if month == 10 else month + 3, 1)
    if end > now.astimezone(timezone.utc).date():
        raise BillArchiveError("archive_requires_closed_quarter")
    return [begin + timedelta(days=offset) for offset in range((end - begin).days)]


def parse_bill_archive(
    content: bytes, year: int, quarter: str, subtypes: dict[str, str],
) -> dict[date, list[dict[str, str]]]:
    days = quarter_days(year, quarter)
    if len(content) > MAX_DOWNLOAD_BYTES:
        raise BillArchiveError("archive_download_too_large")
    try:
        if content.startswith(b"PK"):
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                files = [item for item in archive.infolist() if not item.is_dir()]
                if len(files) != 1:
                    raise BillArchiveError("archive_requires_single_csv")
                info = files[0]
                name = PurePosixPath(info.filename)
                if name.is_absolute() or ".." in name.parts or "\\" in info.filename or name.suffix.lower() != ".csv":
                    raise BillArchiveError("archive_member_invalid")
                if info.file_size > MAX_CSV_BYTES or info.flag_bits & 1:
                    raise BillArchiveError("archive_csv_too_large_or_encrypted")
                with archive.open(info) as source:
                    decoded = source.read(MAX_CSV_BYTES + 1)
        elif content.startswith(b"\x1f\x8b"):
            with gzip.GzipFile(fileobj=io.BytesIO(content)) as source:
                decoded = source.read(MAX_CSV_BYTES + 1)
        else:
            decoded = content
        if len(decoded) > MAX_CSV_BYTES:
            raise BillArchiveError("archive_csv_too_large")
        text = decoded.decode("utf-8-sig")
        if "\x00" in text:
            raise BillArchiveError("archive_csv_invalid")
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        fields = reader.fieldnames or []
        required = {"billId", "ts", "ccy", "instId", "instType", "subType", "balChg", "posBalChg", "pnl", "fee"}
        if not required.issubset(fields) or len(fields) != len(set(fields)) or len(fields) > 64:
            raise BillArchiveError("archive_csv_columns_invalid")
        result = {day: [] for day in days}
        seen: set[str] = set()
        begin_ms, end_ms = day_ms(days[0]), day_ms(days[-1]) + DAY_MS
        for index, row in enumerate(reader):
            if index >= MAX_ROWS:
                raise BillArchiveError("archive_row_limit")
            if None in row or any(value is None for value in row.values()):
                raise BillArchiveError("archive_csv_row_invalid")
            bill_id, timestamp = row["billId"], row["ts"]
            if not bill_id or len(bill_id) > 128 or bill_id in seen or not timestamp.isascii() or not timestamp.isdigit():
                raise BillArchiveError("archive_bill_identity_invalid")
            seen.add(bill_id)
            stamp = int(timestamp)
            if not begin_ms <= stamp < end_ms:
                raise BillArchiveError("archive_bill_outside_quarter")
            if not row["ccy"].strip() or len(row["ccy"]) > 40:
                raise BillArchiveError("archive_currency_invalid")
            mapped = subtypes.get(row["subType"])
            if row.get("type") and mapped and row["type"] != mapped:
                raise BillArchiveError("archive_bill_type_conflict")
            if not row.get("type") and mapped:
                row["type"] = mapped
                row["archive_type_source"] = "okx_subtypes"
            day = days[(stamp - begin_ms) // DAY_MS]
            result[day].append(row)
        return result
    except BillArchiveError:
        raise
    except (ValueError, OSError, EOFError, csv.Error, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
        raise BillArchiveError("archive_file_invalid") from exc


class BillArchiveDownloader:
    def __init__(self, *, proxy: str | None = None, transport=None) -> None:
        self.proxy = proxy
        self.transport = transport
        self.allowed_hosts = {
            host.strip().lower() for host in os.getenv("OKX_ARCHIVE_DOWNLOAD_HOSTS", "static.okx.com").split(",")
            if host.strip()
        }

    async def download(self, url: str) -> bytes:
        try:
            if not isinstance(url, str):
                raise BillArchiveError("archive_download_url_not_allowed")
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment
                    or parsed.port not in {None, 443} or host not in self.allowed_hosts
                    or not host.isascii() or len(url) > 8192):
                raise BillArchiveError("archive_download_url_not_allowed")
            try:
                literal = ipaddress.ip_address(host)
            except ValueError:
                literal = None
            if literal is not None and not literal.is_global:
                raise BillArchiveError("archive_download_private_address")
            target = httpx.URL(url)
            if not self.proxy:
                addresses = await asyncio.wait_for(
                    asyncio.to_thread(socket.getaddrinfo, host, 443, type=socket.SOCK_STREAM), 10,
                )
                ips = {ipaddress.ip_address(item[4][0]) for item in addresses}
                if not ips or any(not ip.is_global for ip in ips):
                    raise BillArchiveError("archive_download_private_address")
                # Pin the checked IP while retaining the original TLS identity.
                address = sorted(ips, key=lambda ip: (ip.version, int(ip)))[0]
                target = target.copy_with(host=str(address))
            async with asyncio.timeout(90):
                async with httpx.AsyncClient(
                    proxy=self.proxy, transport=self.transport, follow_redirects=False,
                    trust_env=False, timeout=httpx.Timeout(30, connect=10),
                ) as client:
                    async with client.stream(
                        "GET", target, headers={"Host": host, "Accept-Encoding": "identity"},
                        extensions={"sni_hostname": host, "trace": proxy_cleanup_trace()},
                    ) as response:
                        if response.status_code != 200 or response.headers.get("content-encoding", "identity") != "identity":
                            raise BillArchiveError("archive_download_rejected")
                        length = response.headers.get("content-length")
                        if length and (not length.isdigit() or int(length) > MAX_DOWNLOAD_BYTES):
                            raise BillArchiveError("archive_download_too_large")
                        chunks, size = [], 0
                        async for chunk in response.aiter_raw(chunk_size=65536):
                            size += len(chunk)
                            if size > MAX_DOWNLOAD_BYTES:
                                raise BillArchiveError("archive_download_too_large")
                            chunks.append(chunk)
                        return b"".join(chunks)
        except BillArchiveError:
            raise
        except (httpx.HTTPError, OSError, ValueError, TimeoutError) as exc:
            raise BillArchiveError("archive_download_failed") from exc
