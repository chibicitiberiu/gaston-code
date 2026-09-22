"""Frame codec tests.

The guest parses these frames in C with strchr and fixed buffers, so the cases
that matter most are the malformed ones: anything that gets past the bridge and
confuses the guest is a hang or a crash on a machine with no debugger.
"""

import struct

import pytest

from bridge.wire.protocol import (
                                  MAX_FRAME,
                                  MAX_HEADERS,
                                  MAX_HEADER_KEY,
                                  MAX_HEADER_VALUE,
                                  MAX_TYPE,
                                  PROTO_VERSION,
                                  Frame,
                                  FrameReader,
                                  ProtocolError,
                                  error,
                                  hello,
                                  option,
                                  truncate_header_value,
                                  )


def roundtrip(f: Frame) -> Frame:
    wire = f.encode()
    (length,) = struct.unpack_from("<I", wire, 0)
    assert length == len(wire) - 4
    return Frame.decode(wire[4:])


def test_roundtrip_plain():
    got = roundtrip(Frame("TEXT", {"seq": "1", "style": "plain"}, b"hello"))
    assert got.type == "TEXT"
    assert got.headers == {"seq": "1", "style": "plain"}
    assert got.body == b"hello"


def test_roundtrip_empty_body():
    got = roundtrip(Frame("INTERRUPT", {}))
    assert got.type == "INTERRUPT"
    assert got.body == b""


def test_body_may_contain_the_header_terminator():
    """A body containing \\n\\n must not be mistaken for the end of headers --
    only the FIRST occurrence terminates."""
    body = b"line one\n\nline two\n\nline three"
    got = roundtrip(Frame("TEXT", {"seq": "9"}, body))
    assert got.body == body


def test_body_may_be_arbitrary_binary():
    body = bytes(range(256))
    got = roundtrip(Frame("BLOB", {"n": "1"}, body))
    assert got.body == body


@pytest.mark.parametrize(
    "value",
    [
        "plain",
        "has spaces",
        "has=equals",
        "has\nnewline",
        "has\r\ncrlf",
        "100% literal",
        "%0A not really a newline",
        "",
    ],
)
def test_header_values_survive_quoting(value):
    """Header values are percent-encoded rather than backslash-escaped.

    claudewin9x needed a section of its system prompt explaining how to
    quadruple backslashes across four interpreter layers. Percent-encoding
    three characters avoids that entire class of problem.
    """
    got = roundtrip(Frame("TEXT", {"v": value}, b""))
    assert got.headers["v"] == value


def test_enc_defaults_to_raw():
    assert Frame("TEXT", {}).enc == "raw"
    assert Frame("TEXT", {"enc": "deflate"}).enc == "deflate"


# --- malformed input --------------------------------------------------------


def test_missing_header_terminator_rejected():
    with pytest.raises(ProtocolError, match="terminator"):
        Frame.decode(b"TEXT\nseq=1\n")


def test_empty_type_rejected():
    with pytest.raises(ProtocolError, match="empty frame type"):
        Frame.decode(b"\n\n")


def test_header_without_equals_rejected():
    with pytest.raises(ProtocolError, match="without"):
        Frame.decode(b"TEXT\ngarbage\n\n")


def test_duplicate_header_rejected():
    """Silently taking the last value would make a spoofed seq= trivially
    injectable into an otherwise valid frame."""
    with pytest.raises(ProtocolError, match="duplicate"):
        Frame.decode(b"TEXT\nseq=1\nseq=2\n\n")


def test_lowercase_type_rejected_on_encode():
    with pytest.raises(ProtocolError, match="uppercase"):
        Frame("text").encode()


def test_bad_header_key_rejected_on_encode():
    with pytest.raises(ProtocolError, match="bad header key"):
        Frame("TEXT", {"has space": "1"}).encode()


def test_oversized_frame_rejected_on_encode():
    with pytest.raises(ProtocolError, match="cap"):
        Frame("TEXT", {}, b"x" * MAX_FRAME).encode()


def test_oversized_type_rejected_on_encode():
    with pytest.raises(ProtocolError, match="frame type too long"):
        Frame("A" * (MAX_TYPE + 1)).encode()


