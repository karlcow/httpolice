# -*- coding: utf-8; -*-

from cStringIO import StringIO
import gzip
import json
import zlib

import defusedxml
import defusedxml.ElementTree

from httpolice import common, header_view, parse
from httpolice.common import Unparseable, okay
from httpolice.known import cc, header, media, media_type, tc
from httpolice.syntax import rfc7230
from httpolice.syntax.common import crlf


class Message(common.ReportNode):

    self_name = 'msg'

    def __init__(self, version, header_entries,
                 body=None, trailer_entries=None, raw=None):
        super(Message, self).__init__()
        self.version = version
        self.header_entries = header_entries
        self.body = body
        self.trailer_entries = trailer_entries
        self.raw = raw
        self.rebuild_headers()
        self._decoded_body = None

    def rebuild_headers(self):
        self.headers = header_view.HeadersView(self)

    @property
    def decoded_body(self):
        if self._decoded_body is None:
            self._decode_body()
        return self._decoded_body

    def _decode_body(self):
        r = self.body
        codings = list(self.headers.content_encoding)
        while codings and okay(r):
            coding = codings.pop()
            if coding in [cc.gzip, cc.x_gzip]:
                try:
                    r = decode_gzip(r)
                except Exception, e:
                    self.complain(1037, coding=coding, error=e)
                    r = Unparseable
            elif coding == cc.deflate:
                try:
                    r = decode_deflate(r)
                except Exception, e:
                    self.complain(1037, coding=coding, error=e)
                    r = Unparseable
            elif okay(coding):
                self.complain(1036, coding=coding)
                r = Unparseable
            else:
                r = Unparseable
        self._decoded_body = r


def check_message(msg):
    # Force parsing every header present in the message according to its rules.
    for entry in msg.header_entries + (msg.trailer_entries or []):
        _ = msg.headers[entry.name].value

    data = msg.decoded_body
    if okay(data) and msg.headers.content_type.is_okay:
        check_media(msg, msg.headers.content_type.value, data)

    for entry in msg.trailer_entries or []:
        if entry.name not in msg.headers.trailer:
            msg.complain(1030, header=entry)

    if msg.headers.transfer_encoding.is_present and \
            msg.headers.content_length.is_present:
        msg.complain(1020)

    for opt in msg.headers.connection:
        if okay(opt) and header.is_bad_for_connection(common.FieldName(opt)):
            msg.complain(1034, header=msg.headers[common.FieldName(opt)])

    if msg.headers.content_type.is_okay:
        if media_type.deprecated(msg.headers.content_type.value.item):
            msg.complain(1035)
        seen_params = set()
        for param_name, _ in msg.headers.content_type.value.param:
            if param_name in seen_params:
                msg.complain(1042, param=param_name)
            seen_params.add(param_name)

    if okay(msg.body) and msg.body and msg.headers.content_type.is_absent:
        msg.complain(1041)

    if msg.headers.upgrade.is_present and \
            u'upgrade' not in msg.headers.connection:
        msg.complain(1050)


def check_media(msg, type_, data):
    the_type = type_.item

    if media_type.is_json(the_type):
        try:
            json.loads(data)
        except Exception, e:
            msg.complain(1038, error=e)

    if media_type.is_xml(the_type):
        try:
            defusedxml.ElementTree.fromstring(data)
        except defusedxml.DefusedXmlException:
            pass
        except Exception, e:
            msg.complain(1039, error=e)

    if the_type == media.application_x_www_form_urlencoded:
        # This list is taken from the HTML specification --
        # http://www.w3.org/TR/html/forms.html#url-encoded-form-data --
        # as the exhaustive list of bytes that can be output
        # by a "conformant" URL encoder.
        good_bytes = ([0x25, 0x26, 0x2A, 0x2B, 0x2D, 0x2E, 0x5F] +
                      range(0x30, 0x40) + range(0x41, 0x5B) +
                      range(0x61, 0x7B))
        for byte in data:
            if ord(byte) not in good_bytes:
                msg.complain(1040, offending_value=hex(ord(byte)))
                break


def body_charset(msg):
    if msg.headers.content_type.is_okay:
        for name, value in msg.headers.content_type.value.param:
            if name == u'charset':
                return value


def parse_chunked(msg, state):
    data = []
    try:
        chunk = rfc7230.chunk.parse(state)
        while chunk:
            data.append(chunk)
            chunk = rfc7230.chunk.parse(state)
        trailer = rfc7230.trailer_part.parse(state)
        crlf.parse(state)
    except parse.ParseError, e:
        msg.complain(1005, error=e)
        msg.body = Unparseable
        state.sane = False
    else:
        msg.body = ''.join(data)
        msg.trailer_entries = trailer
        if trailer:
            msg.rebuild_headers()           # Rebuid the `HeadersView` cache


def decode_transfer_coding(msg, coding):
    if coding == tc.chunked:
        # The outermost chunked has already been peeled off at this point.
        msg.complain(1002)
        msg.body = Unparseable
    elif coding in [tc.gzip, tc.x_gzip]:
        try:
            msg.body = decode_gzip(msg.body)
        except Exception, e:
            msg.complain(1027, coding=coding, error=e)
            msg.body = Unparseable
    elif coding == tc.deflate:
        try:
            msg.body = decode_deflate(msg.body)
        except Exception, e:
            msg.complain(1027, coding=coding, error=e)
            msg.body = Unparseable
    else:
        msg.complain(1003, coding=coding)
        msg.body = Unparseable


def decode_gzip(data):
    return gzip.GzipFile(fileobj=StringIO(data)).read()


def decode_deflate(data):
    return zlib.decompress(data)
