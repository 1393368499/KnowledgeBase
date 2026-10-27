import importlib.util
import io
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("voice_gateway", Path(__file__).resolve().parents[1] / "deploy/voice-gateway/server.py")
gateway = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gateway)


def test_rechunked_request_preserves_next_request_on_persistent_connection():
    stream = io.BytesIO(b'3;ext=1\r\nabc\r\n2\r\nde\r\n0\r\nX-Trailer: ignored\r\n\r\nGET /next HTTP/1.1\r\n')
    assert gateway.read_body(stream, None, 'chunked') == b'abcde'
    assert stream.readline() == b'GET /next HTTP/1.1\r\n'
    empty = io.BytesIO(b'0\r\n\r\nNEXT')
    assert gateway.read_body(empty, None, 'chunked') == b''
    assert empty.read() == b'NEXT'


@pytest.mark.parametrize('body,length,encoding', [
    (b'0\r\n\r\n', '0', 'chunked'),
    (b'', None, 'gzip, chunked'),
    (b'XYZ\r\n', None, 'chunked'),
    (b'5\r\nabc\r\n', None, 'chunked'),
    (b'1\r\nxXX', None, 'chunked'),
    (b'0\r\nunterminated', None, 'chunked'),
    (b'', '-1', None),
    (b'ab', '3', None),
])
def test_rejects_ambiguous_truncated_and_invalid_framing(body, length, encoding):
    with pytest.raises(ValueError):
        gateway.read_body(io.BytesIO(body), length, encoding)


def test_chunk_limit_is_enforced_before_reading_unbounded_body():
    with pytest.raises(gateway.BodyTooLarge):
        gateway.read_body(io.BytesIO(b'400000\r\n'), None, 'chunked')
    with pytest.raises(gateway.BodyTooLarge):
        gateway.read_body(io.BytesIO(), str(gateway.MAX_BODY + 1), None)