def test_too_many_headers_rejected_on_encode():
    headers = {f"k{i}": "v" for i in range(MAX_HEADERS + 1)}
    with pytest.raises(ProtocolError, match="too many headers"):
        Frame("TEST", headers).encode()


def test_oversized_key_rejected_on_encode():
    with pytest.raises(ProtocolError, match="header key too long"):
        Frame("TEST", {"k" * (MAX_HEADER_KEY + 1): "v"}).encode()


def test_oversized_value_rejected_on_encode():
    with pytest.raises(ProtocolError, match="value too long"):
        Frame("TEST", {"k": "x" * (MAX_HEADER_VALUE + 1)}).encode()


def test_oversized_type_rejected_on_decode():
    payload = ("A" * (MAX_TYPE + 1)).encode("ascii") + b"\n\n"
    with pytest.raises(ProtocolError, match="frame type too long"):
        Frame.decode(payload)


def test_too_many_headers_rejected_on_decode():
    lines = [b"TEST"] + [f"k{i}=v".encode("ascii") for i in range(MAX_HEADERS + 1)] + [b"", b""]
    with pytest.raises(ProtocolError, match="too many headers"):
        Frame.decode(b"\n".join(lines))


def test_oversized_key_rejected_on_decode():
    payload = b"TEST\n" + (b"k" * (MAX_HEADER_KEY + 1)) + b"=v\n\n"
    with pytest.raises(ProtocolError, match="header key too long"):
        Frame.decode(payload)


def test_oversized_value_rejected_on_decode():
    payload = b"TEST\nk=" + (b"v" * (MAX_HEADER_VALUE + 1)) + b"\n\n"
    with pytest.raises(ProtocolError, match="header 'k' value too long"):
        Frame.decode(payload)


def test_non_ascii_header_rejected_on_decode():
    payload = b"TEST\nk=caf\xe9\n\n"
    with pytest.raises(ProtocolError, match="non-ascii"):
        Frame.decode(payload)


def test_truncate_header_value():
    assert truncate_header_value("hello", 10) == "hello"
    long_val = "a" * 300
    res = truncate_header_value(long_val, MAX_HEADER_VALUE)
    assert len(res) <= MAX_HEADER_VALUE
    assert res.endswith("...")
    # Accented chars expand to %XX (3 bytes each)
    accents = "é" * 100
    res2 = truncate_header_value(accents, MAX_HEADER_VALUE)
    from bridge.wire.protocol import _quote
    assert len(_quote(res2)) <= MAX_HEADER_VALUE
    assert res2.endswith("...")


def test_error_frame_truncates_oversized_msg():
    huge_msg = "protocol error details: " + ("x" * 1000)
    f = error("protocol", huge_msg)
    wire = f.encode()
    decoded = Frame.decode(wire[4:])
    assert decoded.type == "ERROR"
    assert decoded.headers["code"] == "protocol"
    assert decoded.headers["msg"].endswith("...")
    assert len(decoded.headers["msg"]) <= MAX_HEADER_VALUE


def test_option_frame_truncates_oversized_values():
    long_path = "C:\\" + ("a" * 300)
    f = option("work", long_path, long_path, False, desc=long_path)
    wire = f.encode()
    decoded = Frame.decode(wire[4:])
    assert decoded.type == "OPTION"
    assert decoded.headers["id"].endswith("...")
    assert decoded.headers["label"].endswith("...")
    assert decoded.headers["desc"].endswith("...")
    assert len(decoded.headers["id"]) <= MAX_HEADER_VALUE
    assert len(decoded.headers["label"]) <= MAX_HEADER_VALUE
    assert len(decoded.headers["desc"]) <= MAX_HEADER_VALUE


# --- incremental reassembly -------------------------------------------------


def test_reader_handles_split_across_reads():
    """TCP gives you arbitrary fragments, and the guest's recv() will too."""
    wire = (Frame("TEXT", {"seq": "1"}, b"abc").encode()
            + Frame("TEXT", {"seq": "2"}, b"def").encode())
    reader = FrameReader()
    out = []
    for i in range(0, len(wire), 3):
        out.extend(reader.feed(wire[i : i + 3]))
    assert [f.body for f in out] == [b"abc", b"def"]
    assert [f.headers["seq"] for f in out] == ["1", "2"]


