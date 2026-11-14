# PB-012 — Support MVP: revisión y prueba local

Implementación acumulativa sobre SUP-01: backend PR #38 y frontend PR #54,
ambos hacia `develop`. No fusionar ni desplegar como parte de esta entrega.

## Iniciar un entorno ficticio aislado

Requisitos: Docker Compose v2, Node >=22.22.2 y Flutter 3.47.2 (Dart 3.13.2).
Desde los checkouts hermanos `finance-ai-backend` y `finance-ai-frontend`:

```bash
cd finance-ai-backend
git switch codex/sup-01-backend
docker compose -p finance-support-demo -f compose.support-demo.yaml up -d --build
docker compose -p finance-support-demo -f compose.support-demo.yaml ps
# Esperar a que el API esté healthy.
docker compose -p finance-support-demo -f compose.support-demo.yaml exec -T api cat demo-data/credentials.txt
```

El último comando muestra las credenciales ficticias generadas, conservadas con
permisos 0600 en un volumen exclusivo. No copiarlas a PR, capturas, Trello ni logs.
Existen dos clientes y dos agentes activados. El registro administrativo de
provisión está inactivo y no permite iniciar sesión: no hay bypass de MFA.
La invitación consumida es una fixture de desarrollo, no una ruta pública de
provisión. El script rechaza cualquier base distinta de `support_demo_test` y
cualquier entorno distinto de `local`/`test`. Las bases existentes no se usan.

En otra terminal:

```bash
cd finance-ai-frontend/staff-web
git switch codex/sup-01-frontend
npm ci
VITE_FINANCE_AI_API_BASE_URL=http://127.0.0.1:8001 npm run dev -- --host 127.0.0.1 --port 5173
```

Abrir `http://127.0.0.1:5173`. Para PowerShell, establecer primero
`$env:VITE_FINANCE_AI_API_BASE_URL='http://127.0.0.1:8001'` y ejecutar `npm run dev -- --host 127.0.0.1 --port 5173`.

Cliente Flutter, con un emulador Android iniciado:

```bash
cd finance-ai-frontend
flutter pub get --enforce-lockfile
flutter devices
flutter run -d emulator-5554 --dart-define=FINANCE_AI_API_BASE_URL=http://10.0.2.2:8001
# Alternativa en un host Windows:
flutter run -d windows --dart-define=FINANCE_AI_API_BASE_URL=http://127.0.0.1:8001
```

Cambiar `emulator-5554` por el identificador mostrado por `flutter devices`.
En un Android físico conectado por USB: `adb reverse tcp:8001 tcp:8001`, y
`flutter run -d ID --dart-define=FINANCE_AI_API_BASE_URL=http://127.0.0.1:8001`.
El puerto demo está limitado al loopback; PostgreSQL no se publica al host.

Reiniciar **solamente** los datos ficticios (genera contraseñas nuevas):

```bash
cd finance-ai-backend
docker compose -p finance-support-demo -f compose.support-demo.yaml down -v
docker compose -p finance-support-demo -f compose.support-demo.yaml up -d --build
docker compose -p finance-support-demo -f compose.support-demo.yaml exec -T api cat demo-data/credentials.txt
```

Para detener conservando datos: sustituir `down -v` por `stop`.

## Recorrido manual

1. Web: iniciar sesión como `agente@example.test` con la contraseña generada.
   Recargar para verificar restauración de sesión.
2. Abrir «Crear ticket para un cliente». Buscar `cliente@example.test` completo;
   un prefijo o `%` debe fallar. Verificar identificador, correo y estado.
   Completar asunto, categoría y descripción. El ticket nace abierto sin asignar.
3. Reclamar el ticket. Pasa a «En atención» y muestra cliente, conversación e
   historial. En otra ventana privada con `otro-agente@example.test`, intentar
   reclamar el mismo ticket: la API debe impedirlo.
4. Flutter: iniciar sesión como `cliente@example.test`; Ajustes → Ayuda y soporte.
   Abrir el mismo ticket, enviar un mensaje. Web: «Actualizar conversación»;
   responder. Flutter: «Actualizar» para ver la respuesta. No hay tiempo real.
5. Comprobar que los metadatos del cliente solo incluyen identificador, correo
   y activo/inactivo. No aparecen nombre/suscripción ficticios ni datos financieros.
6. Escalar ese ticket con motivo obligatorio. Se conserva el historial y queda
   en solo lectura para soporte y cliente. Flutter no recibe el motivo interno.
   La atención administrativa posterior depende de ADM-05/SEC-01; este MVP no
   ofrece un inicio de sesión administrativo alternativo.
7. Crear/reclamar otro ticket y resolver con resumen obligatorio. Comprobar el
   evento de resolución y la imposibilidad de enviar más mensajes o escalarlo.
8. Flutter: actualizar y verificar estado «Resuelto», resumen y mensajes previos.
   Crear además una solicitud desde «Crear solicitud» para verificar USR-SUP-01.
9. Otro cliente no ve los tickets anteriores. Otro agente recibe 404 al pedir
   su conversación por ID. Sin sesión se rechazan los endpoints; una sesión
   cerrada/revocada o cuenta inactiva no permite continuar. Intentar enviar
   autor/cliente/fecha en los payloads devuelve 422. Mensajes vacíos, texto con
   controles inválidos y más de 4000 caracteres se rechazan. Un ticket resuelto
   devuelve 409 ante nuevas respuestas. El backend decide todos los permisos.

