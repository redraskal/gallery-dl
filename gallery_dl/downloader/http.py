# -*- coding: utf-8 -*-

# Copyright 2014-2022 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

"""Downloader module for http:// and https:// URLs"""

import time
import mimetypes
from requests.exceptions import RequestException, ConnectionError, Timeout
from .common import DownloaderBase
from .. import text, util

from email.utils import parsedate_tz
from datetime import datetime
from ssl import SSLError
try:
    from OpenSSL.SSL import Error as OpenSSLError
except ImportError:
    OpenSSLError = SSLError


class HttpDownloader(DownloaderBase):
    scheme = "http"

    def __init__(self, job):
        DownloaderBase.__init__(self, job)
        extractor = job.extractor
        self.downloading = False

        self.adjust_extension = self.config("adjust-extensions", True)
        self.chunk_size = self.config("chunk-size", 32768)
        self.metadata = extractor.config("http-metadata")
        self.progress = self.config("progress", 3.0)
        self.headers = self.config("headers")
        self.minsize = self.config("filesize-min")
        self.maxsize = self.config("filesize-max")
        self.retries = self.config("retries", extractor._retries)
        self.timeout = self.config("timeout", extractor._timeout)
        self.verify = self.config("verify", extractor._verify)
        self.mtime = self.config("mtime", True)
        self.rate = self.config("rate")

        if self.retries < 0:
            self.retries = float("inf")
        if self.minsize:
            minsize = text.parse_bytes(self.minsize)
            if not minsize:
                self.log.warning(
                    "Invalid minimum file size (%r)", self.minsize)
            self.minsize = minsize
        if self.maxsize:
            maxsize = text.parse_bytes(self.maxsize)
            if not maxsize:
                self.log.warning(
                    "Invalid maximum file size (%r)", self.maxsize)
            self.maxsize = maxsize
        if isinstance(self.chunk_size, str):
            chunk_size = text.parse_bytes(self.chunk_size)
            if not chunk_size:
                self.log.warning(
                    "Invalid chunk size (%r)", self.chunk_size)
                chunk_size = 32768
            self.chunk_size = chunk_size
        if self.rate:
            rate = text.parse_bytes(self.rate)
            if rate:
                if rate < self.chunk_size:
                    self.chunk_size = rate
                self.rate = rate
                self.receive = self._receive_rate
            else:
                self.log.warning("Invalid rate limit (%r)", self.rate)
        if self.progress is not None:
            self.receive = self._receive_rate

    def download(self, url, pathfmt):
        try:
            return self._download_impl(url, pathfmt)
        except Exception:
            print()
            raise
        finally:
            # remove file from incomplete downloads
            if self.downloading and not self.part:
                util.remove_file(pathfmt.temppath)

    def _download_impl(self, url, pathfmt):
        response = None
        tries = 0
        msg = ""

        kwdict = pathfmt.kwdict
        adjust_extension = kwdict.get(
            "_http_adjust_extension", self.adjust_extension)

        if self.part:
            pathfmt.part_enable(self.partdir)

        while True:
            if tries:
                if response:
                    response.close()
                    response = None
                self.log.warning("%s (%s/%s)", msg, tries, self.retries+1)
                if tries > self.retries:
                    return False
                time.sleep(tries)

            tries += 1
            file_header = None

            # collect HTTP headers
            headers = {"Accept": "*/*"}
            #   file-specific headers
            extra = kwdict.get("_http_headers")
            if extra:
                headers.update(extra)
            #   general headers
            if self.headers:
                headers.update(self.headers)
            #   partial content
            file_size = pathfmt.part_size()
            if file_size:
                headers["Range"] = "bytes={}-".format(file_size)

            # connect to (remote) source
            try:
                response = self.session.request(
                    kwdict.get("_http_method", "GET"), url,
                    stream=True,
                    headers=headers,
                    data=kwdict.get("_http_data"),
                    timeout=self.timeout,
                    proxies=self.proxies,
                    verify=self.verify,
                )
            except (ConnectionError, Timeout) as exc:
                msg = str(exc)
                continue
            except Exception as exc:
                self.log.warning(exc)
                return False

            # check response
            code = response.status_code
            if code == 200:  # OK
                offset = 0
                size = response.headers.get("Content-Length")
            elif code == 206:  # Partial Content
                offset = file_size
                size = response.headers["Content-Range"].rpartition("/")[2]
            elif code == 416 and file_size:  # Requested Range Not Satisfiable
                break
            else:
                msg = "'{} {}' for '{}'".format(code, response.reason, url)
                if code == 429 or 500 <= code < 600:  # Server Error
                    continue
                self.log.warning(msg)
                return False

            # check for invalid responses
            validate = kwdict.get("_http_validate")
            if validate:
                result = validate(response)
                if isinstance(result, str):
                    url = result
                    tries -= 1
                    continue
                if not result:
                    self.log.warning("Invalid response")
                    return False

            # check file size
            size = text.parse_int(size, None)
            if size is not None:
                if self.minsize and size < self.minsize:
                    self.log.warning(
                        "File size smaller than allowed minimum (%s < %s)",
                        size, self.minsize)
                    return False
                if self.maxsize and size > self.maxsize:
                    self.log.warning(
                        "File size larger than allowed maximum (%s > %s)",
                        size, self.maxsize)
                    return False

            # set missing filename extension from MIME type
            if not pathfmt.extension:
                pathfmt.set_extension(self._find_extension(response))
                if pathfmt.exists():
                    pathfmt.temppath = ""
                    return True

            # set metadata from HTTP headers
            if self.metadata:
                kwdict[self.metadata] = self._extract_metadata(response)
                pathfmt.build_path()
                if pathfmt.exists():
                    pathfmt.temppath = ""
                    return True

            content = response.iter_content(self.chunk_size)

            # check filename extension against file header
            if adjust_extension and not offset and \
                    pathfmt.extension in SIGNATURE_CHECKS:
                try:
                    file_header = next(
                        content if response.raw.chunked
                        else response.iter_content(16), b"")
                except (RequestException, SSLError, OpenSSLError) as exc:
                    msg = str(exc)
                    print()
                    continue
                if self._adjust_extension(pathfmt, file_header) and \
                        pathfmt.exists():
                    pathfmt.temppath = ""
                    return True

            # set open mode
            if not offset:
                mode = "w+b"
                if file_size:
                    self.log.debug("Unable to resume partial download")
            else:
                mode = "r+b"
                self.log.debug("Resuming download at byte %d", offset)

            # download content
            self.downloading = True
            with pathfmt.open(mode) as fp:
                if file_header:
                    fp.write(file_header)
                    offset += len(file_header)
                elif offset:
                    if adjust_extension and \
                            pathfmt.extension in SIGNATURE_CHECKS:
                        self._adjust_extension(pathfmt, fp.read(16))
                    fp.seek(offset)

                self.out.start(pathfmt.path)
                try:
                    self.receive(fp, content, size, offset)
                except (RequestException, SSLError, OpenSSLError) as exc:
                    msg = str(exc)
                    print()
                    continue

                # check file size
                if size and fp.tell() < size:
                    msg = "file size mismatch ({} < {})".format(
                        fp.tell(), size)
                    print()
                    continue

            break

        self.downloading = False
        if self.mtime:
            kwdict.setdefault("_mtime", response.headers.get("Last-Modified"))
        else:
            kwdict["_mtime"] = None

        return True

    @staticmethod
    def receive(fp, content, bytes_total, bytes_downloaded):
        write = fp.write
        for data in content:
            write(data)

    def _receive_rate(self, fp, content, bytes_total, bytes_downloaded):
        rate = self.rate
        progress = self.progress
        bytes_start = bytes_downloaded
        write = fp.write
        t1 = tstart = time.time()

        for data in content:
            write(data)

            t2 = time.time()           # current time
            elapsed = t2 - t1          # elapsed time
            num_bytes = len(data)

            if progress is not None:
                bytes_downloaded += num_bytes
                tdiff = t2 - tstart
                if tdiff >= progress:
                    self.out.progress(
                        bytes_total, bytes_downloaded,
                        int((bytes_downloaded - bytes_start) / tdiff),
                    )

            if rate:
                expected = num_bytes / rate  # expected elapsed time
                if elapsed < expected:
                    # sleep if less time elapsed than expected
                    time.sleep(expected - elapsed)
                    t2 = time.time()

            t1 = t2

    def _extract_metadata(self, response):
        headers = response.headers
        data = dict(headers)

        hcd = headers.get("content-disposition")
        if hcd:
            name = text.extr(hcd, 'filename="', '"')
            if name:
                text.nameext_from_url(name, data)

        hlm = headers.get("last-modified")
        if hlm:
            data["date"] = datetime(*parsedate_tz(hlm)[:6])

        return data

    def _find_extension(self, response):
        """Get filename extension from MIME type"""
        mtype = response.headers.get("Content-Type", "image/jpeg")
        mtype = mtype.partition(";")[0]

        if "/" not in mtype:
            mtype = "image/" + mtype

        if mtype in MIME_TYPES:
            return MIME_TYPES[mtype]

        ext = mimetypes.guess_extension(mtype, strict=False)
        if ext:
            return ext[1:]

        self.log.warning("Unknown MIME type '%s'", mtype)
        return "bin"

    @staticmethod
    def _adjust_extension(pathfmt, file_header):
        """Check filename extension against file header"""
        if not SIGNATURE_CHECKS[pathfmt.extension](file_header):
            for ext, check in SIGNATURE_CHECKS.items():
                if check(file_header):
                    pathfmt.set_extension(ext)
                    return True
        return False


