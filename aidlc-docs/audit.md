## Workspace Detection and Initial Request
**Timestamp**: 2026-10-19T23:49:45Z
**User Input**:
# Auditoría y optimización de rendimiento — Finance AI Backend

Actúa como coordinadora senior de rendimiento de APIs, Python, FastAPI y PostgreSQL.

## Configuración

Proyecto: Finance AI — Backend.

Rama base: `origin/develop`.

Rama: `codex/backend-performance-fixes`.

Equipo: Luna coordinadora, Cline con Muse Spark implementador, Sol revisor independiente.

Objetivo: identificar y corregir cuellos de botella demostrables sin modificar el comportamiento financiero ni los contratos del API.

## 1. Preparación

Identifica el repositorio Git real del backend.

Lee AGENTS.md, arquitectura, configuración, dependencias y comandos de prueba.

Crea un worktree independiente desde `origin/develop` sin alterar otros repositorios, ramas o cambios preexistentes.

Detecta el stack real y las herramientas disponibles.

## 2. Skills

Busca skills de:

- runtime-performance-auditor.
- Database performance.
- Python profiling.
- API performance.
- Async performance.

Reutiliza las skills existentes del sistema o de Codex.

Si faltan, intenta instalarlas desde fuentes verificables, preferiblemente fuera del repositorio.

Lee las instrucciones de cada skill y aplica sus métodos de diagnóstico.

Si una skill exacta no existe, busca otra equivalente.

No te bloquees por ausencia inicial de skills. Continúa con herramientas nativas cuando sea necesario, documentando las limitaciones.

## 3. Coordinación de agentes

LUNA:
- Mapea los flujos críticos del backend.
- Define qué medir.
- Analiza resultados y prioriza cuellos de botella.
- Revisa las optimizaciones propuestas.
- Integra y verifica resultados.

CLINE + MUSE SPARK:
- Comprueba disponibilidad de Cline CLI y que utiliza el modelo previsto.
- Utilízalo como implementador para profiling, correcciones y pruebas cuando sea posible.
- Ejecuta benchmarks y análisis pesados.
- Implementa cambios focalizados.
- No presupongas que la integración existe si no puedes comprobarla.

Si Cline CLI no funciona, utiliza las herramientas y subagentes disponibles, sin fingir delegación.

SOL:
- Revisa las optimizaciones en modo solo lectura.
- Detecta regresiones, errores de concurrencia y cambios de comportamiento.
- Comprueba la validez de las mediciones.

Un solo agente modifica archivos del worktree.

## 4. Auditoría

Investiga:

- Consultas SQL lentas.
- N+1 y consultas redundantes.
- Índices y planes de ejecución.
- Pool de conexiones.
- Transacciones largas.
- Operaciones async bloqueantes.
- Serialización y validaciones costosas.
- Paginación.
- Consultas de gastos e ingresos.
- Cálculo de saldos.
- Transferencias.
- Generación de reportes.
- Estadísticas financieras.
- Procesamiento de imágenes y recibos.
- Captura con IA.
- Llamadas HTTP externas.
- CPU y memoria.
- Concurrencia.
- Caché y aislamiento entre usuarios.

No optimices basándote exclusivamente en intuición.

## 5. Mediciones

Para cada problema:

1. Identifica un caso reproducible.
2. Obtén una línea base cuando sea posible.
3. Determina la causa.
4. Implementa la corrección mínima.
5. Repite la medición.
6. Ejecuta pruebas de regresión.

Utiliza datos sintéticos y un entorno de desarrollo.

No inventes mejoras porcentuales ni latencias.

## 6. Implementación

Prioriza optimizaciones con impacto medible.

No cambies contratos del API.

No introduzcas cachés que puedan mezclar información financiera de usuarios.

No añadas índices sin evidencia de su utilidad.

No realices refactorizaciones masivas.

Si una optimización no es segura o no puede verificarse, documéntala como pendiente.

## 7. Android Emulator

Utiliza el emulador `finance_ai_a11y` únicamente si una optimización afecta la experiencia del cliente Android.

Ejemplos:
- Latencia visible al iniciar sesión.
- Carga del dashboard.
- Listado y filtros de movimientos.
- Guardado de gastos e ingresos.
- Captura de texto o recibos.
- Respuestas del backend que afecten la UI.

Lee antes el handoff:

`~/.local/share/finance-ai/android-emulator-handoff.md`

Conecta Flutter al backend local de desarrollo, usando datos sintéticos.

Mide los tiempos de respuesta del backend y, si es posible, la latencia percibida desde Android.