## Contrato y decisiones

- Se conserva SUP-01: cola global, reclamar solo `open` sin asignar, transición
  atómica a `in_progress`, sin reasignación general.
- Búsqueda exacta por correo normalizado o ID, vía POST para evitar identificadores
  en URLs. Creación del agente vuelve a verificar la identidad en el servidor.
- Clientes: `/api/v1/support/tickets` (GET devuelve `{tickets: [...]}`, POST crea),
  `/{id}` devuelve `{ticket, messages}`, `/{id}/messages` acepta `{body}`.
  La propiedad proviene exclusivamente de la sesión opaca de cliente.
- Staff: `/api/v1/staff/support/tickets/customer-lookup` acepta `{identifier}`;
  POST de tickets añade `customer_identifier` al contrato de creación;
  `/{id}/conversation` devuelve ticket, mensajes, cliente e historial autorizado;
  `/{id}/messages`, `/{id}/escalate` y `/{id}/resolve` escriben. Las transiciones
  aceptan `{summary}`. Se reutilizan las cookies y el límite de origen STAFF-01.
- Asunto: 1–160 caracteres. Descripción, mensaje, motivo y resolución: 1–4000,
  recortados y no blancos. Categorías técnicas iniciales: `general`, `account`,
  `technical`, `other`. Entradas desconocidas y autores/fechas inyectados se rechazan.
  Identificadores de ruta positivos dentro de bigint. Los textos se renderizan
  literalmente. La aplicación no usa HTML suministrado por usuarios.
- Límite de nuevas operaciones de soporte: 120 solicitudes/minuto por peer,
  en memoria del proceso, siguiendo la convención existente. Para despliegues
  con varios workers hace falta el control compartido del perímetro.
- Mensajes del agente solo en su ticket `in_progress`; clientes solo en propios
  `open`/`in_progress`. Resolución y escalamiento solo por el agente asignado
  desde `in_progress`. Escalado/resuelto son de solo lectura para soporte/cliente.
- ADM-05 más reciente prevalece sobre la documentación histórica: al escalar se
  persiste `originating_support_agent_id`, motivo interno y evento con actor/fecha;
  el agente original conserva lectura aunque la asignación administrativa cambie.
  Contrato de integración ADM-05: seleccionar `status=escalated`, reclamar bajo
  bloqueo del ticket conservando origen, y aplicar su retorno atómico documentado.
  No se concede al Support Agent ninguna operación administrativa. Los endpoints
  administrativos y su MFA se entregan en las historias administrativas, no aquí.
- No cambia el esquema: todas las columnas/índices proceden de la migración 0018.
  Auditoría, actividad y mutación comparten transacción. Las transiciones y envíos
  bloquean el mismo ticket en PostgreSQL; no hay estado terminal con respuestas
  confirmadas posteriormente. El historial no se edita ni se elimina por API.
- La proyección cliente nunca serializa actividades, motivos internos ni metadatos
  administrativos. Auditoría de seguridad solo incluye IDs, acción, resultado,
  timestamp y correlación; nunca textos ni correo. No se fabrican datos de perfil.

Las reglas mínimas del solicitante (2026-11-14) son instrucciones de implementación,
no aceptación histórica del PO. Falta validación funcional del PO para las políticas
nuevas y las categorías/límites técnicos elegidos. No se marca Trello como terminado.

## Comandos de verificación

```bash
# Backend: base desechable cuyo nombre termina en _test; nunca producción.
uv sync --frozen
uv run ruff format --check .
uv run ruff check .
uv run mypy src
FINANCE_AI_DATABASE_URL="$FINANCE_AI_TEST_DATABASE_URL" uv run alembic upgrade head
FINANCE_AI_DATABASE_URL="$FINANCE_AI_TEST_DATABASE_URL" uv run python -m finance_ai.db.seeds
FINANCE_AI_DATABASE_URL="$FINANCE_AI_TEST_DATABASE_URL" uv run alembic check
uv run python scripts/export_openapi.py
uv run pytest --cov=finance_ai --cov-report=term-missing
uv run pip-audit
uv build
# Frontend, desde staff-web:
npm run typecheck
npm run lint
npm run test:coverage
npm run build
npm run test:e2e
# Flutter, desde finance-ai-frontend:
flutter analyze --no-pub
flutter test --no-pub --coverage
# En Windows, además:
pwsh -File tool/verify_shared.ps1
pwsh -File tool/build_android.ps1
pwsh -File tool/test_windows_and_restore.ps1
```

Para la prueba de navegador sin stubs `e2e/supportReal.spec.ts`, iniciar la demo,
configurar `VITE_FINANCE_AI_API_BASE_URL=http://127.0.0.1:8001` y establecer
`SUPPORT_DEMO_PASSWORD` con la contraseña local generada antes de ejecutar
`npx playwright test e2e/supportReal.spec.ts`. Esa prueba desactiva trazas para
no guardar credenciales. Por defecto queda omitida. El resto de E2E usa fixtures.