def test_reader_returns_nothing_on_partial_header():
    assert FrameReader().feed(b"\x10") == []


def test_reader_yields_multiple_frames_from_one_read():
    wire = b"".join(Frame("TEXT", {"seq": str(i)}, b"%d" % i).encode()
                    for i in range(5))
    assert len(FrameReader().feed(wire)) == 5


def test_reader_rejects_oversized_length_prefix():
    """The single most dangerous malformed input: a corrupted length prefix on
    a 32MB machine. retro-agent's MAX_FRAME_SIZE is 32MB -- inheriting that
    constant would let one bad frame allocate the whole box away.
    """
    reader = FrameReader()
    with pytest.raises(ProtocolError, match="cap is"):
        reader.feed(struct.pack("<I", 32 * 1024 * 1024) + b"TEXT\n\n")


def test_reader_does_not_allocate_on_huge_claim():
    """Rejection must happen from the length prefix alone, before any attempt
    to buffer the claimed size."""
    reader = FrameReader()
    with pytest.raises(ProtocolError):
        reader.feed(struct.pack("<I", 0xFFFFFFFF))
    # 4 bytes in, nothing buffered beyond them.
    assert len(reader._buf) <= 4


# --- constructors -----------------------------------------------------------


def test_hello_carries_version_and_cap():
    f = hello("s3cret")
    assert f.type == "HELLO"
    assert f.headers["ver"] == str(PROTO_VERSION)
    assert int(f.headers["max_frame"]) == MAX_FRAME


def test_hello_sess_is_optional():
    assert "sess" not in hello("x").headers
    f = hello("x", sess="abc")
    assert f.headers["sess"] == "abc"


def test_error_frame_has_its_own_type():
    f = roundtrip(error("bad_version", "guest speaks 2, bridge speaks 1"))
    assert f.type == "ERROR"
    assert f.headers["code"] == "bad_version"


# --- non-ASCII header values -----------------------------------------------
#
# Not hypothetical. SESSION frames carry session titles, which are arbitrary
# user text; the first real chat list sent to the Win98 guest crashed the
# bridge with UnicodeEncodeError on every reconnect, so the client could never
# populate its chat list.


def test_non_ascii_header_value_survives_encode():
    f = Frame("SESSION", {"title": "café ⑂ naïve"})
    wire = f.encode()
    assert wire.isascii(), "the header section must be ASCII on the wire"


def test_non_ascii_header_round_trips_through_cp1252():
    """Latin-1 characters survive the header encoding unchanged.

    Kept to Latin-1 deliberately. The em dash and euro this test used to carry
    are folded to ASCII now, because Win98's bitmap UI font has no glyphs in
    the CP1252 0x80-0x9F range -- see test_fold.py."""
    f = Frame("SESSION", {"title": "café naïve ½ £5"})
    back = Frame.decode(f.encode()[4:])
    assert back.headers["title"] == "café naïve ½ £5"


def test_header_value_outside_cp1252_is_folded_not_fatal():
    f = Frame("ANCHOR", {"preview": "emoji \U0001f600 and ⑂"})
    back = Frame.decode(f.encode()[4:])
    assert back.headers["preview"].startswith("emoji ")


def test_percent_is_not_double_decoded():
    """A literal '%25' in the value must come back as '%25', not '%'."""
    f = Frame("ANCHOR", {"preview": "100%25 sure"})
    back = Frame.decode(f.encode()[4:])
    assert back.headers["preview"] == "100%25 sure"


def test_control_characters_in_a_header_are_escaped():
    f = Frame("ANCHOR", {"preview": "a\tb\x00c"})
    wire = f.encode()
    assert b"\t" not in wire[4:].split(b"\n\n")[0]
    back = Frame.decode(wire[4:])
    assert back.headers["preview"] == "a\tb\x00c"


def test_a_stray_percent_without_hex_digits_is_left_alone():
    """Decoding must never raise on input the guest may have mangled."""
    f = Frame.decode(b"ANCHOR\npreview=50%\n\n")
    assert f.headers["preview"] == "50%"
