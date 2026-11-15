# PB-014 / PB-015 — Administrador (ADM-01–ADM-05) y seguridad compartida (SEC-01, SEC-02)

Ramas de trabajo (sin fusionar): `feat/admin-adm-sec-backend` y `feat/admin-adm-sec-frontend`, creadas
desde `feat/security-auditor-aud` porque el TOTP de SEC-01 ya existía allí. Requisitos fuente:
`NullCorvus/finance-ai` (`docs/BACKLOG-AND-STORIES.md`, `administrator-role.md`,
`shared-security-requirements.md`). Documento de rutinas de base de datos:
`NullCorvus/finance-ai/docs/DB-ROUTINES-ADMIN.md`.

## Qué incluye

| Historia | Backend (FastAPI, prefijo `/api/v1/staff/admin`) | Staff web (React) |
|---|---|---|
| ADM-01 | `GET /accounts/lookup?email=` o `?id=`, `GET /accounts/{id}` | Cuentas: búsqueda exacta y ficha de la cuenta de acceso |
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
| `sp_admin_set_account_suspension` | Procedimiento P1 | Suspender/reactivar de forma atómica con historial |
| `sp_admin_return_escalated_ticket` | Procedimiento P2 | Devolver un caso escalado a soporte de forma atómica |

Definiciones completas, análisis de concurrencia y pruebas: `docs/DB-ROUTINES-ADMIN.md` en
`NullCorvus/finance-ai`.

## Eventos SEC-02 añadidos

`account.suspended`, `account.reactivated`, `staff.created`, `staff.activated`, `role.changed`,
`auth.mfa_recovered`, `auth.mfa_replaced`, `session.revoked`, `support.ticket_returned`. Cada uno con una
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

## Verificación

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy src
uv run alembic upgrade head && uv run alembic check
uv run python scripts/export_openapi.py && git diff --exit-code -- openapi.json
FINANCE_AI_TEST_DATABASE_URL=postgresql+psycopg://USUARIO:CLAVE@127.0.0.1:5432/finance_ai_test \
  uv run pytest --cov=finance_ai
uv run pip-audit
```

Resultado de la entrega: 913 pruebas pasan, 2 fallan por una causa previa e independiente
(`test_text_capture_persistence`, fecha en el reintento de confirmación), cobertura 92 %.

## Límites conocidos

* Los tickets en progreso de una persona cuyo rol se cambia no se reasignan automáticamente.
* El enlace de activación se muestra una sola vez al administrador (no hay proveedor de correo aprobado).
* Los limitadores de tasa son en memoria por instancia, como los existentes.
* El trigger de auditoría bloquea `UPDATE`/`DELETE` por fila; impedir `TRUNCATE`/`DROP` del propietario
  requiere un rol de base de datos sin esos privilegios (despliegue).
