# PB-016 — Security Auditor (AUD-01–AUD-04): implementación, revisión y prueba local

Rama de trabajo: `feat/security-auditor-aud` en `finance-ai-backend` y `finance-ai-frontend`,
creada desde `develop`. Requisitos fuente: `NullCorvus/finance-ai` (`docs/BACKLOG-AND-STORIES.md`,
`security-auditor-role.md`, `shared-security-requirements.md`).

## Qué incluye

| Historia | Backend (FastAPI) | Staff web (React) |
|---|---|---|
| AUD-01 | `GET /api/v1/staff/audit/events`, `GET /events/{event_id}`, `GET /catalog` | Lista filtrable y paginada, detalle saneado |
| AUD-02 | `GET /api/v1/staff/audit/investigations/{account_id}` | Investigación por identificador histórico |
| AUD-03 | `GET /api/v1/staff/audit/summary` | Métricas, gráficas accesibles y navegación a la lista |
| AUD-04 | `POST /api/v1/staff/audit/reports` (PDF) | Generación y descarga del informe |
| SEC-01 (consumidor Auditor) | `POST /staff/auth/login` → 202 + reto, `/staff/auth/mfa/enrollment`, `/mfa/verify`, `/mfa/cancel` | Paso de verificación en dos pasos con QR |

Todo es de solo lectura sobre la evidencia: no existe API de actualización o borrado de eventos
(la tabla sigue protegida por el trigger *append-only* de la migración 0018).

## Control de roles

- Solo `security_auditor` activo, con activación autorizada y sesión **verificada con MFA** usa
  `/staff/audit/*`. Cada solicitud vuelve a comprobar cuenta activa, rol actual, activación y MFA;
  la suspensión o la pérdida del rol revocan la sesión existente de inmediato.
- `support_agent` y `admin` reciben 403 en Auditoría y se registra `access.denied` (sin contenido de
  la solicitud), aunque el administrador tenga su sesión verificada con MFA: el rol por sí solo niega.
  `security_auditor` recibe 403 en Soporte y en Administración con el mismo registro.
- Fuera de la demo, el auditor se crea con una invitación del Administrador (ADM-03, página
  `/activate`); ver `docs/ADMINISTRATOR-MVP.md`.
- `security_auditor` no puede autenticarse en la API de clientes (login, refresh y resolución de
  sesión): no accede a funciones de usuario Free/Premium ni a datos financieros.
- Una sesión emitida antes de asignar el rol de auditor nunca obtiene autoridad: no tiene marca de MFA.
- En la web cada rol solo ve su espacio; la otra sección muestra “No tienes permisos para acceder a estas funciones” sin pedir
  datos. Es presentación: la autorización real ocurre en el backend.

## MFA (consumidor de SEC-01 para el Auditor)

- La contraseña se verifica primero. Para `security_auditor` el login responde **202** con un reto
  de un solo uso (cookie `HttpOnly`, `Secure`, `SameSite=Strict`, 5 minutos) sin autoridad.
- Enrolamiento: QR generado en el backend (SVG en línea) y clave mostrada una sola vez; el factor
  queda `pending` hasta verificar un código válido. Secreto TOTP cifrado con AES-256-GCM ligado a la
  cuenta; la clave viene de `FINANCE_AI_STAFF_MFA_ENCRYPTION_KEY` (32 bytes base64url). Sin clave, el
  inicio de sesión del auditor falla de forma cerrada (`mfa_unavailable`) y los demás roles no se afectan.
- TOTP RFC 6238 (SHA-1, 6 dígitos, 30 s, ±1 paso). Antirreplay: se guarda el último paso aceptado
  bajo `SELECT … FOR UPDATE`; un código ya usado se rechaza también con solicitudes concurrentes.
- Límites: 5 intentos fallidos por reto (configurable) y 10 intentos/minuto por cliente.
- Eventos SEC-02: `auth.mfa_enrolled`, `auth.mfa_success`, `auth.mfa_failed` y `auth.login_success`
  (solo tras contraseña + MFA). `auth.mfa_success` no se cuenta como inicio de sesión.
