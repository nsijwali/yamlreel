"""The network guard: which addresses a spec's URLs may reach (see docs/specs/batch1-trust-the-input.md, PR A).

Nothing here resolves a real name or connects to a non-loopback address: `safety._resolve` is replaced by a fake
table, and the proxy tests spy on `socket.create_connection` and fail the test on any non-loopback connect."""
from __future__ import annotations

import http.server
import ipaddress
import socket
import threading
import time
from urllib.parse import urlsplit

import pytest

from yamlreel import safety
from yamlreel.safety import UnsafeInput, check_http_url, pinned_ips

FAKE_DNS: dict[str, list[str]] = {
    "localhost": ["127.0.0.1"],
    "mixed.test": ["93.184.216.34", "100.64.0.1"],
    "meta-mixed.test": ["93.184.216.34", "169.254.169.254"],
    "mapped.test": ["::ffff:100.64.0.1"],
    "dupes.test": ["93.184.216.34", "93.184.216.34"],
    "empty.test": [],
    "blocked.test": ["0.0.0.0"],
    "blocked6.test": ["::"],
    "blockedmap.test": ["::ffff:0.0.0.0"],
    "blockedmany.test": ["0.0.0.0", "::"],
    "halfblocked.test": ["0.0.0.0", "93.184.216.34"],
}


@pytest.fixture(autouse=True)
def fake_dns(monkeypatch):
    def resolve(host: str) -> list[str]:
        assert host in FAKE_DNS, f"unexpected name resolution for {host!r}"
        return list(FAKE_DNS[host])

    monkeypatch.setattr(safety, "_resolve", resolve)


def _teredo(client: str) -> str:
    """A Teredo address (2001::/32) whose obfuscated client field encodes `client`."""
    c = int(ipaddress.IPv4Address(client)) ^ 0xFFFFFFFF
    addr = ipaddress.IPv6Address((0x20010000 << 96) | (0x01020304 << 64) | c)
    assert addr.teredo == (ipaddress.IPv4Address("1.2.3.4"), ipaddress.IPv4Address(client))
    return str(addr)


def _url(host: str) -> str:
    return f"http://[{host}]/" if ":" in host else f"http://{host}/"


REFUSED_WITHOUT_FLAG = [
    "100.64.0.1", "100.100.100.200", "100.127.255.254", "::ffff:100.64.0.1", "::ffff:127.0.0.1",
    "64:ff9b::a9fe:a9fe", "2002:a9fe:a9fe::1", _teredo("10.0.0.1"), "192.0.0.192", "198.18.0.1", "240.0.0.1",
    "2130706433", "0x7f.1", "0177.0.0.1", "127.1",
]


@pytest.mark.parametrize("host", REFUSED_WITHOUT_FLAG)
def test_refused_without_the_private_flag(host):
    with pytest.raises(UnsafeInput):
        check_http_url(_url(host), allow_private=False)


ALWAYS_REFUSED = [
    "169.254.169.254", "169.254.170.2", "fd00:ec2::254", "100.100.100.200", "192.0.0.192",
    "METADATA.GOOGLE.INTERNAL.", "::ffff:169.254.169.254", "64:ff9b::a9fe:a9fe", "0.0.0.0", "::", "224.0.0.1",
    "fec0::1", "ff02::1", "0.1.2.3", "64:ff9b:1::a9fe:a9fe", "instance-data.", "metadata.goog",
    "2002:a9fe:a9fe::1", _teredo("169.254.169.254"), "fd20:ce::254", "fd00:c1::a9fe:a9fe", "168.63.129.16",
    "metadata.tencentyun.com", "\uff4d\uff45\uff54\uff41\uff44\uff41\uff54\uff41.google.internal",
    "\uff11\uff12\uff17\uff0e\uff11",
]
ALWAYS_MESSAGE = r"always refused|cloud metadata host refused|non-standard numeric address"
# Refused with and without the flag, but as "not a public internet address" rather than as metadata/reserved.
NOT_PUBLIC = ["3fff::1", "5f00::1", "4000::1", "200::1", "::ffff:0:a9fe:a9fe", "192.0.2.1", "198.51.100.7",
              "203.0.113.9", "2001:db8::1", "100::1", "2001:1::1", "198.18.0.1"]


@pytest.mark.parametrize("host", ALWAYS_REFUSED)
@pytest.mark.parametrize("flag", [True, False])
def test_refused_even_with_the_private_flag(host, flag):
    with pytest.raises(UnsafeInput, match=ALWAYS_MESSAGE):
        check_http_url(_url(host), allow_private=flag)


@pytest.mark.parametrize("host", NOT_PUBLIC)
@pytest.mark.parametrize("flag", [True, False])
def test_non_public_ranges_are_refused_with_and_without_the_flag(host, flag):
    with pytest.raises(UnsafeInput, match="not a public internet address"):
        check_http_url(_url(host), allow_private=flag)


