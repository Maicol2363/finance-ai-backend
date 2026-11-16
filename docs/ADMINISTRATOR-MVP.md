# PB-014 / PB-015 — Administrador (ADM-01–ADM-05) y seguridad compartida (SEC-01, SEC-02)

Ramas de trabajo (sin fusionar): `feat/admin-adm-sec-backend` y `feat/admin-adm-sec-frontend`, creadas
desde `feat/security-auditor-aud` porque el TOTP de SEC-01 ya existía allí. Requisitos fuente:
`NullCorvus/finance-ai` (`docs/BACKLOG-AND-STORIES.md`, `administrator-role.md`,
`shared-security-requirements.md`). Documento de rutinas de base de datos:
`NullCorvus/finance-ai/aidlc-docs/construction/administrator/code/DB-ROUTINES-ADMIN.md`.

## Qué incluye

| Historia | Backend (FastAPI, prefijo `/api/v1/staff/admin`) | Staff web (React) |
|---|---|---|
| ADM-01 | `POST /accounts/lookup` (cuerpo `{"email": …}` o `{"id": …}`), `GET /accounts/{id}` | Cuentas: búsqueda exacta y ficha de la cuenta de acceso |
| ADM-02 | `POST /accounts/{id}/suspend`, `POST /accounts/{id}/reactivate` | Cuentas: suspender (motivo + confirmación) y reactivar |
| ADM-03 | `POST /staff/invitations`, público `POST /staff/auth/activate` | Personal: invitación con enlace de un solo uso; página `/activate` |
| ADM-04 | `GET /staff/{id}`, `POST /staff/{id}/role` | Personal: cambio entre agente de soporte y auditor |
| ADM-05 | `GET /tickets/escalated`, `GET /tickets/{id}`, `POST …/claim`, `…/messages`, `…/resolve`, `…/return` | Casos escalados: cola, contexto, reclamar, responder, resolver, devolver |
| SEC-01 | `POST /staff/auth/mfa/recovery-codes`, `POST /staff/auth/mfa/reset` (+ MFA existente) | Seguridad: códigos de recuperación; página `/recover` |
| SEC-02 | Eventos nuevos en el escritor de auditoría (ver más abajo) | — |

## Control de roles y permisos

Cada ruta exige un administrador **activo**, con sesión **verificada con MFA** y con un autenticador
**activo**; se vuelve a comprobar en cada solicitud (`get_admin_user`). Ocultar rutas en React no
concede ni quita autoridad.

| Operación | user | support_agent | security_auditor | admin sin MFA | admin con MFA |
|---|---|---|---|---|---|
| Buscar/ver cuentas | ✗ | ✗ | ✗ | ✗ | ✓ acotado |
| Suspender/reactivar | ✗ | ✗ | ✗ | ✗ | ✓ no a sí mismo ni al último admin |
| Crear personal | ✗ | ✗ | ✗ | ✗ | ✓ solo support_agent/security_auditor |
| Cambiar rol | ✗ | ✗ | ✗ | ✗ | ✓ no el propio, no admin, no clientes |
| Casos escalados | ✗ | solo lectura de los suyos | ✗ | ✗ | ✓ reclamar, responder, resolver, devolver |

El correo de búsqueda viaja en el **cuerpo** (POST), nunca en la URL, para que no quede en los registros
de acceso de nginx ni en el historial del navegador.

La activación de una invitación comprueba primero la política de contraseña (que no depende del token) y solo
después el token: cualquier fallo que dependa del token (desconocido, mal formado, vencido, ya usado) es un
único mensaje genérico y una contraseña rechazada nunca revela si el token es válido.

Un rol no permitido recibe 403 `staff_access_denied` y se registra `access.denied`. Un campo `role`
forzado (por ejemplo `admin`) falla en validación: los modelos usan `extra="forbid"` y el rol es un
`Literal` de dos valores.

## Aprovisionar el primer administrador (no existe ruta en el portal)

```bash
uv run python scripts/admin_ops.py create-admin --email persona@example.com
uv run python scripts/admin_ops.py promote --email cuenta-existente@example.com
uv run python scripts/admin_ops.py reset-mfa --email persona@example.com   # tras verificar identidad por otro canal
```