- La recuperación y el reemplazo del autenticador (SEC-01 compartido con el administrador) están en
  `staff_auth/recovery.py` y la página `/recover`; sin ninguna prueba de identidad, un operador usa
  `scripts/admin_ops.py reset-mfa`. Ver `docs/ADMINISTRATOR-MVP.md`.

## Reglas de consulta

- Marcas de tiempo en UTC; presentación por defecto `America/Bogota (UTC-05:00)`, rotulada. Fechas
  de calendario → inicio inclusivo y fin exclusivo (medianoche siguiente a la fecha final).
- Máximo 90 días por consulta (`FINANCE_AI_AUDIT_MAX_QUERY_DAYS`), validado tras normalizar a UTC.
- Lista: más reciente primero por `(occurred_at, event_id)`; cursor estable ligado al alcance y a un
  corte de lectura. Investigación: más antiguo primero; coincide actor **o** objetivo tipado
  `account`, una fila por evento, sin consultar el directorio de cuentas.
- Proyección con lista blanca (`reason`, `resource`, metadatos del informe…); nunca contraseñas,
  tokens, secretos MFA, datos financieros ni cuerpos de solicitudes.
- Un fallo de consulta es un error (`audit_query_unavailable`), nunca un resultado vacío.

### Rol del actor (migración 0021)

- Cada evento guarda `actor_role`: el rol que tenía el actor **en el momento del evento**. Lo calcula
  PostgreSQL con un trigger `BEFORE INSERT` a partir de la cuenta del actor; cualquier valor enviado
  por la aplicación se reemplaza, así que no se puede falsear, y un cambio de rol posterior no
  reescribe la historia. Sin actor, con un actor no numérico o sin cuenta, queda nulo.
- Los eventos anteriores a la migración quedan con rol nulo («No registrado»): la tabla es
  *append-only* y no se rellena hacia atrás.
- Filtro `actor_role` (`user`, `support_agent`, `admin`, `security_auditor`) en lista, resumen e
  informe; el catálogo publica `actor_roles`. Los eventos sin rol registrado no coinciden con ningún
  rol. El PDF muestra el rol junto al actor y el filtro aplicado (`filter_actor_role`).

### Inicios de sesión de la app de clientes

La API de clientes (app Flutter en Android y Windows) registra los eventos aprobados de SEC-02:

| Evento | Cuándo | Actor | Contexto |
|---|---|---|---|
| `auth.login_success` (success) | Sesión emitida; en la misma transacción | Cuenta | `channel=customer_app`, `platform` |
| `auth.login_failed` (failure) | Cuenta desconocida o contraseña incorrecta (`reason=invalid_credentials`) | Cuenta si existe; si no, nulo | `reason`, `channel`, `platform` |
| `auth.login_failed` (denied) | Cuenta inactiva (`account_inactive`) o rol sin acceso a la API de clientes (`role_not_permitted`) | Cuenta | `reason`, `channel`, `platform` |
| `auth.logout` (success) | Cierre de sesión que revoca una sesión activa | Cuenta | `channel`, `platform` |

Nunca se registran el correo ni la contraseña. El cliente sigue recibiendo el mismo error genérico; si
el registro de un intento fallido no se puede guardar, se anota en el log y la respuesta no cambia.
Estos eventos entran en las métricas «Inicios de sesión exitosos/fallidos».

### Convenciones de la página (staff web)

Nombres de evento legibles junto a su código exacto; origen («App de clientes · Android») y rol bajo el
actor; enlaces en actor y correlación para filtrar sin perder el periodo; periodos rápidos (Hoy, 7, 30
y 90 días); ayuda bajo cada campo mientras tiene el foco; botón «Actualizar» y aviso de eventos nuevos
cada 30 s cuando el periodo incluye hoy (la lista visible no cambia sola); estado vacío que sugiere
ampliar el periodo o quitar filtros. Otro rol ve «No tienes permisos para acceder a estas funciones».

