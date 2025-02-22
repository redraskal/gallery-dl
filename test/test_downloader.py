#!/usr/bin/env python3
# -*- coding: utf-8 -*-

# Copyright 2018-2022 Mike Fährmann
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 2 as
# published by the Free Software Foundation.

import os
import sys
import unittest
from unittest.mock import Mock, MagicMock, patch

import re
import logging
import os.path
import binascii
import tempfile
import threading
import http.server


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gallery_dl import downloader, extractor, output, config, path  # noqa E402
from gallery_dl.downloader.http import MIME_TYPES, SIGNATURE_CHECKS # noqa E402


class MockDownloaderModule(Mock):
    __downloader__ = "mock"


class FakeJob():

    def __init__(self):
        self.extractor = extractor.find("test:")
        self.pathfmt = path.PathFormat(self.extractor)
        self.out = output.NullOutput()
        self.get_logger = logging.getLogger


class TestDownloaderModule(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # allow import of ytdl downloader module without youtube_dl installed
        sys.modules["youtube_dl"] = MagicMock()

    @classmethod
    def tearDownClass(cls):
        del sys.modules["youtube_dl"]

    def tearDown(self):
        downloader._cache.clear()

    def test_find(self):
        cls = downloader.find("http")
        self.assertEqual(cls.__name__, "HttpDownloader")
        self.assertEqual(cls.scheme  , "http")

        cls = downloader.find("https")
        self.assertEqual(cls.__name__, "HttpDownloader")
        self.assertEqual(cls.scheme  , "http")

        cls = downloader.find("text")
        self.assertEqual(cls.__name__, "TextDownloader")
        self.assertEqual(cls.scheme  , "text")

        cls = downloader.find("ytdl")
        self.assertEqual(cls.__name__, "YoutubeDLDownloader")
        self.assertEqual(cls.scheme  , "ytdl")

        self.assertEqual(downloader.find("ftp"), None)
        self.assertEqual(downloader.find("foo"), None)
        self.assertEqual(downloader.find(1234) , None)
        self.assertEqual(downloader.find(None) , None)

    @patch("builtins.__import__")
    def test_cache(self, import_module):
        import_module.return_value = MockDownloaderModule()
        downloader.find("http")
        downloader.find("text")
        downloader.find("ytdl")
        self.assertEqual(import_module.call_count, 3)
        downloader.find("http")
        downloader.find("text")
        downloader.find("ytdl")
        self.assertEqual(import_module.call_count, 3)

    @patch("builtins.__import__")
    def test_cache_http(self, import_module):
        import_module.return_value = MockDownloaderModule()
        downloader.find("http")
        downloader.find("https")
        self.assertEqual(import_module.call_count, 1)

    @patch("builtins.__import__")
    def test_cache_https(self, import_module):
        import_module.return_value = MockDownloaderModule()
        downloader.find("https")
        downloader.find("http")
        self.assertEqual(import_module.call_count, 1)


class TestDownloaderBase(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.TemporaryDirectory()
        cls.fnum = 0
        config.set((), "base-directory", cls.dir.name)
        cls.job = FakeJob()

    @classmethod
    def tearDownClass(cls):
        cls.dir.cleanup()
        config.clear()

    @classmethod
    def _prepare_destination(cls, content=None, part=True, extension=None):
        name = "file-{}".format(cls.fnum)
        cls.fnum += 1

        kwdict = {
            "category"   : "test",
            "subcategory": "test",
            "filename"   : name,
            "extension"  : extension,
        }

        pathfmt = cls.job.pathfmt
        pathfmt.set_directory(kwdict)
        pathfmt.set_filename(kwdict)

        if content:
            mode = "w" + ("b" if isinstance(content, bytes) else "")
            with pathfmt.open(mode) as file:
                file.write(content)

        return pathfmt

    def _run_test(self, url, input, output,
                  extension, expected_extension=None):
        pathfmt = self._prepare_destination(input, extension=extension)
        success = self.downloader.download(url, pathfmt)

        # test successful download
        self.assertTrue(success, "downloading '{}' failed".format(url))

        # test content
        mode = "r" + ("b" if isinstance(output, bytes) else "")
        with pathfmt.open(mode) as file:
            content = file.read()
        self.assertEqual(content, output)

        # test filename extension
        self.assertEqual(
            pathfmt.extension,
            expected_extension,
        )
        self.assertEqual(
            os.path.splitext(pathfmt.realpath)[1][1:],
            expected_extension,
        )


class TestHTTPDownloader(TestDownloaderBase):

    @classmethod
    def setUpClass(cls):
        TestDownloaderBase.setUpClass()
        cls.downloader = downloader.find("http")(cls.job)

        port = 8088
        cls.address = "http://127.0.0.1:{}".format(port)
        server = http.server.HTTPServer(("", port), HttpRequestHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def _run_test(self, ext, input, output,
                  extension, expected_extension=None):
        TestDownloaderBase._run_test(
            self, self.address + "/image." + ext, input, output,
            extension, expected_extension)

    def tearDown(self):
        self.downloader.minsize = self.downloader.maxsize = None

    def test_http_download(self):
        self._run_test("jpg", None, DATA["jpg"], "jpg", "jpg")
        self._run_test("png", None, DATA["png"], "png", "png")
        self._run_test("gif", None, DATA["gif"], "gif", "gif")

    def test_http_offset(self):
        self._run_test("jpg", DATA["jpg"][:123], DATA["jpg"], "jpg", "jpg")
        self._run_test("png", DATA["png"][:12] , DATA["png"], "png", "png")
        self._run_test("gif", DATA["gif"][:1]  , DATA["gif"], "gif", "gif")

    def test_http_extension(self):
        self._run_test("jpg", None, DATA["jpg"], None, "jpg")
        self._run_test("png", None, DATA["png"], None, "png")
        self._run_test("gif", None, DATA["gif"], None, "gif")

    def test_http_adjust_extension(self):
        self._run_test("jpg", None, DATA["jpg"], "png", "jpg")
        self._run_test("png", None, DATA["png"], "gif", "png")
        self._run_test("gif", None, DATA["gif"], "jpg", "gif")

    def test_http_filesize_min(self):
        url = self.address + "/image.gif"
        pathfmt = self._prepare_destination(None, extension=None)
        self.downloader.minsize = 100
        with self.assertLogs(self.downloader.log, "WARNING"):
            success = self.downloader.download(url, pathfmt)
        self.assertFalse(success)

    def test_http_filesize_max(self):
        url = self.address + "/image.jpg"
        pathfmt = self._prepare_destination(None, extension=None)
        self.downloader.maxsize = 100
        with self.assertLogs(self.downloader.log, "WARNING"):
            success = self.downloader.download(url, pathfmt)
        self.assertFalse(success)


class TestTextDownloader(TestDownloaderBase):

    @classmethod
    def setUpClass(cls):
        TestDownloaderBase.setUpClass()
        cls.downloader = downloader.find("text")(cls.job)

    def test_text_download(self):
        self._run_test("text:foobar", None, "foobar", "txt", "txt")

    def test_text_offset(self):
        self._run_test("text:foobar", "foo", "foobar", "txt", "txt")

    def test_text_empty(self):
        self._run_test("text:", None, "", "txt", "txt")


class HttpRequestHandler(http.server.BaseHTTPRequestHandler):

    def do_GET(self):
        if self.path.startswith("/image."):
            ext = self.path.rpartition(".")[2]
            content_type = MIME_TYPES.get(ext)
            output = DATA[ext]
        else:
            self.send_response(404)
            self.wfile.write(self.path.encode())
            return

        headers = {
            "Content-Type": content_type,
            "Content-Length": len(output),
        }

        if "Range" in self.headers:
            status = 206

            match = re.match(r"bytes=(\d+)-", self.headers["Range"])
            start = int(match.group(1))

            headers["Content-Range"] = "bytes {}-{}/{}".format(
                start, len(output)-1, len(output))
            output = output[start:]
        else:
            status = 200

        self.send_response(status)
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(output)


DATA = {
    "jpg" : binascii.a2b_base64(
        "/9j/4AAQSkZJRgABAQEASABIAAD/2wBDAAEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
        "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQH/2wBDAQEB"
        "AQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEBAQEB"
        "AQEBAQEBAQEBAQEBAQH/wAARCAABAAEDAREAAhEBAxEB/8QAFAABAAAAAAAAAAAA"
        "AAAAAAAACv/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAA"
        "AAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AfwD/2Q=="),
    "png" : binascii.a2b_base64(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAAAAAA6fptVAAAACklEQVQIHWP4DwAB"
        "AQEANl9ngAAAAABJRU5ErkJggg=="),
    "gif" : binascii.a2b_base64(
        "R0lGODdhAQABAIAAAP///////ywAAAAAAQABAAACAkQBADs="),
    "bmp" : b"BM",
    "webp": b"RIFF????WEBP",
    "avif": b"????ftypavif",
    "svg" : b"<?xml",
    "ico" : b"\x00\x00\x01\x00",
    "cur" : b"\x00\x00\x02\x00",
    "psd" : b"8BPS",
    "webm": b"\x1A\x45\xDF\xA3",
    "ogg" : b"OggS",
    "wav" : b"RIFF????WAVE",
    "mp3" : b"ID3",
    "zip" : b"PK\x03\x04",
    "rar" : b"\x52\x61\x72\x21\x1A\x07",
    "7z"  : b"\x37\x7A\xBC\xAF\x27\x1C",
    "pdf" : b"%PDF-",
    "swf" : b"CWS",
}


# reverse mime types mapping
MIME_TYPES = {
    ext: mtype
    for mtype, ext in MIME_TYPES.items()
}


def generate_tests():
    def _generate_test(ext):
        def test(self):
            self._run_test(ext, None, DATA[ext], "bin", ext)
        test.__name__ = "test_http_ext_" + ext
        return test

    for ext in DATA:
        test = _generate_test(ext)
        setattr(TestHTTPDownloader, test.__name__, test)


generate_tests()
if __name__ == "__main__":
    unittest.main()