@pytest.mark.parametrize("flag", [True, False])
def test_verdicts_come_from_the_tables_not_from_is_global(flag, monkeypatch):
    """An interpreter whose is_global says True for everything (older tables) must not change any verdict."""
    monkeypatch.setattr(ipaddress.IPv4Address, "is_global", property(lambda self: True))
    monkeypatch.setattr(ipaddress.IPv6Address, "is_global", property(lambda self: True))
    for host in ALWAYS_REFUSED:
        with pytest.raises(UnsafeInput, match=ALWAYS_MESSAGE):
            check_http_url(_url(host), allow_private=flag)
    for host in NOT_PUBLIC:
        with pytest.raises(UnsafeInput, match="not a public internet address"):
            check_http_url(_url(host), allow_private=flag)
    check_http_url(_url("93.184.216.34"), allow_private=flag)
    if not flag:
        for host in ("100.64.0.1", "10.0.0.1", "127.0.0.1", "fd12:3456::1"):
            with pytest.raises(UnsafeInput, match="--allow-private-hosts"):
                check_http_url(_url(host), allow_private=False)


def test_six_to_four_is_judged_by_the_embedded_ipv4_and_keeps_the_v6_address():
    assert pinned_ips("2002:808:808::1", False) == ["2002:808:808::1"]


def test_unwrapped_wrapper_message_names_the_embedded_address():
    with pytest.raises(UnsafeInput, match=r"\(embeds 169\.254\.169\.254\), a cloud-metadata"):
        check_http_url("http://[2002:a9fe:a9fe::1]/", True)


def test_bad_urls_and_hosts_are_unsafe_input_not_other_exceptions():
    for url in ("http://[::ffff:127.1]/", "http://[abc]/", "http://[::1/", "http://[::g]/", "http://a..b/",
                "http://" + "a" * 70 + ".test/"):
        with pytest.raises(UnsafeInput):
            check_http_url(url, allow_private=True)
    with pytest.raises(UnsafeInput, match="not a valid IPv6 address"):
        pinned_ips("::ffff:127.1", True)


def test_scope_ids_are_stripped_and_judged_as_the_address():
    assert pinned_ips("::1%lo0", True) == ["::1"]
    with pytest.raises(UnsafeInput, match=r"::1%lo0 resolves to a private or loopback address \(::1\).*--allow-private-hosts"):
        pinned_ips("::1%lo0", False)
    with pytest.raises(UnsafeInput, match="always refused"):
        pinned_ips("FE80::1%LO0", True)


@pytest.mark.parametrize("host", ["100.64.0.1", "::ffff:10.0.0.5", "fd12:3456::1", "172.16.0.1", "127.0.0.1",
                                  "localhost", "::1"])
def test_private_ranges_are_allowed_with_the_flag(host):
    check_http_url(_url(host), allow_private=True)


