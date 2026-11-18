"""Guards for everything a spec can make yamlreel read or fetch.

A spec is data, but it names files and URLs. These checks keep a spec you did not write from reading
arbitrary files on your machine or reaching internal network services.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from pathlib import Path
from typing import Literal, NamedTuple
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname


class UnsafeInput(ValueError):
    pass


class Unresolvable(UnsafeInput):
    """A host name that did not resolve: still refused, but not a policy decision (a dead name is just a dead name)."""


def resolve_local(path: str | Path, base: Path, roots: list[Path]) -> Path:
    """Resolve `path` (relative to `base`) and require it to sit inside one of `roots`.

    The check is done on the fully resolved path, so symlinks cannot be used to escape."""
    p = Path(path).expanduser()
    p = p if p.is_absolute() else base / p
    real = p.resolve()
    for root in roots:
        try:
            real.relative_to(root.expanduser().resolve())
            return real
        except ValueError:
            continue
    allowed = ", ".join(str(r) for r in roots) or "(none)"
    raise UnsafeInput(
        f"{path}: outside the folders this run may read ({allowed}). "
        "Move the file next to the spec, or pass --allow-path DIR."
    )


def file_url_to_path(url: str) -> str:
    u = urlparse(url)
    if u.netloc not in ("", "localhost"):
        raise UnsafeInput(f"file URL with a host is not allowed: {url}")
    return url2pathname(unquote(u.path))


# ---------- network policy ----------
# Explicit tables, so that Python 3.10-3.12 give the same answer (ipaddress's is_private/is_global tables changed in
# patch releases). `is_global` is only the last step.

METADATA_NAMES = frozenset({
    "metadata", "metadata.google.internal", "metadata.goog", "instance-data", "instance-data.ec2.internal",
    "metadata.tencentyun.com",
})


def _nets(*cidrs: str) -> tuple[ipaddress._BaseNetwork, ...]:
    return tuple(ipaddress.ip_network(c) for c in cidrs)


ALWAYS_DENY = _nets(
    "0.0.0.0/8", "::/128",                                      # "this host"
    "169.254.0.0/16", "fe80::/10",                              # link-local: cloud metadata, ECS credentials
    "fd00:ec2::/32",                                            # AWS IPv6 metadata (inside ULA: before PRIVATE_OPT_IN)
    "fd20:ce::254/128", "fd00:c1::a9fe:a9fe/128",               # GCP and OCI IPv6 metadata (inside ULA)
    "100.100.100.200/32",                                       # Alibaba Cloud metadata (inside CGNAT)
    "168.63.129.16/32",                                         # Azure WireServer / host agent
    "192.0.0.0/24",                                             # IETF assignments, incl. Oracle metadata 192.0.0.192
    "224.0.0.0/4", "ff00::/8", "240.0.0.0/4",                   # multicast, reserved, broadcast
    "::/96", "fec0::/10", "64:ff9b:1::/48",                     # deprecated or local-NAT64 forms
)
PRIVATE_OPT_IN = _nets(
    "127.0.0.0/8", "::1/128", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7",
)
NOT_GLOBAL_EXTRA = _nets(
    "192.0.2.0/24", "198.18.0.0/15", "198.51.100.0/24", "203.0.113.0/24",
    "2001:db8::/32", "3fff::/20", "100::/64", "2001::/23", "5f00::/16",
)
_GLOBAL_UNICAST_V6 = ipaddress.ip_network("2000::/3")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LOOPBACK6 = ipaddress.IPv6Address("::1")
_NUMERIC_LABEL = re.compile(r"[0-9]+|0x[0-9a-f]*")
_DOTTED_QUAD = re.compile(r"(0|[1-9][0-9]{0,2})(\.(0|[1-9][0-9]{0,2})){3}")

Kind = Literal["allow", "private", "deny"]


class Verdict(NamedTuple):
    kind: Kind
    connect: str            # the address to connect to (the embedded IPv4 only for ::ffff:a.b.c.d)
    embeds: str | None      # an IPv4 address found inside a wrapper form, when the verdict is about that address
    always: bool = False    # a "deny" from ALWAYS_DENY (metadata, link-local, reserved) rather than "not public"


def _in(ip: ipaddress._BaseAddress, nets: tuple[ipaddress._BaseNetwork, ...]) -> bool:
    return any(ip.version == n.version and ip in n for n in nets)


def _embedded_v4(ip: ipaddress.IPv6Address) -> ipaddress.IPv4Address | None:
    if ip.ipv4_mapped is not None:
        return ip.ipv4_mapped
    if ip.sixtofour is not None:
        return ip.sixtofour
    if ip.teredo is not None:
        return ip.teredo[1]
    if ip in _NAT64:
        return ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
    return None


def _fmt(ip: ipaddress._BaseAddress) -> str:
    """Stable text for an address (Python 3.13 changed how mapped addresses print)."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return f"::ffff:{ip.ipv4_mapped}"
    return str(ip)


def classify(ip: ipaddress._BaseAddress) -> Verdict:
    """Decide what may be done with one resolved address: "allow", "private" (only with the opt-in flag) or "deny"."""
    if ip == _LOOPBACK6:  # before ALWAYS_DENY: ::1 sits inside the deprecated IPv4-compatible range ::/96
        return Verdict("private", str(ip), None)
    embedded = _embedded_v4(ip) if isinstance(ip, ipaddress.IPv6Address) else None
    target: ipaddress._BaseAddress = ip if embedded is None else embedded
    mapped = isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None
    connect = str(embedded) if mapped else str(ip)  # only ::ffff:a.b.c.d connects to the embedded address
    note = None if embedded is None else str(embedded)
    if _in(target, ALWAYS_DENY):
        return Verdict("deny", connect, note, always=True)
    if _in(target, PRIVATE_OPT_IN):
        return Verdict("private", connect, note)
    if (target.is_global and not _in(target, NOT_GLOBAL_EXTRA)
            and (target.version == 4 or target in _GLOBAL_UNICAST_V6)):
        return Verdict("allow", connect, note)
    return Verdict("deny", connect, note)


