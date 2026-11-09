const path = require('node:path');
const { randomUUID } = require('node:crypto');
const readline = require('node:readline/promises');
const { stdin: input, stdout: output } = require('node:process');
const grpc = require('@grpc/grpc-js');
const protoLoader = require('@grpc/proto-loader');

const protoPath = path.resolve(__dirname, '../proto/rubi.proto');
const definition = protoLoader.loadSync(protoPath, {
  keepCase: true,
  longs: Number,
  enums: String,
  defaults: true,
  oneofs: true,
});
const rubi = grpc.loadPackageDefinition(definition).rubi;

const addresses = (process.env.NODES || 'localhost:50051,localhost:50052,localhost:50053')
  .split(',')
  .map((address) => address.trim())
  .filter(Boolean);
const clients = addresses.map((address) => ({
  address,
  client: new rubi.NodoRubi(address, grpc.credentials.createInsecure()),
}));

function invoke(client, method, request) {
  return new Promise((resolve, reject) => {
    client[method](request, { deadline: Date.now() + 3000 }, (error, response) => {
      if (error) reject(error);
      else resolve(response);
    });
  });
}

async function callWithFailover(method, request) {
  let lastError;
  for (const entry of clients) {
    try {
      return { response: await invoke(entry.client, method, request), address: entry.address };
    } catch (error) {
      lastError = error;
      console.log(`Nodo ${entry.address} no disponible; probando el siguiente...`);
    }
  }
  throw lastError || new Error('No hay nodos configurados.');
}

function showBeds(beds) {
  if (!beds.length) {
    console.log('No se encontraron camas.');
    return;
  }
  console.table(beds.map((bed) => ({
    ID: bed.id,
    Hospital: bed.hospital,
    Tipo: bed.tipo,
    Estado: bed.estado,
    Versión: bed.version,
  })));
}

async function main() {
  const rl = readline.createInterface({ input, output });
  console.log('Mini-RUBI · Cliente de consola');
  console.log(`Nodos configurados: ${addresses.join(', ')}`);

  try {
    while (true) {
      console.log('\n1) Consultar camas  2) Reservar cama  0) Salir');
      const option = (await rl.question('Opción: ')).trim();
      if (option === '0') break;

      if (option === '1') {
        const hospital = (await rl.question('Hospital (Enter para todos): ')).trim();
        const { response, address } = await callWithFailover('consultarCamas', { hospital });
        console.log(`Vista obtenida desde ${address}`);
        showBeds(response.camas);
      } else if (option === '2') {
        const cama_id = (await rl.question('ID de cama: ')).trim();
        if (!cama_id) {
          console.log('Debe indicar un ID de cama.');
          continue;
        }
        const { response, address } = await callWithFailover('reservarCama', {
          cama_id,
          request_id: randomUUID(),
        });
        console.log(`[${address}] ${response.mensaje}`);
        if (response.cama && response.cama.id) showBeds([response.cama]);
      } else {
        console.log('Opción no reconocida.');
      }
    }
  } catch (error) {
    console.error(`Error: ${error.message}`);
  } finally {
    rl.close();
    for (const entry of clients) entry.client.close();
  }
}

main();