MIME_TYPES = {
    "image/jpeg"    : "jpg",
    "image/jpg"     : "jpg",
    "image/png"     : "png",
    "image/gif"     : "gif",
    "image/bmp"     : "bmp",
    "image/x-bmp"   : "bmp",
    "image/x-ms-bmp": "bmp",
    "image/webp"    : "webp",
    "image/avif"    : "avif",
    "image/svg+xml" : "svg",
    "image/ico"     : "ico",
    "image/icon"    : "ico",
    "image/x-icon"  : "ico",
    "image/vnd.microsoft.icon" : "ico",
    "image/x-photoshop"        : "psd",
    "application/x-photoshop"  : "psd",
    "image/vnd.adobe.photoshop": "psd",

    "video/webm": "webm",
    "video/ogg" : "ogg",
    "video/mp4" : "mp4",

    "audio/wav"  : "wav",
    "audio/x-wav": "wav",
    "audio/webm" : "webm",
    "audio/ogg"  : "ogg",
    "audio/mpeg" : "mp3",

    "application/zip"  : "zip",
    "application/x-zip": "zip",
    "application/x-zip-compressed": "zip",
    "application/rar"  : "rar",
    "application/x-rar": "rar",
    "application/x-rar-compressed": "rar",
    "application/x-7z-compressed" : "7z",

    "application/pdf"  : "pdf",
    "application/x-pdf": "pdf",
    "application/x-shockwave-flash": "swf",

    "application/ogg": "ogg",
    "application/octet-stream": "bin",
}