La contraseña se pide por consola (nunca por argumentos ni variables de entorno). El script muestra
la base de datos (sin credenciales) y pide confirmación (`--yes` la omite). Un administrador no puede
usar ninguna función administrativa hasta completar el MFA; al promover una cuenta se revocan sus
sesiones y autenticadores previos.

## MFA, sesiones y recuperación

* Contraseña + TOTP; secretos cifrados con AES-256-GCM (`FINANCE_AI_STAFF_MFA_ENCRYPTION_KEY`); sin esa
  clave el inicio de sesión de administradores y auditores falla cerrado.
* La activación de una invitación (`/activate`) y la recuperación (`/mfa/reset`, `/mfa/recovery-codes`)
  tienen cada una su propio cubo de limitación de tasa (10 por minuto por origen de red), distinto del
  de verificación MFA, para que el abuso de uno no bloquee los otros.
* Sesión de administrador: 10 min de acceso, 15 min de inactividad, 4 h absolutas (configurable).
* Una sesión de personal recuerda el rol con el que se emitió; si el rol cambia, deja de valer.
* 10 códigos de recuperación de un solo uso (80 bits, guardados con hash). `POST /mfa/reset` verifica la
  contraseña más un código de recuperación o el código actual del autenticador; después desactiva los
  autenticadores, invalida códigos y sesiones y abre un nuevo reto de enrolamiento.

## Rutinas de base de datos (migración `0020_admin_account_management`)

| Objeto | Tipo | Función |
|---|---|---|
| `app_user_last_admin_guard` | Trigger T1 | Nunca deja el sistema sin administrador activo, ni con operaciones concurrentes |
| `app_user_revoke_sessions_on_authority_change` | Trigger T2 | Revoca las sesiones vivas al suspender o cambiar el rol |
| `sp_admin_set_account_suspension` | Procedimiento P1 | Suspender/reactivar de forma atómica con historial; al suspender a un administrador libera a la cola los casos escalados que tenía reclamados (una fila `ticket_admin_released` por caso) |
| `sp_admin_return_escalated_ticket` | Procedimiento P2 | Devolver un caso escalado a soporte de forma atómica |

Definiciones completas, análisis de concurrencia y pruebas: `aidlc-docs/construction/administrator/code/DB-ROUTINES-ADMIN.md` en
`NullCorvus/finance-ai`.

## Eventos SEC-02 añadidos

`account.suspended`, `account.reactivated`, `staff.created`, `staff.activated`, `role.changed`,
`auth.mfa_recovered`, `auth.mfa_replaced`, `support.ticket_returned`. La revocación de sesiones la hace el
trigger T2 y no genera un evento propio (el evento del cambio que la causa ya la deja registrada). Cada uno con una
lista cerrada de claves de contexto; nunca se registran contraseñas, tokens, códigos, motivos libres ni
texto de conversaciones. Los cambios críticos y su evento se confirman en una sola transacción: si la
auditoría falla, el cambio se revierte con un 503 seguro.

## Configuración nueva (`.env`)

| Variable | Valor por defecto | Uso |
|---|---|---|
| `FINANCE_AI_STAFF_ADMIN_ACCESS_MINUTES` | 10 | Vida del acceso del administrador |
| `FINANCE_AI_STAFF_ADMIN_IDLE_MINUTES` | 15 | Inactividad máxima |
| `FINANCE_AI_STAFF_ADMIN_ABSOLUTE_HOURS` | 4 | Vida absoluta de la sesión |
| `FINANCE_AI_STAFF_INVITATION_HOURS` | 48 | Vigencia del enlace de activación |

## Probar localmente sin Docker (PostgreSQL ya instalado)

```powershell
# 1. Base aislada que termine en _test o una exclusiva para la prueba
$env:FINANCE_AI_ENVIRONMENT = 'local'
$env:FINANCE_AI_DATABASE_URL = 'postgresql+psycopg://USUARIO:CLAVE@127.0.0.1:5432/finance_ai_admin_demo'
$env:FINANCE_AI_CORS_ALLOWED_ORIGINS = '["http://127.0.0.1:5173"]'
$env:FINANCE_AI_STAFF_LOCAL_COOKIE_EXCEPTION_ENABLED = 'true'
$env:FINANCE_AI_STAFF_MFA_ENCRYPTION_KEY = (uv run python -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())")

uv run alembic upgrade head
uv run python -m finance_ai.db.seeds
uv run python scripts/admin_ops.py create-admin --email owner@example.com
uv run uvicorn finance_ai.main:app --host 127.0.0.1 --port 8000
```

