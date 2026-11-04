# modules/execution/lib/universal_resolver.py
# Copyright (c) 2026 sergson (https://github.com/sergson)
# Licensed under GNU General Public License v3.0
# DISCLAIMER: Trading cryptocurrencies involves significant risk.
# This software is for educational purposes only. Use at your own risk.

"""
Universal DNS resolver for all platforms
"""
import socket
import sys
import asyncio
from typing import List, Dict, Any
import aiohttp


class UniversalDNSResolver(aiohttp.resolver.AbstractResolver):
    """Cross-platform DNS resolver for Windows and other OS"""

    def __init__(self, loop: asyncio.AbstractEventLoop = None):
        self._loop = loop or asyncio.get_event_loop()

    async def resolve(
            self,
            hostname: str,
            port: int = 0,
            family: int = socket.AF_INET
    ) -> List[Dict[str, Any]]:
        """Asynchronous DNS resolution"""

        # Use a thread for the blocking call
        infos = await self._loop.run_in_executor(
            None,
            socket.getaddrinfo,
            hostname, port, family, socket.SOCK_STREAM, socket.IPPROTO_TCP, 0
        )

        # Format the result for aiohttp
        result = []
        for fam, typ, proto, canonname, sockaddr in infos:
            result.append({
                'hostname': hostname,
                'host': sockaddr[0],
                'port': sockaddr[1],
                'family': fam,
                'proto': proto,
                'flags': socket.AI_NUMERICHOST,
            })

        return result

    async def close(self) -> None:
        """Cleanup resources"""
        pass


def create_aiohttp_session():
    """Create an aiohttp session.

    On Windows we install UniversalDNSResolver because the default aiodns
    resolver sometimes cannot reach system DNS servers on certain networks
    ('Could not contact DNS servers').

    On Linux/macOS we rely on aiohttp's default resolver: it already uses
    getaddrinfo in a thread executor and honours /etc/resolv.conf via glibc,
    while supporting IPv6 (the custom resolver is IPv4-only).
    """
    timeout = aiohttp.ClientTimeout(total=30, connect=10)

    if sys.platform.startswith('win'):
        resolver = UniversalDNSResolver()
        connector = aiohttp.TCPConnector(
            resolver=resolver,
            use_dns_cache=True,
            ttl_dns_cache=300,
            family=socket.AF_INET,
            limit_per_host=5,
        )
    else:
        connector = aiohttp.TCPConnector(
            use_dns_cache=True,
            ttl_dns_cache=300,
            limit_per_host=5,
        )

    return aiohttp.ClientSession(connector=connector, timeout=timeout)