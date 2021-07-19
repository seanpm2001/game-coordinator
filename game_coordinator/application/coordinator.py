import asyncio
import click
import logging
import secrets

from openttd_helpers import click_helper
from openttd_protocol.protocol.coordinator import (
    ConnectionType,
    NetworkCoordinatorErrorType,
)

from .helpers.invite_code import (
    generate_invite_code,
    generate_invite_code_secret,
    validate_invite_code_secret,
)
from .helpers.server import (
    Server,
    ServerExternal,
)
from .helpers.token_connect import TokenConnect
from .helpers.token_verify import TokenVerify

log = logging.getLogger(__name__)

_socks_proxy = None
_shared_secret = None


class Application:
    def __init__(self, database):
        if not _shared_secret:
            raise Exception("Please set --shared-secret for this application")

        log.info("Starting Game Coordinator ...")

        self._shared_secret = _shared_secret
        self.database = database
        self.socks_proxy = _socks_proxy
        self._servers = {}
        self._tokens = {}
        self._newgrf_lookup_table = {}

        self.database.application = self

    async def startup(self):
        await self.database.sync_and_monitor()

    def disconnect(self, source):
        if hasattr(source, "server"):
            asyncio.create_task(self.remove_server(source.server.server_id))

    def delete_token(self, token):
        del self._tokens[token]

    async def newgrf_added(self, index, newgrf):
        self._newgrf_lookup_table[index] = newgrf

    async def remove_newgrf_from_table(self, grfid, md5sum):
        for index, newgrf in self._newgrf_lookup_table.items():
            if newgrf["grfid"] == grfid and newgrf["md5sum"] == md5sum:
                del self._newgrf_lookup_table[index]
                return

    async def update_external_server(self, server_id, info):
        if server_id not in self._servers:
            self._servers[server_id] = ServerExternal(self, server_id)

        if not isinstance(self._servers[server_id], ServerExternal):
            log.error("Internal error: update_external_server() called on a server managed by us")
            return

        await self._servers[server_id].update(info)

    async def update_newgrf_external_server(self, server_id, newgrfs_indexed):
        if server_id not in self._servers:
            self._servers[server_id] = ServerExternal(self, server_id)

        if not isinstance(self._servers[server_id], ServerExternal):
            log.error("Internal error: update_external_server() called on a server managed by us")
            return

        await self._servers[server_id].update_newgrf(newgrfs_indexed)

    async def update_external_direct_ip(self, server_id, type, ip, port):
        if server_id not in self._servers:
            self._servers[server_id] = ServerExternal(self, server_id)

        if not isinstance(self._servers[server_id], ServerExternal):
            log.error("Internal error: update_external_direct_ip() called on a server managed by us")
            return

        await self._servers[server_id].update_direct_ip(type, ip, port)

    async def send_server_stun_request(self, server_id, protocol_version, token):
        if server_id not in self._servers:
            return

        if isinstance(self._servers[server_id], ServerExternal):
            log.error("Internal error: server_stun_request() called on a server NOT managed by us")
            return

        await self._servers[server_id].send_stun_request(protocol_version, token)

    async def send_server_stun_connect(
        self, server_id, protocol_version, token, tracking_number, interface_number, peer_ip, peer_port
    ):
        if server_id not in self._servers:
            return

        if isinstance(self._servers[server_id], ServerExternal):
            log.error("Internal error: server_stun_connect() called on a server NOT managed by us")
            return

        await self._servers[server_id].send_stun_connect(
            protocol_version, token, tracking_number, interface_number, peer_ip, peer_port
        )

    async def send_server_connect_failed(self, server_id, protocol_version, token):
        if server_id not in self._servers:
            return

        if isinstance(self._servers[server_id], ServerExternal):
            log.error("Internal error: server_connect_failed() called on a server NOT managed by us")
            return

        await self._servers[server_id].send_connect_failed(protocol_version, token)

    async def stun_result(self, token, interface_number, peer_type, peer_ip, peer_port):
        prefix = token[0]
        token = self._tokens.get(token[1:])
        if not token:
            return

        await token.stun_result(prefix, interface_number, peer_type, peer_ip, peer_port)

    async def remove_server(self, server_id):
        if server_id not in self._servers:
            return

        asyncio.create_task(self._servers[server_id].disconnect())
        del self._servers[server_id]

    async def receive_PACKET_COORDINATOR_SERVER_REGISTER(
        self, source, protocol_version, game_type, server_port, invite_code, invite_code_secret
    ):
        if (
            invite_code
            and invite_code_secret
            and invite_code[0] == "+"
            and validate_invite_code_secret(self._shared_secret, invite_code, invite_code_secret)
        ):
            # Invite code given is valid, so re-use it.
            server_id = invite_code
        else:
            while True:
                server_id = generate_invite_code(self.database.get_server_id())
                if server_id not in self._servers:
                    break

            invite_code_secret = generate_invite_code_secret(self._shared_secret, server_id)

        source.server = Server(self, server_id, game_type, source, server_port, invite_code_secret)
        self._servers[source.server.server_id] = source.server

        # Find an unused token.
        while True:
            token = secrets.token_hex(16)
            if token not in self._tokens:
                break

        # Create a token to connect server and client.
        token = TokenVerify(self, source, protocol_version, token, source.server)
        self._tokens[token.token] = token

        await token.connect()

    async def receive_PACKET_COORDINATOR_SERVER_UPDATE(
        self, source, protocol_version, newgrf_serialization_type, newgrfs, **info
    ):
        await source.server.update_newgrf(newgrf_serialization_type, newgrfs)
        await source.server.update(info)

    async def receive_PACKET_COORDINATOR_CLIENT_LISTING(
        self, source, protocol_version, game_info_version, openttd_version, newgrf_lookup_table_cursor
    ):
        if protocol_version >= 4 and self._newgrf_lookup_table:
            await source.protocol.send_PACKET_COORDINATOR_GC_NEWGRF_LOOKUP(
                protocol_version, newgrf_lookup_table_cursor, self._newgrf_lookup_table
            )

        # Ensure servers matching "openttd_version" are at the top.
        servers_match = []
        servers_other = []
        for server in self._servers.values():
            # Servers that are not reachable shouldn't be listed.
            if server.connection_type == ConnectionType.CONNECTION_TYPE_ISOLATED:
                continue
            # Server is announced but hasn't finished registration.
            if not server.info:
                continue

            if server.info["openttd_version"] == openttd_version:
                servers_match.append(server)
            else:
                servers_other.append(server)

        await source.protocol.send_PACKET_COORDINATOR_GC_LISTING(
            protocol_version, game_info_version, servers_match + servers_other, self._newgrf_lookup_table
        )
        await self.database.stats_listing(game_info_version)

    async def receive_PACKET_COORDINATOR_CLIENT_CONNECT(self, source, protocol_version, invite_code):
        if not invite_code or invite_code[0] != "+" or invite_code not in self._servers:
            await source.protocol.send_PACKET_COORDINATOR_GC_ERROR(
                protocol_version, NetworkCoordinatorErrorType.NETWORK_COORDINATOR_ERROR_INVALID_INVITE_CODE, invite_code
            )
            source.protocol.transport.close()
            return

        # Find an unused token.
        while True:
            token = secrets.token_hex(16)
            if token not in self._tokens:
                break

        # Create a token to connect server and client.
        token = TokenConnect(self, source, protocol_version, token, self._servers[invite_code])
        self._tokens[token.token] = token

        # Inform client of token value, and start the connection attempt(s).
        await source.protocol.send_PACKET_COORDINATOR_GC_CONNECTING(protocol_version, token.client_token, invite_code)
        await token.connect()

    async def receive_PACKET_COORDINATOR_SERCLI_CONNECT_FAILED(self, source, protocol_version, token, tracking_number):
        token = self._tokens.get(token[1:])
        if token is None:
            # Don't close connection, as this might just be a delayed failure.
            return

        # Client or server noticed the connection attempt failed.
        await token.connect_failed(tracking_number)

    async def receive_PACKET_COORDINATOR_CLIENT_CONNECTED(self, source, protocol_version, token):
        token = self._tokens.get(token[1:])
        if token is None:
            source.protocol.transport.close()
            return

        # Client and server are connected; clean the token.
        await token.connected()
        self.delete_token(token.token)

    async def receive_PACKET_COORDINATOR_SERCLI_STUN_RESULT(
        self, source, protocol_version, token, interface_number, result
    ):
        # This informs us that the client has did his STUN request. We
        # currently take no action on this packet, but it could be used to
        # know there should be a STUN result or to continue with the next
        # available method.
        pass


@click_helper.extend
@click.option("--shared-secret", help="Shared secret to validate invite-code-secrets with")
@click.option(
    "--socks-proxy",
    help="Use a SOCKS proxy to query game servers.",
)
def click_application_coordinator(socks_proxy, shared_secret):
    global _socks_proxy, _shared_secret

    _socks_proxy = socks_proxy
    _shared_secret = shared_secret