def _resolve(host: str) -> list[str]:
    """Every address the system resolver gives for `host` (the seam tests replace; never raises for a bad name)."""
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError):
        return []
    return [str(info[4][0]) for info in infos]


def _ips_for(host: str) -> list[ipaddress._BaseAddress]:
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    out: list[ipaddress._BaseAddress] = []
    for addr in _resolve(host):
        try:
            out.append(ipaddress.ip_address(addr.split("%")[0]))
        except ValueError:
            continue
    return out


def _normalise_host(host: str) -> str:
    """The name that is judged and resolved: no brackets or scope id, IDNA/ASCII, lower case, no trailing dots.

    Fullwidth digits and ideographic full stops therefore get the verdict of their ASCII equivalents. A host with a
    colon must be an IPv6 literal (it never reaches DNS)."""
    name = host.strip("[]")
    if ":" in name:
        addr = name.partition("%")[0]  # the scope id (interface name) is not part of the address
        try:
            return str(ipaddress.IPv6Address(addr))
        except ValueError:
            raise UnsafeInput(f"URL host {host!r} is not a valid IPv6 address") from None
    try:
        name = name.encode("idna").decode("ascii")
    except UnicodeError:
        raise UnsafeInput(f"URL host {host!r} is not a valid host name") from None
    return name.lower().rstrip(".")


def _is_nonstandard_numeric(name: str) -> bool:
    """WHATWG "ends in a number": such a host must be a strict dotted quad, never a form inet_aton would reinterpret."""
    if ":" in name or not _NUMERIC_LABEL.fullmatch(name.rsplit(".", 1)[-1]):
        return False
    if not _DOTTED_QUAD.fullmatch(name):
        return True
    try:
        ipaddress.IPv4Address(name)
    except ValueError:
        return True
    return False


def _is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name.strip("[]"))
    except ValueError:
        return False
    return True


_UNSPECIFIED_V4 = ipaddress.IPv4Address("0.0.0.0")
_UNSPECIFIED_V6 = ipaddress.IPv6Address("::")


def _unspecified(ip: ipaddress._BaseAddress) -> bool:
    """0.0.0.0, :: or the mapped ::ffff:0.0.0.0, decided here rather than by `is_unspecified`, whose answer for the
    mapped form differs between Python patch releases. Only for the DNS-blocker check: refusals use `classify`."""
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        return ip.ipv4_mapped == _UNSPECIFIED_V4
    return ip == _UNSPECIFIED_V4 or ip == _UNSPECIFIED_V6


def pinned_ips(host: str, allow_private: bool) -> list[str]:
    """Resolve `host` once, validate every address, and return the addresses that may be connected to.

    Callers must connect to a returned address, not re-resolve the name (that is the DNS-rebinding gap)."""
    name = _normalise_host(host)
    if name in METADATA_NAMES:
        raise UnsafeInput(f"cloud metadata host refused: {host}")
    if _is_nonstandard_numeric(name):
        raise UnsafeInput(
            f"URL host {host!r} is a non-standard numeric address; write it as a dotted quad such as 127.0.0.1"
        )
    ips = _ips_for(name)
    if not ips:
        raise Unresolvable(f"cannot resolve host: {host}")
    if not _is_ip_literal(name) and all(_unspecified(ip) for ip in ips):
        # DNS ad/tracker blockers (Pi-hole, NextDNS) answer 0.0.0.0 or ::. Still refused and never connected to,
        # but a blocked name is not a policy decision about the page.
        raise Unresolvable(f"host is blocked by DNS (resolves to {_fmt(ips[0])}): {host}")
    out: list[str] = []
    for ip in ips:
        verdict = classify(ip)
        shown = _fmt(ip) + (f" (embeds {verdict.embeds})" if verdict.embeds else "")
        if verdict.kind == "deny":
            if verdict.always:
                raise UnsafeInput(
                    f"{host} resolves to {shown}, a cloud-metadata, link-local or reserved address; always refused"
                )
            raise UnsafeInput(f"{host} resolves to {shown}, which is not a public internet address; refused")
        if verdict.kind == "private" and not allow_private:
            raise UnsafeInput(
                f"{host} resolves to a private or loopback address ({shown}). "
                "To capture a local dev server pass --allow-private-hosts (or set it in your user config)."
            )
        if verdict.connect not in out:
            out.append(verdict.connect)
    return out


def check_http_url(url: str, allow_private: bool) -> None:
    """Allow http(s) URLs. Link-local and cloud-metadata addresses are always refused; loopback and
    private ranges only with allow_private (a local dev server is a normal thing to capture)."""
    try:
        u = urlparse(url)
        hostname = u.hostname
    except ValueError as e:
        raise UnsafeInput(f"not a valid URL ({e}): {url}") from None
    if u.scheme not in ("http", "https"):
        raise UnsafeInput(f"only http(s) URLs can be captured: {url}")
    if not hostname:
        raise UnsafeInput(f"URL has no host: {url}")
    pinned_ips(hostname, allow_private)