En otra terminal: `cd staff-web; npm ci; npm run dev -- --host 127.0.0.1 --port 5173`, abrir
`http://127.0.0.1:5173`, iniciar sesión con el administrador y escanear el QR con una app TOTP.

## Datos de demostración (solo local)

`scripts/seed_admin_demo.py` prepara una base **desechable** para recorrer el flujo completo. Se niega a
correr fuera de `local`/`test`, exige que la base se llame `admin_demo_test` y no se ejecuta dos veces
sobre la misma base. Crea dos administradores (`admin1@example.com`, `admin2@example.com`), dos agentes
(`agente1`, `agente2`), dos clientes (`cliente1`, `cliente2`) y cinco tickets (1 abierto, 3 escalados,
1 resuelto). La contraseña se genera al azar y se escribe en un archivo de permisos 0600 cuya ruta se
indica con `ADMIN_DEMO_CREDENTIALS`; nunca se imprime ni se guarda en el repositorio. Los
administradores se enrolan con TOTP en el primer inicio de sesión.

```powershell
$env:FINANCE_AI_ENVIRONMENT = 'local'
$env:FINANCE_AI_DATABASE_URL = 'postgresql+psycopg://USUARIO:CLAVE@127.0.0.1:5432/admin_demo_test'
$env:ADMIN_DEMO_CREDENTIALS = "$env:TEMP\admin-demo-credentials.txt"
uv run alembic upgrade head
uv run python scripts/seed_admin_demo.py
```

## Verificación en pila real

Además de las pruebas automáticas, el flujo se comprobó contra el servidor y PostgreSQL reales:

* `staff-web/e2e/adminReal.spec.ts` (Playwright con Chrome del sistema, en serie, 10 recorridos) usa
  el servidor y PostgreSQL reales y **dos administradores reales** en contextos separados: inscripción
  TOTP y rechazo de un código repetido, búsqueda de cuentas, suspensión de un cliente y bloqueo
  inmediato, invitación y activación de un solo uso (el token sale de la barra de direcciones), carrera
  por reclamar un caso (gana exactamente uno), respuesta del titular del caso y resolución sin que el
  cliente vea los motivos internos, devolución a soporte, códigos de recuperación, recuperación y
  reemplazo del autenticador, permisos por rol y suspensión mutua simultánea. Solo corre con
  `ADMIN_DEMO_CREDENTIALS` definido; el origen `http://127.0.0.1:4173` debe estar permitido en CORS.
* Tiempos de vida de la sesión (administrador con 1 min de acceso, 5 min de inactividad y 1 h absoluta,
  solo para la prueba): una pestaña activa renueva en segundo plano y sigue dentro (la renovación
  responde 200); si el acceso vence en medio de una acción, la pantalla avisa «Tu sesión se está
  renovando», conserva lo escrito y la acción se repite sin volver a iniciar sesión; tras la
  inactividad el servidor rechaza la renovación (403 `staff_access_denied`) y el API de administración
  (401) aunque se presente la cookie guardada. El límite absoluto de 4 h solo se cubre con pruebas
  unitarias.
* Accesibilidad: auditoría con axe-core (WCAG 2.0/2.1/2.2 A y AA más buenas prácticas) sobre 22
  estados de pantalla en tema claro y oscuro. Encontró contraste insuficiente del texto de error
  (3,55:1) y del texto atenuado (4,36:1), un `article` con `role="listitem"` y un landmark
  complementario anidado; corregidos, la auditoría final da 0 infracciones. La pantalla de soporte
  existente conserva los dos patrones de marcado (no se modificó fuera del alcance de la historia).
