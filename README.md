# Mini-RUBI

Prototipo educativo de reservas distribuidas de camas con tres nodos Python, SQLite local, gRPC/Protocol Buffers y un cliente de consola Node.js.

## Requisitos

- Python 3.10 o superior
- Node.js 18 o superior y npm

## Preparar el entorno (Windows PowerShell)

Abre PowerShell en la carpeta raíz del proyecto. Crea el entorno virtual **una sola vez** e instala las dependencias:

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r server/requirements.txt
New-Item -ItemType Directory -Force server/generated
python -m grpc_tools.protoc -I proto --python_out=server/generated --grpc_python_out=server/generated proto/rubi.proto
Push-Location client
npm install
Pop-Location
```

Para **activar el entorno virtual** en una terminal PowerShell nueva, primero cambia a la carpeta raíz del proyecto. Por ejemplo, si PowerShell inicia en `C:\Windows\system32`, ejecuta:

```powershell
Set-Location "C:\Users\Juan Arevalo\Desktop\gRPC"
.\.venv\Scripts\Activate.ps1
```

La ruta `\.venv\Scripts\Activate.ps1` es relativa a la carpeta actual: no funcionará desde `C:\Windows\system32`. Cuando quede activo, el prompt de PowerShell mostrará `(.venv)` al inicio. Debes activarlo por separado en cada terminal donde ejecutes Python (por ejemplo, en las tres terminales de los nodos y en la terminal de pruebas). No hace falta activarlo en la terminal del cliente Node.js. Para salir del entorno virtual ejecuta `deactivate`.

Si PowerShell impide ejecutar el script de activación por la política de ejecución, permite scripts locales solo durante esa sesión y vuelve a activarlo:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
```

El contrato está en `proto/rubi.proto`. El comando `protoc` genera para Python los módulos que los servidores importan al arrancar. Node.js carga directamente el mismo archivo `.proto`.

## Iniciar los tres nodos

Abre tres terminales PowerShell en la raíz. **En cada una**, ejecuta primero `\.venv\Scripts\Activate.ps1` (el mismo comando del bloque de activación anterior). Confirma que aparece `(.venv)` en el prompt y luego configura y ejecuta un nodo por terminal:

**Nodo A**

```powershell
$env:NODE_ID="node-a"; $env:GRPC_PORT="50051"; $env:DB_PATH="server/data/node-a.sqlite3"; $env:PEERS="node-b=localhost:50052,node-c=localhost:50053"; python server/node.py
```

**Nodo B**

```powershell
$env:NODE_ID="node-b"; $env:GRPC_PORT="50052"; $env:DB_PATH="server/data/node-b.sqlite3"; $env:PEERS="node-a=localhost:50051,node-c=localhost:50053"; python server/node.py
```

**Nodo C**

```powershell
$env:NODE_ID="node-c"; $env:GRPC_PORT="50053"; $env:DB_PATH="server/data/node-c.sqlite3"; $env:PEERS="node-a=localhost:50051,node-b=localhost:50052"; python server/node.py
```

Cada nodo inicializa su propia base SQLite con las mismas nueve camas de demostración. Mantén los tres procesos activos para probar la mayoría.

Al reservar desde el cliente, revisa las tres terminales de los nodos: mostrarán la solicitud, los votos `SÍ`/`NO`, el conteo de mayoría y los mensajes de aprobación y replicación. El nodo coordinador imprime el resultado final; los nodos que votan imprimen su voto y la confirmación de su copia local.

## Ejecutar el cliente

En una cuarta terminal:

```powershell
Push-Location client
npm start
Pop-Location
```

El menú permite consultar camas (opcionalmente por hospital) y reservar por ID. El cliente prueba los nodos configurados en orden si hay un error de transporte. Para cambiar la lista, define `NODES`, por ejemplo: `$env:NODES="localhost:50052,localhost:50053"`.

IDs de ejemplo: `BOG-CENTRAL-UCI-01`, `BOG-CENTRAL-GEN-01`, `BOG-NORTE-GEN-01` y `BOG-SUR-UCI-01`.

## Probar dos clientes reservando la misma cama al mismo tiempo

1. Deja los tres nodos ejecutándose. Para empezar con una cama libre, detén los nodos con `Ctrl+C`, elimina **solo los datos de demostración** y vuelve a iniciar los nodos:

	```powershell
	Remove-Item .\server\data\* -Force
	```

	Al reiniciarse, cada nodo recrea la base con las nueve camas libres. Este borrado restablece todas las reservas locales de demostración.

2. Abre dos terminales PowerShell nuevas en la raíz del proyecto. En la primera ejecuta:

   ```powershell
   $env:NODES="localhost:50051,localhost:50052,localhost:50053"
   Push-Location client
   npm start
   Pop-Location
   ```

3. En la segunda ejecuta, cambiando el orden para que el segundo cliente intente primero con otro nodo coordinador:

   ```powershell
   $env:NODES="localhost:50052,localhost:50051,localhost:50053"
   Push-Location client
   npm start
   Pop-Location
   ```

4. En ambos clientes selecciona `2) Reservar cama` y escribe exactamente el mismo ID, por ejemplo `BOG-CENTRAL-UCI-01`. Envía las dos solicitudes casi al mismo tiempo: pulsa Enter en ambas terminales rápidamente, una después de la otra.

5. Resultado esperado: solo una solicitud debe mostrar `Reserva confirmada por mayoría`; la otra debe ser rechazada porque no obtuvo mayoría o porque la cama ya está reservada. En las terminales de los nodos podrás observar los votos `SÍ`/`NO` y cuál solicitud fue aprobada.

Este ejercicio ilustra la exclusión mutua del prototipo; por las limitaciones del algoritmo simplificado descritas abajo, no constituye una garantía de consenso para producción.

## Pruebas

Con el entorno virtual activo, ejecuta desde la raíz:

```powershell
python -m unittest discover -s server -v
```

Las pruebas cubren la inicialización de camas, exclusión de bloqueos, confirmación idempotente y aplicación de versiones replicadas.

## Flujo y límites conocidos

- El coordinador obtiene un bloqueo local y solicita bloqueos a sus pares. Para tres nodos requiere dos votos y una segunda confirmación durable antes de responder éxito.
- Los bloqueos expiran después de cinco segundos. Después de confirmar, se propaga el estado; los errores de replicación no revierten la reserva y las lecturas pueden quedar temporalmente desactualizadas.
- Cada nodo consulta periódicamente a sus pares mediante `Sincronizar` y compara la versión de cada cama; cuando vuelve a conectarse, puede recuperar versiones más recientes automáticamente.
- La votación con bloqueos es una **simplificación para una demostración académica**, no una implementación de Raft/Paxos. No ofrece garantías formales ante particiones, caídas en momentos críticos o relojes desajustados. No debe usarse con datos clínicos ni en producción.
- No se incluyen TLS, autenticación, autorización, auditoría ni persistencia de un registro de consenso.

Para reiniciar el escenario, detén los nodos y elimina `server/data/`; las camas volverán a su estado inicial al arrancar.