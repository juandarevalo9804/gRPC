import socket
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import grpc

GENERATED_DIR = Path(__file__).parent / "generated"
sys.path.insert(0, str(GENERATED_DIR))
import rubi_pb2  # noqa: E402  # type: ignore[import-not-found]
import rubi_pb2_grpc  # noqa: E402  # type: ignore[import-not-found]

from node import NodoRubiService  # noqa: E402
from store import Store  # noqa: E402


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class GrpcClusterTests(unittest.TestCase):
    def test_majority_reservation_replicates_and_blocks_duplicate(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            ports = {node_id: unused_port() for node_id in ("node-a", "node-b", "node-c")}
            addresses = {node_id: f"127.0.0.1:{port}" for node_id, port in ports.items()}
            servers = []
            services = []
            client_channels = []
            for node_id, port in ports.items():
                peers = {peer_id: address for peer_id, address in addresses.items() if peer_id != node_id}
                service = NodoRubiService(
                    node_id,
                    Store(str(Path(temp_dir) / f"{node_id}.sqlite3")),
                    peers,
                )
                server = grpc.server(ThreadPoolExecutor(max_workers=8))
                rubi_pb2_grpc.add_NodoRubiServicer_to_server(service, server)
                server.add_insecure_port(f"127.0.0.1:{port}")
                server.start()
                services.append(service)
                servers.append(server)

            try:
                client_channel = grpc.insecure_channel(addresses["node-a"])
                client_channels.append(client_channel)
                client = rubi_pb2_grpc.NodoRubiStub(client_channel)
                beds = client.ConsultarCamas(rubi_pb2.ConsultaRequest(), timeout=3).camas
                cama_id = beds[0].id
                result = client.ReservarCama(
                    rubi_pb2.ReservaRequest(cama_id=cama_id, request_id="integration-test"),
                    timeout=5,
                )
                self.assertTrue(result.exito, result.mensaje)

                for service in services:
                    self.assertEqual(service.store.get_bed(cama_id)["status"], "RESERVADA")

                synchronized = client.Sincronizar(rubi_pb2.SyncRequest(), timeout=3)
                synchronized_bed = next(cama for cama in synchronized.camas if cama.id == cama_id)
                self.assertEqual(synchronized_bed.version, 1)

                second_channel = grpc.insecure_channel(addresses["node-b"])
                client_channels.append(second_channel)
                duplicate = rubi_pb2_grpc.NodoRubiStub(second_channel).ReservarCama(
                    rubi_pb2.ReservaRequest(cama_id=cama_id, request_id="second-request"),
                    timeout=3,
                )
                self.assertFalse(duplicate.exito)
            finally:
                for channel in client_channels:
                    channel.close()
                for server in servers:
                    server.stop(grace=0).wait()
                for service in services:
                    for channel in service.channels.values():
                        channel.close()


if __name__ == "__main__":
    unittest.main()