@pytest.mark.parametrize("host", ["93.184.216.34", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed_without_the_flag(host):
    check_http_url(_url(host), allow_private=False)


def test_mixed_answers_are_judged_as_a_whole():
    with pytest.raises(UnsafeInput, match="--allow-private-hosts"):
        check_http_url("http://mixed.test/", allow_private=False)
    assert pinned_ips("mixed.test", True) == ["93.184.216.34", "100.64.0.1"]
    for flag in (False, True):
        with pytest.raises(UnsafeInput, match="always refused"):
            check_http_url("http://meta-mixed.test/", allow_private=flag)
    with pytest.raises(UnsafeInput, match="--allow-private-hosts"):
        check_http_url("http://mapped.test/", allow_private=False)
    with pytest.raises(UnsafeInput, match="cannot resolve host: empty.test"):
        check_http_url("http://empty.test/", allow_private=True)


def test_pinned_addresses_are_deduplicated_and_mapped_ones_connect_to_the_ipv4():
    assert pinned_ips("dupes.test", False) == ["93.184.216.34"]
    assert pinned_ips("::ffff:10.0.0.5", True) == ["10.0.0.5"]
    assert pinned_ips("64:ff9b::808:808", False) == ["64:ff9b::808:808"]  # other wrappers keep the v6 address


def test_messages():
    with pytest.raises(UnsafeInput, match=r"169\.254\.169\.254 .*a cloud-metadata, link-local or reserved address; always refused"):
        check_http_url("http://169.254.169.254/", True)
    with pytest.raises(UnsafeInput, match=r"resolves to ::ffff:169\.254\.169\.254 \(embeds 169\.254\.169\.254\), a cloud-metadata"):
        check_http_url("http://[::ffff:169.254.169.254]/", True)
    with pytest.raises(UnsafeInput, match=r"resolves to 198\.18\.0\.1, which is not a public internet address; refused"):
        check_http_url("http://198.18.0.1/", False)
    with pytest.raises(UnsafeInput, match="cloud metadata host refused: metadata.google.internal."):
        check_http_url("http://metadata.google.internal./", True)
    with pytest.raises(UnsafeInput, match=r"non-standard numeric address; write it as a dotted quad such as 127\.0\.0\.1"):
        check_http_url("http://2130706433/", True)


def test_numeric_forms_are_refused_before_any_resolution(monkeypatch):
    def boom(host):
        raise AssertionError("must not resolve")

    monkeypatch.setattr(safety, "_resolve", boom)
    for host in ("2130706433", "0x7f.1", "0177.0.0.1", "127.1", "0x7f000001", "1.2.3.4.5", "127.0.0.1.", "10.0.0"):
        if host == "127.0.0.1.":  # a trailing dot on a proper dotted quad is the same address
            assert pinned_ips(host, True) == ["127.0.0.1"]
            continue
        with pytest.raises(UnsafeInput, match="non-standard numeric address"):
            pinned_ips(host, True)


# ---------- the pinning proxy enforces the policy on every redirect hop and CONNECT ----------

def _redirecting_server(location: str):
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def _raw(proxy_url: str, request: bytes) -> bytes:
    ph = urlsplit(proxy_url)
    with socket.create_connection((ph.hostname, ph.port), timeout=5) as s:
        s.sendall(request)
        data = b""
        while chunk := s.recv(4096):
            data += chunk
    return data


def _get(proxy_url: str, target: str) -> bytes:
    return _raw(proxy_url, f"GET {target} HTTP/1.1\r\nHost: x\r\n\r\n".encode())


@pytest.fixture
def connect_spy(monkeypatch):
    """Record every address the proxy connects to; a connection to anything but loopback fails the test."""
    seen: list[tuple[str, int]] = []
    real = socket.create_connection

    def spy(addr, *args, **kwargs):
        seen.append((addr[0], addr[1]))
        assert addr[0] in ("127.0.0.1", "::1"), f"test tried to reach {addr[0]}"
        return real(addr, *args, **kwargs)

    monkeypatch.setattr(socket, "create_connection", spy)
    return seen


@pytest.mark.parametrize("location,allow_private", [
    ("http://169.254.169.254/latest/meta-data/", True),
    ("http://100.100.100.200/", True),
    ("http://100.64.0.1/", False),
    ("http://[::ffff:169.254.169.254]/", True),
])
def test_a_redirect_hop_to_a_forbidden_address_gets_403_and_no_connection(location, allow_private, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    srv = _redirecting_server(location)
    try:
        with PinningProxy(allow_private=True) as px_first, PinningProxy(allow_private=allow_private) as px:
            first = _get(px_first.url, f"http://127.0.0.1:{srv.server_address[1]}/")
            assert first.startswith(b"HTTP/1.0 302") or first.startswith(b"HTTP/1.1 302")
            hop = [h for h in first.decode("latin-1").split("\r\n") if h.lower().startswith("location:")][0]
            assert hop.split(":", 1)[1].strip() == location
            resp = _get(px.url, location)  # what the browser does for the next hop
        assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 403")
        forbidden = {"169.254.169.254", "100.100.100.200", "100.64.0.1"}
        assert not forbidden & {a for a, _ in connect_spy}
    finally:
        srv.shutdown()


def test_the_proxy_serves_the_first_hop_then_refuses_the_redirect_target(connect_spy):
    """One proxy with the flag on: loopback is served, the metadata target it redirects to is refused."""
    from yamlreel.pinproxy import PinningProxy
    srv = _redirecting_server("http://169.254.169.254/")
    try:
        with PinningProxy(allow_private=True) as px:
            assert b" 302 " in _get(px.url, f"http://127.0.0.1:{srv.server_address[1]}/").split(b"\r\n")[0] + b" "
            assert _get(px.url, "http://169.254.169.254/").split(b"\r\n")[0].startswith(b"HTTP/1.1 403")
        assert {a for a, _ in connect_spy} == {"127.0.0.1"}
    finally:
        srv.shutdown()


@pytest.mark.parametrize("target,allow_private", [
    ("100.64.0.1:443", False), ("169.254.169.254:443", True), ("[::ffff:100.64.0.1]:443", False),
    ("[fd00:ec2::254]:443", True), ("2130706433:443", True),
])
def test_connect_to_a_forbidden_address_gets_403(target, allow_private, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=allow_private) as px:
        resp = _raw(px.url, f"CONNECT {target} HTTP/1.1\r\nHost: {target}\r\n\r\n".encode())
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 403")
    assert [c for c in connect_spy if c[1] == 443] == []  # the only connection is the test client to the proxy


@pytest.mark.parametrize("target", ["100.64.0.1:+443", "100.64.0.1:4_43", "127.0.0.1:0", "127.0.0.1:65536",
                                    "127.0.0.1:"])
def test_connect_ports_must_be_plain_ascii_digits_in_range(target, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=True) as px:
        resp = _raw(px.url, f"CONNECT {target} HTTP/1.1\r\n\r\n".encode())
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 400")
    assert b"X-Yamlreel-Refused" not in resp
    assert [c for c in connect_spy if c[1] in (0, 443, 65536)] == []


def test_proxy_refusals_carry_the_refused_header(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=False) as px:
        plain = _get(px.url, "http://100.64.0.1/")
        tunnel = _raw(px.url, b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n")
    for resp in (plain, tunnel):
        head = resp.split(b"\r\n\r\n")[0].decode("latin-1").lower().split("\r\n")
        assert head[0].startswith("http/1.1 403")
        assert "x-yamlreel-refused: 1" in head


def _keepalive_upstream(received: list[bytes]):
    """A loopback server that ignores `Connection: close` and answers every request it sees on a connection."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(5)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return

            def one(c=c):
                buf = b""
                while data := c.recv(65536):
                    buf += data
                    while b"\r\n\r\n" in buf:
                        head, _, buf = buf.partition(b"\r\n\r\n")
                        received.append(head.split(b"\r\n")[0])
                        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nhello")

            threading.Thread(target=one, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return srv


@pytest.mark.parametrize("pipelined", [False, True])
def test_a_second_request_on_the_same_proxy_connection_is_never_forwarded(pipelined, connect_spy):
    """The upstream ignores `Connection: close`; the proxy still ends the response and closes the client socket."""
    import time
    from yamlreel.pinproxy import PinningProxy
    received: list[bytes] = []
    up = _keepalive_upstream(received)
    port = up.getsockname()[1]
    first = f"GET http://127.0.0.1:{port}/one HTTP/1.1\r\nHost: x\r\n\r\n".encode()
    second = b"GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\nHost: 169.254.169.254\r\n\r\n"
    try:
        with PinningProxy(allow_private=True) as px:
            ph = urlsplit(px.url)
            t0 = time.monotonic()
            with socket.create_connection((ph.hostname, ph.port), timeout=10) as s:
                s.sendall(first + second if pipelined else first)
                data = b""
                while chunk := s.recv(65536):  # ends by a clean close right after the body, not after 30 s
                    data += chunk
                head = data.split(b"\r\n\r\n")[0].lower()
                assert data.endswith(b"hello") and b"connection: close" in head and b"proxy-connection: close" in head
                assert b"keep-alive" not in head and data.count(b"HTTP/1.1") == 1
                if not pipelined:  # the connection is gone: nothing may be forwarded on it
                    try:
                        s.sendall(second)
                        assert s.recv(65536) == b""
                    except OSError:
                        pass
            assert time.monotonic() - t0 < 5
        assert received == [b"GET /one HTTP/1.1"]  # only the checked request reached the upstream
        assert {a for a, _ in connect_spy} == {"127.0.0.1"}
    finally:
        up.close()


def _one_shot_upstream(response: bytes, close: bool = True):
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        c, _ = srv.accept()
        buf = b""
        while b"\r\n\r\n" not in buf:
            buf += c.recv(65536)
        c.sendall(response)
        if close:
            c.close()
        else:
            time.sleep(8)

    threading.Thread(target=serve, daemon=True).start()
    return srv


@pytest.mark.parametrize("method,response,tail", [
    ("GET", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: keep-alive\r\n\r\n"
            b"5\r\nhello\r\n6\r\n world\r\n0\r\n\r\n", b"0\r\n\r\n"),
    ("GET", b"HTTP/1.0 200 OK\r\nKeep-Alive: timeout=5\r\n\r\nclose-delimited body", b"close-delimited body"),
    ("HEAD", b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\nConnection: keep-alive\r\n\r\n", b"\r\n\r\n"),
    ("GET", b"HTTP/1.1 204 No Content\r\nConnection: keep-alive\r\n\r\n", b"\r\n\r\n"),
    ("GET", b"HTTP/1.1 304 Not Modified\r\nConnection: keep-alive\r\n\r\n", b"\r\n\r\n"),
])
@pytest.mark.parametrize("hold_open", [False, True])
def test_response_framing_survives_the_head_rewrite(method, response, tail, hold_open, connect_spy):
    """Bodies are forwarded as the upstream framed them; with a framed body the proxy closes without waiting for an
    upstream that keeps the connection open (close-delimited bodies legitimately end only when it closes)."""
    import time
    from yamlreel.pinproxy import PinningProxy
    if hold_open and response.startswith(b"HTTP/1.0"):
        pytest.skip("a close-delimited body ends only with the upstream close")
    up = _one_shot_upstream(response, close=not hold_open)
    try:
        with PinningProxy(allow_private=True) as px:
            t0 = time.monotonic()
            got = _raw(px.url, f"{method} http://127.0.0.1:{up.getsockname()[1]}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
            assert time.monotonic() - t0 < 5
        head, _, body = got.partition(b"\r\n\r\n")
        low = head.lower()
        assert b"connection: close" in low and b"proxy-connection: close" in low and b"keep-alive" not in low
        assert body == response.partition(b"\r\n\r\n")[2] if method == "GET" and b"204" not in response else True
        if b"chunked" in response:
            assert b"transfer-encoding: chunked" in low and got.endswith(tail)
        if b"content-length: 99" in low:
            assert b"content-length: 99" in low and body == b""  # HEAD: framing header kept, no body
    finally:
        up.close()


@pytest.mark.parametrize("headers", [
    "Content-Length: abc", "Content-Length: -1", "Content-Length: 1\r\nContent-Length: 2", "Content-Length: 1 2",
])
def test_bad_or_conflicting_content_length_is_a_400(headers, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=True) as px:
        resp = _raw(px.url, f"POST http://127.0.0.1:9/ HTTP/1.1\r\nHost: x\r\n{headers}\r\n\r\nab".encode())
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 400")
    assert [c for c in connect_spy if c[1] == 9] == []


def test_identical_duplicate_content_length_is_accepted(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    up = _one_shot_upstream(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
    try:
        with PinningProxy(allow_private=True) as px:
            resp = _raw(px.url, (f"POST http://127.0.0.1:{up.getsockname()[1]}/ HTTP/1.1\r\nHost: x\r\n"
                                 "Content-Length: 1\r\nContent-Length: 1\r\n\r\nA").encode())
        assert resp.endswith(b"ok")
    finally:
        up.close()


@pytest.mark.parametrize("raw", [
    b"GET http://127.0.0.1:9/ HTTP/1.1\r\nHost: x\nX-Evil: 1\r\n\r\n",
    b"GET http://127.0.0.1:9/ HTTP/1.1\r\nHost: x\rX-Evil: 1\r\n\r\n",
    b"CONNECT 127.0.0.1:9 HTTP/1.1\r\nHost: x\nX: 1\r\n\r\n",
])
def test_a_bare_lf_or_cr_in_the_head_is_a_400(raw, connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=True) as px:
        resp = _raw(px.url, raw)
    assert resp.split(b"\r\n")[0].startswith(b"HTTP/1.1 400")
    assert [c for c in connect_spy if c[1] == 9] == []


def test_the_forwarded_request_asks_for_connection_close_and_keeps_its_body(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    seen: list[bytes] = []
    up = socket.socket()
    up.bind(("127.0.0.1", 0))
    up.listen(1)

    def serve():
        c, _ = up.accept()
        data = b""
        while b"ABCDE" not in data:
            data += c.recv(65536)
        seen.append(data)
        c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        with PinningProxy(allow_private=True) as px:
            resp = _raw(px.url, (f"POST http://127.0.0.1:{up.getsockname()[1]}/p HTTP/1.1\r\nHost: x\r\n"
                                 "Connection: keep-alive\r\nContent-Length: 5\r\n\r\nABCDE").encode())
        assert resp.endswith(b"ok")
        low = seen[0].lower()
        assert b"connection: close" in low and b"proxy-connection: close" in low and b"keep-alive" not in low
        assert seen[0].endswith(b"ABCDE")
    finally:
        up.close()


@pytest.mark.browser
def test_browser_redirect_to_a_mapped_metadata_address_is_a_capture_error(tmp_path):
    """Optional: needs playwright and a Chromium build; skips cleanly otherwise (like the capture test)."""
    pytest.importorskip("playwright")
    from playwright.sync_api import sync_playwright

    from yamlreel.shots import CaptureError, load_shot
    try:
        with sync_playwright() as p:
            p.chromium.launch().close()
    except Exception as e:  # noqa: BLE001 - any launch failure means no usable Chromium here
        pytest.skip(f"chromium unavailable: {e}")
    srv = _redirecting_server("http://[::ffff:169.254.169.254]/")
    try:
        with pytest.raises(CaptureError):
            load_shot(f"http://127.0.0.1:{srv.server_address[1]}/", {"viewport": [400, 300], "scale": 1},
                      tmp_path, tmp_path, [tmp_path], allow_private=True)
    finally:
        srv.shutdown()


# ---------- response relaying: interim answers, trailing bytes, limits, stuck upstreams ----------

def _via(response: bytes, method: str = "GET", close: bool = True) -> bytes:
    from yamlreel.pinproxy import PinningProxy
    up = _one_shot_upstream(response, close=close)
    try:
        with PinningProxy(allow_private=True) as px:
            return _raw(px.url, f"{method} http://127.0.0.1:{up.getsockname()[1]}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    finally:
        up.close()


FINAL = b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: keep-alive\r\n\r\nhello"


@pytest.mark.parametrize("interim", [
    [b"HTTP/1.1 103 Early Hints\r\nLink: </a.css>; rel=preload\r\n\r\n"],
    [b"HTTP/1.1 100 Continue\r\n\r\n"],
    [b"HTTP/1.1 103 Early Hints\r\nLink: </a>\r\n\r\n", b"HTTP/1.1 103 Early Hints\r\nLink: </b>\r\n\r\n"],
])
@pytest.mark.parametrize("hold_open", [False, True])
def test_interim_responses_are_passed_on_unchanged_before_the_final_one(interim, hold_open, connect_spy):
    got = _via(b"".join(interim) + FINAL, close=not hold_open)
    assert got.startswith(b"".join(interim))  # byte for byte: no Connection: close added to a 1xx
    final = got[len(b"".join(interim)):]
    low = final.split(b"\r\n\r\n")[0].lower()
    assert final.startswith(b"HTTP/1.1 200 OK") and final.endswith(b"\r\n\r\nhello")
    assert b"connection: close" in low and b"keep-alive" not in low


def test_interim_head_arriving_in_pieces_is_still_followed_by_the_final_answer(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        c, _ = srv.accept()
        while b"\r\n\r\n" not in c.recv(65536):
            pass
        for piece in (b"HTTP/1.1 103 Early", b" Hints\r\n\r\nHTTP/1.1 200 O", b"K\r\nContent-Length: 2\r\n\r\no", b"k"):
            c.sendall(piece)
            time.sleep(0.05)
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        with PinningProxy(allow_private=True) as px:
            got = _raw(px.url, f"GET http://127.0.0.1:{srv.getsockname()[1]}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
        assert got.startswith(b"HTTP/1.1 103 Early Hints\r\n\r\nHTTP/1.1 200 OK") and got.endswith(b"\r\n\r\nok")
    finally:
        srv.close()


def test_101_is_final_and_never_relayed_as_an_upgrade(connect_spy):
    got = _via(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n\r\nraw websocket bytes")
    head, _, body = got.partition(b"\r\n\r\n")
    assert head.startswith(b"HTTP/1.1 101") and b"Connection: close" in head and b"Connection: Upgrade" not in head
    assert body == b""


def test_bytes_after_a_content_length_body_are_not_forwarded(connect_spy):
    for hold_open in (False, True):
        got = _via(FINAL + b"EXTRA-BYTES", close=not hold_open)
        assert got.endswith(b"\r\n\r\nhello")


@pytest.mark.parametrize("response,expected_end", [
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\nEXTRA-BYTES", b"0\r\n\r\n"),
    (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\nX-T: 1\r\n\r\nEXTRA", b"X-T: 1\r\n\r\n"),
])
def test_bytes_after_the_chunked_terminator_are_not_forwarded(response, expected_end, connect_spy):
    for hold_open in (False, True):
        assert _via(response, close=not hold_open).endswith(expected_end)


def test_chunked_feed_reports_how_many_bytes_belong_to_the_body():
    from yamlreel.pinproxy import _Chunked
    c = _Chunked()
    assert c.feed(b"3\r\nabc\r\n0\r\n\r\nTRAILING") == len(b"3\r\nabc\r\n0\r\n\r\n") and c.done
    c = _Chunked()
    assert c.feed(b"3\r\nab") == 5 and not c.done and c.feed(b"c\r\n0\r\n\r\n") == 8 and c.done


@pytest.mark.parametrize("line", [b"0x2\r\n", b"1_0\r\n", b"-5\r\n", b"\r\n", b"g\r\n", b"+5\r\n", b"11111111111111111\r\n"])
def test_chunk_sizes_must_be_plain_hex(line):
    from yamlreel.pinproxy import _Chunked
    with pytest.raises(ValueError):
        _Chunked().feed(line)


def test_chunk_size_lines_and_extensions_are_accepted_and_the_line_length_is_bounded(monkeypatch):
    from yamlreel import pinproxy
    from yamlreel.pinproxy import _Chunked
    assert _Chunked().feed(b"A;name=value\r\n") == 14
    monkeypatch.setattr(pinproxy, "MAX_HEAD", 64)
    with pytest.raises(ValueError, match="too long"):
        _Chunked().feed(b"5;" + b"x" * 100)  # no newline: bounded while it accumulates
    c = _Chunked()
    with pytest.raises(ValueError, match="too long"):
        c.feed(b"5;" + b"x" * 40)
        c.feed(b"y" * 40 + b"\r\n")  # a line that ends but is over the limit


@pytest.mark.parametrize("response", [
    b"HTTP/1.1 200 OK\nContent-Length: 2\n\nok",                    # head ended only by bare LFs
    b"just some garbage with no head terminator",                    # not HTTP at all
])
def test_a_bad_upstream_head_is_answered_with_502(response, connect_spy):
    assert _via(response).split(b"\r\n")[0].startswith(b"HTTP/1.1 502")


@pytest.mark.parametrize("response", [
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n-5\r\nabc",
    b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0x2\r\nab\r\n",
])
def test_a_broken_chunked_body_truncates_the_answer_without_a_502(response, connect_spy):
    got = _via(response)
    assert got.startswith(b"HTTP/1.1 200 OK") and b"502" not in got


def test_the_response_head_size_limit(monkeypatch, connect_spy):
    from yamlreel import pinproxy
    monkeypatch.setattr(pinproxy, "MAX_HEAD", 200)
    filler = b"X-Filler: " + b"a" * 300 + b"\r\n"
    assert _via(b"HTTP/1.1 200 OK\r\n" + filler).split(b"\r\n")[0].startswith(b"HTTP/1.1 502")            # no terminator
    assert _via(b"HTTP/1.1 200 OK\r\n" + filler + b"\r\nok").split(b"\r\n")[0].startswith(b"HTTP/1.1 502")  # terminator late
    assert _via(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok").endswith(b"ok")                              # small is fine
    t0 = time.monotonic()  # an upstream that keeps the connection open must not be waited for
    assert _via(b"HTTP/1.1 200 OK\r\n" + filler, close=False).split(b"\r\n")[0].startswith(b"HTTP/1.1 502")
    assert time.monotonic() - t0 < 5


def test_the_request_head_size_limit(monkeypatch, connect_spy):
    from yamlreel import pinproxy
    from yamlreel.pinproxy import PinningProxy
    monkeypatch.setattr(pinproxy, "MAX_HEAD", 300)
    with PinningProxy(allow_private=True) as px:
        assert _raw(px.url, b"GET http://127.0.0.1:9/ HTTP/1.1\r\nX: " + b"a" * 1000 + b"\r\n\r\n") == b""
        assert _raw(px.url, b"GET http://127.0.0.1:9/ HTTP/1.1\r\nX: " + b"a" * 1000) == b""  # never terminated, then EOF
    assert [c for c in connect_spy if c[1] == 9] == []


def _handler_threads() -> int:
    return sum("process_request_thread" in t.name for t in threading.enumerate())


def _dripping_upstream():
    """Accepts requests and then sends one header byte every 50 ms for as long as the peer listens."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(50)

    def serve():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return

            def drip(c=c):
                try:
                    c.recv(65536)
                    c.sendall(b"HTTP/1.1 200 OK\r\n")
                    while True:
                        c.sendall(b"X")
                        time.sleep(0.05)
                except OSError:
                    c.close()

            threading.Thread(target=drip, daemon=True).start()

    threading.Thread(target=serve, daemon=True).start()
    return srv


def _wait_for(cond, seconds=8.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.05)
    return cond()


def test_clients_that_leave_do_not_keep_handler_threads_alive(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    up = _dripping_upstream()
    port = up.getsockname()[1]
    try:
        with PinningProxy(allow_private=True) as px:
            base = _handler_threads()
            ph = urlsplit(px.url)
            clients = []
            for _ in range(15):
                s = socket.create_connection((ph.hostname, ph.port), timeout=5)
                s.sendall(f"GET http://127.0.0.1:{port}/ HTTP/1.1\r\nHost: x\r\n\r\n".encode())
                clients.append(s)
            assert _wait_for(lambda: _handler_threads() >= base + 15, 3)  # they are all stuck on the dripping upstream
            for s in clients:
                s.close()
            assert _wait_for(lambda: _handler_threads() <= base), f"{_handler_threads() - base} handler threads left"
    finally:
        up.close()


def test_a_response_has_an_overall_deadline(monkeypatch, connect_spy):
    from yamlreel import pinproxy
    from yamlreel.pinproxy import PinningProxy
    monkeypatch.setattr(pinproxy, "RELAY_DEADLINE", 1.0)
    up = _dripping_upstream()
    try:
        with PinningProxy(allow_private=True) as px:
            t0 = time.monotonic()
            got = _get(px.url, f"http://127.0.0.1:{up.getsockname()[1]}/")  # the client stays connected throughout
            assert time.monotonic() - t0 < 6
            assert got.split(b"\r\n")[0].startswith(b"HTTP/1.1 502")
    finally:
        up.close()


@pytest.mark.parametrize("request_bytes,host", [
    (b"CONNECT 169.254.169.254:443 HTTP/1.1\r\n\r\n", "169.254.169.254"),
    (b"CONNECT 100.64.0.1:443 HTTP/1.1\r\n\r\n", "100.64.0.1"),
    (b"GET http://100.64.0.1/x HTTP/1.1\r\nHost: x\r\n\r\n", "100.64.0.1"),
])
def test_the_proxy_records_every_refusal_itself(request_bytes, host, connect_spy):
    """HTTPS refusals never reach the page as a response, so the capture reads the proxy's own record."""
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=False) as px:
        assert px.refusals == []
        _raw(px.url, request_bytes)
        refusals = px.refusals
    assert len(refusals) == 1 and refusals[0][0] == host and refusals[0][1]


def test_an_allowed_request_records_no_refusal(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    srv = _redirecting_server("http://127.0.0.1/")
    try:
        with PinningProxy(allow_private=True) as px:
            _get(px.url, f"http://127.0.0.1:{srv.server_address[1]}/")
            assert px.refusals == []
    finally:
        srv.shutdown()


def test_the_refused_header_is_stripped_from_upstream_responses(connect_spy):
    """Only the proxy may say "refused": a site that sends the header must not be able to forge it."""
    from yamlreel.pinproxy import PinningProxy

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(403)
            self.send_header("X-Yamlreel-Refused", "1")
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"no")

        def log_message(self, *a): ...

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with PinningProxy(allow_private=True) as px:
            resp = _get(px.url, f"http://127.0.0.1:{srv.server_address[1]}/")
            assert px.refusals == []
    finally:
        srv.shutdown()
    assert b" 403 " in resp.split(b"\r\n")[0] + b" "
    assert b"x-yamlreel-refused" not in resp.lower()


@pytest.mark.parametrize("request_bytes", [
    b"CONNECT empty.test:443 HTTP/1.1\r\n\r\n",
    b"GET http://empty.test/ HTTP/1.1\r\nHost: x\r\n\r\n",
])
def test_an_unresolvable_host_is_refused_with_403_but_not_recorded(request_bytes, connect_spy):
    """A dead name is not a policy decision: the request fails, the capture does not."""
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=False) as px:
        resp = _raw(px.url, request_bytes)
        assert px.refusals == [] and px.refusal_count == 0 and px.first_refusal is None
    assert b" 403 " in resp.split(b"\r\n")[0] + b" "


def test_the_unresolvable_error_is_still_an_unsafe_input():
    from yamlreel.safety import Unresolvable
    with pytest.raises(UnsafeInput) as e:
        pinned_ips("empty.test", False)
    assert isinstance(e.value, Unresolvable)


def test_recorded_refusals_are_bounded_but_counted(connect_spy):
    from yamlreel.pinproxy import MAX_RECORDED_REFUSALS, PinningProxy
    with PinningProxy(allow_private=False) as px:
        for i in range(MAX_RECORDED_REFUSALS + 5):
            px.record_refusal(f"h{i}.test", f"reason {i}")
        assert len(px.refusals) == MAX_RECORDED_REFUSALS
        assert px.refusal_count == MAX_RECORDED_REFUSALS + 5
        assert px.first_refusal == ("h0.test", "reason 0")


def test_the_refused_header_is_stripped_from_interim_responses_too(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)

    def serve():
        c, _ = srv.accept()
        c.recv(65536)
        c.sendall(b"HTTP/1.1 103 Early Hints\r\nX-Yamlreel-Refused: 1\r\nLink: </a.css>\r\n\r\n"
                  b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        c.close()

    threading.Thread(target=serve, daemon=True).start()
    try:
        with PinningProxy(allow_private=True) as px:
            resp = _get(px.url, f"http://127.0.0.1:{srv.getsockname()[1]}/")
    finally:
        srv.close()
    assert b" 103 " in resp and b"Link: </a.css>" in resp and resp.endswith(b"ok")
    assert b"x-yamlreel-refused" not in resp.lower()


@pytest.mark.parametrize("host", ["blocked.test", "blocked6.test", "blockedmap.test", "blockedmany.test"])
def test_a_name_a_dns_blocker_answers_with_unspecified_is_unresolvable_not_a_policy_refusal(host):
    """Pi-hole/NextDNS answer 0.0.0.0 or :: for blocked trackers: refused, but not a policy decision."""
    from yamlreel.safety import Unresolvable
    for allow_private in (False, True):
        with pytest.raises(Unresolvable, match="blocked by DNS"):
            pinned_ips(host, allow_private)


def test_a_mixed_answer_with_an_unspecified_address_keeps_the_policy_refusal():
    from yamlreel.safety import Unresolvable
    with pytest.raises(UnsafeInput) as e:
        pinned_ips("halfblocked.test", True)
    assert not isinstance(e.value, Unresolvable)


def test_an_unspecified_address_literal_stays_a_policy_refusal():
    from yamlreel.safety import Unresolvable
    with pytest.raises(UnsafeInput) as e:
        pinned_ips("0.0.0.0", True)
    assert not isinstance(e.value, Unresolvable)


def test_the_proxy_refuses_a_dns_blocked_host_with_403_and_does_not_record_it(connect_spy):
    from yamlreel.pinproxy import PinningProxy
    with PinningProxy(allow_private=True) as px:
        resp = _get(px.url, "http://blocked.test/")
        assert px.refusal_count == 0
    assert b" 403 " in resp.split(b"\r\n")[0] + b" "
    assert all(a == "127.0.0.1" for a, _ in connect_spy)  # only the test client reached the proxy