### Mapeo de métricas (AUD-03 y PDF)

| Métrica | Tipos (resultados) |
|---|---|
| Inicios de sesión exitosos | `auth.login_success` (success), `staff.login` (success) |
| Inicios de sesión fallidos | `auth.login_failed` (failure, denied), `staff.login_denied` (denied) |
| Verificaciones MFA fallidas | `auth.mfa_failed` (failure, denied) |
| Accesos denegados | `access.denied` (denied) |
| Suspensiones de cuenta | `account.suspended` (success) |
| Cambios de rol del personal | `role.changed` (success) |

`staff.login`/`staff.login_denied` son los eventos ya emitidos por el inicio de sesión de Soporte.
El resumen informa su corte y la cobertura histórica (`no_records` / `partial`) para no confundir
ausencia de registros con actividad cero.

## Informe PDF (AUD-04)

Una sola transacción `REPEATABLE READ` de solo lectura produce el conteo y todas las filas; las
estadísticas, gráficas y la tabla se calculan desde esas mismas filas. Máximo 2 000 eventos
(`FINANCE_AI_AUDIT_MAX_REPORT_EVENTS`): 2 000 se acepta, 2 001 se rechaza (`audit_report_too_large`),
nunca se recorta. Tras generar el PDF en memoria se vuelve a comprobar la autoridad del auditor y se
registra `audit.report_exported` (id del informe, periodo, filtros saneados y conteo); si ese registro
falla no se entrega nada (`audit_export_unrecorded`). El evento de exportación queda fuera de su propia
instantánea. No se guardan archivos ni se emiten URL; cada reintento es un informe nuevo con su propio id.

## Demo local aislada

Requisitos: Docker Compose v2 y Node ≥ 22.22.2. Desde `finance-ai-backend`:

```bash
export AUDIT_DEMO_DB_PASSWORD="$(python -c 'import secrets;print(secrets.token_urlsafe(24))')"
docker compose -p finance-audit-demo -f compose.audit-demo.yaml up -d --build
docker compose -p finance-audit-demo -f compose.audit-demo.yaml exec -T api cat demo-data/credentials.txt
```

En PowerShell: `$env:AUDIT_DEMO_DB_PASSWORD = [guid]::NewGuid().ToString('N')`. La base
`audit_demo_test` y la clave MFA (generada dentro del volumen) son exclusivas de la demo; las
credenciales ficticias se generan al iniciar. Luego, en `finance-ai-frontend/staff-web`:

```bash
npm ci
VITE_FINANCE_AI_API_BASE_URL=http://127.0.0.1:8002 npm run dev -- --host 127.0.0.1 --port 5173
```

Entrar con `auditor@example.test`, generar el QR, escanearlo con una app TOTP e ingresar el código.
`agente@example.test` sirve para comprobar que Soporte no puede abrir Auditoría. Reiniciar solo la
demo: `docker compose -p finance-audit-demo -f compose.audit-demo.yaml down -v`.

## Verificación ejecutada (2026-11-14)

- Backend: `ruff check`, `ruff format --check`, `mypy src` (strict), 558 pruebas unitarias/API y
  83 de integración PostgreSQL (641 en total) — incluye concurrencia antirreplay, cubetas por zona,
  `REPEATABLE READ`, trigger *append-only* y exclusión del auditor en la API de clientes;
  `alembic check` sin deriva; `pip-audit` sin vulnerabilidades; OpenAPI regenerado (solo adiciones).
- Staff web: `npm run lint`, `npm run typecheck`, 135 pruebas Vitest, `npm run build`.
- Prueba real en navegador contra la demo: enrolamiento TOTP, entrada a Auditoría, lista, resumen,
  PDF, `access.denied` del agente, 401 del auditor en la API de clientes.
- SonarQube local: ver `sonar-local/evidencia/`.