# https://en.wikipedia.org/wiki/List_of_file_signatures
SIGNATURE_CHECKS = {
    "jpg" : lambda s: s[0:3] == b"\xFF\xD8\xFF",
    "png" : lambda s: s[0:8] == b"\x89PNG\r\n\x1A\n",
    "gif" : lambda s: s[0:6] in (b"GIF87a", b"GIF89a"),
    "bmp" : lambda s: s[0:2] == b"BM",
    "webp": lambda s: (s[0:4] == b"RIFF" and
                       s[8:12] == b"WEBP"),
    "avif": lambda s: s[4:12] == b"ftypavif",
    "svg" : lambda s: s[0:5] == b"<?xml",
    "ico" : lambda s: s[0:4] == b"\x00\x00\x01\x00",
    "cur" : lambda s: s[0:4] == b"\x00\x00\x02\x00",
    "psd" : lambda s: s[0:4] == b"8BPS",
    "webm": lambda s: s[0:4] == b"\x1A\x45\xDF\xA3",
    "ogg" : lambda s: s[0:4] == b"OggS",
    "wav" : lambda s: (s[0:4] == b"RIFF" and
                       s[8:12] == b"WAVE"),
    "mp3" : lambda s: (s[0:3] == b"ID3" or
                       s[0:2] in (b"\xFF\xFB", b"\xFF\xF3", b"\xFF\xF2")),
    "zip" : lambda s: s[0:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    "rar" : lambda s: s[0:6] == b"\x52\x61\x72\x21\x1A\x07",
    "7z"  : lambda s: s[0:6] == b"\x37\x7A\xBC\xAF\x27\x1C",
    "pdf" : lambda s: s[0:5] == b"%PDF-",
    "swf" : lambda s: s[0:3] in (b"CWS", b"FWS"),
    # check 'bin' files against all other file signatures
    "bin" : lambda s: False,
}

__downloader__ = HttpDownloader