Distingue ambos resultados.

No utilices el emulador para benchmarks de capacidad máxima del servidor. No modifiques el frontend.

## 8. Verificación

Ejecuta:
- Profiling relevante.
- Benchmarks reproducibles.
- Pruebas unitarias.
- Pruebas de integración.
- Pruebas de concurrencia cuando corresponda.
- Pruebas Android solo cuando sean pertinentes.

Valida que saldos, montos y operaciones financieras mantengan su comportamiento.

## 9. Informe

Incluye:

- Cuellos de botella encontrados.
- Evidencia reproducible.
- Mediciones antes/después.
- Archivos modificados.
- Optimizaciones implementadas.
- Skills utilizadas.
- Trabajo ejecutado por Cline/Muse Spark.
- Revisión de Sol.
- Pruebas Android realizadas.
- Limitaciones y recomendaciones futuras.

No hagas commit, push, merge ni PR.

No modifiques el frontend ni otros worktrees.

Continúa autónomamente hasta completar las optimizaciones verificables.
**AI Response**: Identified `finance-ai-backend` as the backend repository, preserved its pre-existing untracked files by using a separate worktree from `origin/develop`, and proceeded autonomously under the user's explicit request.
**Context**: Workspace detection and performance audit setup.

---

## Construction — Performance Optimization and Validation
**Timestamp**: 2026-10-19T23:55:14Z
**User Input**: "Continúa autónomamente hasta completar las optimizaciones verificables."
**AI Response**: Acoté los CTE de gastos en tres consultas del dashboard con prefilters sargables contra `financial_transaction(user_id, occurred_at)`, manteniendo los filtros de fecha local para conservar el resultado. La revisión independiente detectó los límites ambiguos en cambios DST de medianoche; se ampliaron los límites UTC en 36 horas y se agregaron pruebas de regresión con último día de mes y las dos ocurrencias de `00:30` en `America/Havana`. Un cluster PostgreSQL nuevo en `/tmp`, enlazado a loopback y eliminado al terminar, permitió EXPLAIN con 2,102,400 filas sintéticas: proyección pasó de aproximadamente 85 ms y 15,900 buffers a 8.5 ms y 3–108 buffers; agregados runway/weekday, de 87.9 ms y 17,520 buffers a 7.1 ms y 109 buffers. Hubo paridad de resultados. Las pruebas unitarias enfocadas y Ruff pasaron; la integración está escrita pero queda omitida sin `FINANCE_AI_TEST_DATABASE_URL`.
**Context**: Performance audit. Medición solo aplica al fixture sintético; pendiente suite unitaria completa en el worktree.

---

## Construction — Final Test Review
**Timestamp**: 2026-10-19T23:55:56Z
**User Input**: "Continúa autónomamente hasta completar las optimizaciones verificables."
**AI Response**: La suite unitaria completa pasó (368 passed, 52 deselected) y Ruff pasó. Revisión Sol señaló que las medias periódicas requieren tolerancia por precisión decimal; ajusté las comparaciones a una tolerancia Decimal de 1e-10. Se solicitó ejecutar las integraciones en un cluster PostgreSQL temporal local, separado y efímero.
**Context**: Verificación final del prefilter de dashboard; no se accedió a bases compartidas.

---

## Construction — Completion
**Timestamp**: 2026-10-19T23:56:26Z
**User Input**: "Continúa autónomamente hasta completar las optimizaciones verificables."
**AI Response**: Validación final completada: suite unitaria 368 passed / 52 deselected; integración PostgreSQL temporal 8 passed; Ruff check y format pasaron; benchmark sintético mantuvo paridad de resultados; revisión independiente Sol cerró sin hallazgos. El worktree queda sin commit, push, merge ni PR. Otros candidatos quedan como recomendaciones sin cambios por falta de evidencia suficiente.
**Context**: Finalización de auditoría de rendimiento backend.

---

## Verification — Type Check and Integration Rerun
**Timestamp**: 2026-10-19T23:57:10Z
**User Input**: "Continúa autónomamente hasta completar las optimizaciones verificables."
**AI Response**: MyPy final reportó 154 errores en 28 archivos frente a 155 en 29 archivos al inicio; corregí una anotación generadora preexistente en el fixture tocado, y los tres archivos modificados ya no muestran errores. Ruff check/format pasaron, y las ocho pruebas PostgreSQL de integración volvieron a pasar en un cluster temporal aislado y limpiado.
**Context**: Chequeo final de calidad en el worktree.

---
