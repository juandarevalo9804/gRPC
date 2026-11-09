"""gRPC server and simplified majority-lock reservation coordinator."""

from __future__ import annotations

import os
import sys
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import grpc

GENERATED_DIR = Path(__file__).parent / "generated"
sys.path.insert(0, str(GENERATED_DIR))
import rubi_pb2  # noqa: E402
import rubi_pb2_grpc  # noqa: E402

from store import Store  # noqa: E402


def parse_peers(raw: str) -> dict[str, str]:
    peers: dict[str, str] = {}
    for item in raw.split(","):
        if "=" in item:
            peer_id, address = item.split("=", 1)
            if peer_id.strip() and address.strip():
                peers[peer_id.strip()] = address.strip()
    return peers


class NodoRubiService(rubi_pb2_grpc.NodoRubiServicer):
    def __init__(self, node_id: str, store: Store, peers: dict[str, str]) -> None:
        self.node_id = node_id
        self.store = store
        self.peers = peers
        self.channels = {peer_id: grpc.insecure_channel(address) for peer_id, address in peers.items()}
        self.stubs = {
            peer_id: rubi_pb2_grpc.NodoRubiStub(channel)
            for peer_id, channel in self.channels.items()
        }

    def _log(self, message: str) -> None:
        print(f"[{self.node_id}] {message}", flush=True)

    @staticmethod
    def _as_proto(row: dict) -> rubi_pb2.Cama:
        return rubi_pb2.Cama(
            id=row["id"], hospital=row["hospital"], tipo=row["type"],
            estado=row["status"], version=row["version"],
        )

    def ConsultarCamas(self, request, context):
        return rubi_pb2.ListaCamas(camas=[self._as_proto(row) for row in self.store.list_beds(request.hospital)])

    def PedirBloqueo(self, request, context):
        granted, message = self.store.try_lock(
            request.cama_id, request.coordinador_id, request.request_id, request.lease_ms
        )
        vote = "SÍ" if granted else "NO"
        self._log(
            f"Votación por cama {request.cama_id}: voto {vote} "
            f"para la solicitud de {request.coordinador_id}. {message}"
        )
        return rubi_pb2.BloqueoResponse(concedido=granted, mensaje=message)

    def ConfirmarReserva(self, request, context):
        confirmed = self.store.confirm_reservation(request.cama_id, request.request_id, request.version)
        if confirmed:
            self._log(
                f"Reserva de cama {request.cama_id} aprobada: "
                f"copia guardada con versión {request.version}."
            )
        else:
            self._log(f"No se pudo confirmar la reserva de cama {request.cama_id}.")
        return rubi_pb2.ConfirmarResponse(confirmado=confirmed)

    def LiberarBloqueo(self, request, context):
        released = self.store.release_lock(request.cama_id, request.request_id)
        return rubi_pb2.LiberarResponse(liberado=released)

    def ReplicarCambio(self, request, context):
        cama = request.cama
        applied = self.store.apply_replica({
            "id": cama.id, "hospital": cama.hospital, "tipo": cama.tipo,
            "estado": cama.estado, "version": cama.version,
        })
        return rubi_pb2.CambioResponse(aplicado=applied)

    def Sincronizar(self, request, context):
        camas = [
            row for row in self.store.list_beds()
            if row["version"] > request.versions.get(row["id"], -1)
        ]
        return rubi_pb2.ListaCamas(camas=[self._as_proto(row) for row in camas])

    def synchronize_from_peers(self) -> None:
        versions = {row["id"]: row["version"] for row in self.store.list_beds()}
        request = rubi_pb2.SyncRequest(versions=versions)
        for stub in self.stubs.values():
            try:
                response = stub.Sincronizar(request, timeout=2)
                for cama in response.camas:
                    self.store.apply_replica({
                        "id": cama.id, "hospital": cama.hospital, "tipo": cama.tipo,
                        "estado": cama.estado, "version": cama.version,
                    })
            except grpc.RpcError:
                continue

    def _release_everywhere(self, bed_id: str, request_id: str, lock_holders: list[str]) -> None:
        self.store.release_lock(bed_id, request_id)
        for peer_id in lock_holders:
            try:
                self.stubs[peer_id].LiberarBloqueo(
                    rubi_pb2.LiberarRequest(cama_id=bed_id, request_id=request_id), timeout=1
                )
            except grpc.RpcError:
                pass  # The peer's lease expires automatically.

    def _replicate(self, cama: rubi_pb2.Cama) -> None:
        for stub in self.stubs.values():
            try:
                stub.ReplicarCambio(rubi_pb2.CambioRequest(cama=cama), timeout=1)
            except grpc.RpcError:
                pass  # A returning node can be repaired with Sincronizar.

    def ReservarCama(self, request, context):
        request_id = request.request_id or str(uuid.uuid4())
        self._log(f"Solicitud de reserva recibida para cama {request.cama_id}; iniciando acuerdo con los pares.")
        bed = self.store.get_bed(request.cama_id)
        if bed is None:
            self._log(f"Reserva rechazada: la cama {request.cama_id} no existe.")
            return rubi_pb2.ReservaResponse(exito=False, mensaje="La cama solicitada no existe.")
        if bed["status"] == "RESERVADA":
            if bed["reservation_request"] == request_id:
                return rubi_pb2.ReservaResponse(exito=True, mensaje="Reserva ya confirmada.", cama=self._as_proto(bed))
            self._log(f"Reserva rechazada: la cama {request.cama_id} ya está reservada.")
            return rubi_pb2.ReservaResponse(exito=False, mensaje="La cama ya está reservada.", cama=self._as_proto(bed))

        granted, reason = self.store.try_lock(request.cama_id, self.node_id, request_id, 5000)
        if not granted:
            self._log(f"Voto local NO para cama {request.cama_id}: {reason}")
            return rubi_pb2.ReservaResponse(exito=False, mensaje=reason, cama=self._as_proto(bed))

        lock_holders = [self.node_id]
        self._log(f"Voto local SÍ para cama {request.cama_id}; solicitando votos a {len(self.stubs)} pares.")
        lock_request = rubi_pb2.BloqueoRequest(
            cama_id=request.cama_id, request_id=request_id,
            coordinador_id=self.node_id, lease_ms=5000,
        )
        with ThreadPoolExecutor(max_workers=max(1, len(self.stubs))) as pool:
            futures = {
                pool.submit(stub.PedirBloqueo, lock_request, timeout=1.5): peer_id
                for peer_id, stub in self.stubs.items()
            }
            for future in as_completed(futures):
                peer_id = futures[future]
                try:
                    vote = future.result()
                    if vote.concedido:
                        lock_holders.append(peer_id)
                        self._log(f"{peer_id} votó SÍ por la cama {request.cama_id}.")
                    else:
                        self._log(f"{peer_id} votó NO por la cama {request.cama_id}: {vote.mensaje}")
                except grpc.RpcError:
                    self._log(f"{peer_id} no respondió a la votación por cama {request.cama_id}.")

        quorum = (len(self.peers) + 1) // 2 + 1
        self._log(f"Acuerdo para cama {request.cama_id}: {len(lock_holders)} de {len(self.peers) + 1} votos; se requieren {quorum}.")
        if len(lock_holders) < quorum:
            self._log(f"No hubo mayoría para cama {request.cama_id}; se liberan los bloqueos.")
            self._release_everywhere(request.cama_id, request_id, lock_holders[1:])
            return rubi_pb2.ReservaResponse(exito=False, mensaje="No se obtuvo mayoría; reserva rechazada.", cama=self._as_proto(bed))

        next_version = bed["version"] + 1
        confirmation = rubi_pb2.ConfirmarRequest(
            cama_id=request.cama_id, request_id=request_id, version=next_version
        )
        remote_commits = 0
        with ThreadPoolExecutor(max_workers=max(1, len(lock_holders) - 1)) as pool:
            futures = {
                pool.submit(self.stubs[peer_id].ConfirmarReserva, confirmation, timeout=1.5): peer_id
                for peer_id in lock_holders[1:]
            }
            for future in as_completed(futures):
                try:
                    if future.result().confirmado:
                        remote_commits += 1
                except grpc.RpcError:
                    pass

        # Require a second durable copy before acknowledging the reservation.
        if remote_commits < 1 or not self.store.confirm_reservation(request.cama_id, request_id, next_version):
            self._log(f"No se alcanzó confirmación durable en mayoría para cama {request.cama_id}; reserva rechazada.")
            self._release_everywhere(request.cama_id, request_id, lock_holders[1:])
            return rubi_pb2.ReservaResponse(exito=False, mensaje="No se pudo confirmar la reserva en mayoría.")

        committed = self.store.get_bed(request.cama_id)
        cama = self._as_proto(committed)
        self._replicate(cama)
        self._log(f"MAYORÍA APROBÓ la reserva de cama {request.cama_id}; versión {cama.version} replicada.")
        return rubi_pb2.ReservaResponse(exito=True, mensaje="Reserva confirmada por mayoría.", cama=cama)


def main() -> None:
    node_id = os.getenv("NODE_ID", "node-a")
    port = int(os.getenv("GRPC_PORT", "50051"))
    db_path = os.getenv("DB_PATH", f"data/{node_id}.sqlite3")
    peers = parse_peers(os.getenv("PEERS", ""))
    server = grpc.server(ThreadPoolExecutor(max_workers=16))
    service = NodoRubiService(node_id, Store(db_path), peers)
    rubi_pb2_grpc.add_NodoRubiServicer_to_server(
        service, server
    )
    server.add_insecure_port(f"0.0.0.0:{port}")
    server.start()
    print(f"[{node_id}] gRPC escuchando en el puerto {port}; BD: {db_path}", flush=True)
    stopping = threading.Event()

    def synchronize_periodically() -> None:
        while not stopping.is_set():
            service.synchronize_from_peers()
            stopping.wait(10)

    sync_thread = threading.Thread(target=synchronize_periodically, daemon=True)
    sync_thread.start()
    try:
        server.wait_for_termination()
    except KeyboardInterrupt:
        stopping.set()
        server.stop(grace=2).wait()


if __name__ == "__main__":
    main()