* Cliente Flutter en Windows (compilación de depuración contra el mismo backend): inicio de sesión de un
  cliente, Inicio, Ayuda y soporte con sus tickets, detalle del ticket escalado sin el motivo interno,
  respuesta y resolución del administrador visibles tras actualizar, el cliente suspendido vuelve a la
  pantalla de acceso en la siguiente llamada y se le rechaza con el mensaje genérico de credenciales,
  tras reactivar entra con una sesión nueva, y una cuenta de administrador recibe el mismo rechazo
  genérico en la app de clientes. Verificación compartida de Flutter: formato y análisis limpios,
  644 pruebas pasan, 1 falla (comparación de imagen de referencia
  `dashboard_spending_summary_390x844.png`, 3,99 % de píxeles; no hay cambios de código Flutter en
  estas ramas) y cobertura de líneas 86,17 %. Prueba nativa de humo de Windows: 4 de 5 pasan; falla
  `protects Home and clears on Logout` por un toque que no alcanza el botón (arnés existente) y la
  compilación normal se restauró.
* Cliente Flutter en un teléfono Android real (Samsung SM-A315G, Android 12; APK de depuración
  instalado junto a las compilaciones de prueba existentes, puerto del teléfono redirigido por USB al
  backend local con `adb reverse`, sin exponer nada a la red): mismo recorrido que en Windows con el
  mismo resultado (inicio de sesión, Inicio, tickets, respuesta y resolución del administrador sin motivos
  internos, bloqueo inmediato al suspender, rechazo genérico, acceso con sesión nueva tras reactivar y
  rechazo genérico de la cuenta de administrador). Una consulta de soporte lanzada justo cuando la
  pantalla se apagó falló con «No se pudo conectar con soporte» y funcionó al reintentar con la pantalla
  encendida. La aplicación de prueba se desinstaló al terminar.
* Modo producción sobre TLS real (certificado autofirmado local): prefijo `__Host-`, `Secure`,
  `HttpOnly`, `SameSite=Strict`, sin `Domain`, rechazo de HTTP plano, de orígenes fuera de la lista,
  de `Authorization` y de tipos de contenido que no sean JSON, y fallo cerrado sin clave MFA
  (32 de 32 comprobaciones).

## Verificación

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy src
uv run alembic upgrade head && uv run alembic check
uv run python scripts/export_openapi.py && git diff --exit-code -- openapi.json
FINANCE_AI_TEST_DATABASE_URL=postgresql+psycopg://USUARIO:CLAVE@127.0.0.1:5432/finance_ai_test \
  uv run pytest --cov=finance_ai
uv run pip-audit
```

Resultado de la entrega: 932 pruebas pasan, 2 fallan por una causa previa e independiente
(`test_text_capture_persistence`, fecha en el reintento de confirmación), cobertura 92 %; Ruff, mypy,
`alembic check`, deriva de OpenAPI y `pip-audit` sin hallazgos. Staff-web: 254 pruebas, cobertura de
líneas 95,0 %.

El procedimiento P1 bloquea la cuenta del actor y la del objetivo juntas, en orden de `id`, con
`FOR NO KEY UPDATE` (ver `aidlc-docs/construction/administrator/code/DB-ROUTINES-ADMIN.md`). La primera versión bloqueaba solo el objetivo con
`FOR UPDATE` y dos administradores que se suspendían entre sí a la vez provocaban un *deadlock* de
PostgreSQL (503). Lo encontró la ejecución con dos administradores reales; ahora lo cubren pruebas
simultáneas (30 rondas) que fallan con `DeadlockDetected` contra la versión anterior.

## Límites conocidos

* Los tickets en progreso de una persona cuyo rol se cambia no se reasignan automáticamente.
* El enlace de activación se muestra una sola vez al administrador (no hay proveedor de correo aprobado).
* Los limitadores de tasa son en memoria por instancia, como los existentes.
* Una cuenta del personal solo puede tener una sesión viva: iniciar sesión desde otro navegador se
  rechaza con `staff_session_exists` hasta que la otra sesión cierre o venza por inactividad (15 min
  para administradores). Es el comportamiento existente del portal; cerrar sesión en la pestaña libera
  la cuenta.
* El trigger de auditoría bloquea `UPDATE`/`DELETE` por fila; impedir `TRUNCATE`/`DROP` del propietario
  requiere un rol de base de datos sin esos privilegios (despliegue).
