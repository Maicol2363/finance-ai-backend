"""AUD-04 report snapshot statistics and deterministic PDF rendering (no AI, no storage)."""

from __future__ import annotations

import io
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime
from typing import Final
from uuid import UUID
from xml.sax.saxutils import escape

from reportlab.graphics.charts.barcharts import HorizontalBarChart, VerticalBarChart
from reportlab.graphics.shapes import Drawing
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfgen.canvas import Canvas
from reportlab.platypus import (
    Flowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from finance_ai.audit.policy import (
    SUMMARY_METRICS,
    TIME_ZONE_LABELS,
    AuditFilters,
    AuditPeriod,
    MetricDefinition,
    local_day,
)

_PAGE_SIZE: Final = landscape(A4)
_ACCENT: Final = colors.HexColor("#1F4E79")
_GRID: Final = colors.HexColor("#B7C3D0")
_HEADER_FILL: Final = colors.HexColor("#E8EEF4")
_MAX_CHART_TYPES: Final = 15
_CHART_FONT: Final = "Helvetica"
_FILTER_LABELS: Final[dict[str, str]] = {
    "event_type": "Tipo de evento",
    "outcome": "Resultado",
    "actor_id": "Actor",
    "target_type": "Tipo de objetivo",
    "target_id": "Objetivo",
    "correlation_id": "Correlación",
}

# Deterministic factual observation rules printed verbatim in every report.
OBSERVATION_RULES: Final[tuple[str, ...]] = (
    "R1. Sin eventos en el alcance: se declara que no hay eventos registrados.",
    "R2. Por cada métrica con conteo mayor que cero se informa su conteo exacto.",
    "R3. Se informa el día con más eventos (en empate, el más antiguo).",
    "R4. Se informa el tipo de evento más frecuente (en empate, orden alfabético).",
    "R5. Se informa cuántos eventos no tienen actor identificable registrado.",
    "R6. Se informa cuántos eventos no tienen identificador de correlación registrado.",
)


@dataclass(frozen=True)
class ReportEvent:
    event_id: UUID
    occurred_at: datetime
    event_type: str
    outcome: str
    actor_id: str | None
    target_type: str | None
    target_id: str | None
    correlation_id: str | None
    context: dict[str, str]


@dataclass(frozen=True)
class ReportStatistics:
    total: int
    metrics: tuple[tuple[MetricDefinition, int], ...]
    by_type_outcome: tuple[tuple[str, str, int], ...]
    by_type: tuple[tuple[str, int], ...]
    by_day: tuple[tuple[date, int], ...]
    missing_actor: int
    missing_correlation: int


@dataclass(frozen=True)
class ReportSnapshot:
    report_id: UUID
    period: AuditPeriod
    filters: AuditFilters
    cutoff: datetime
    generated_at: datetime
    acting_auditor_id: int
    events: tuple[ReportEvent, ...]
    statistics: ReportStatistics


def compute_statistics(events: Sequence[ReportEvent], period: AuditPeriod) -> ReportStatistics:
    """All statistics come from the same snapshot rows that the event table prints."""
    pairs = Counter((event.event_type, event.outcome) for event in events)
    types = Counter(event.event_type for event in events)
    days = Counter(local_day(event.occurred_at, period.zone) for event in events)
    return ReportStatistics(
        total=len(events),
        metrics=tuple((metric, metric.count(pairs)) for metric in SUMMARY_METRICS),
        by_type_outcome=tuple(
            (event_type, outcome, count)
            for (event_type, outcome), count in sorted(
                pairs.items(), key=lambda item: (-item[1], item[0][0], item[0][1])
            )
        ),
        by_type=tuple(sorted(types.items(), key=lambda item: (-item[1], item[0]))),
        by_day=tuple((day, days.get(day, 0)) for day in period.days()),
        missing_actor=sum(1 for event in events if event.actor_id is None),
        missing_correlation=sum(1 for event in events if event.correlation_id is None),
    )


def observations(statistics: ReportStatistics) -> list[str]:
    if statistics.total == 0:
        return ["No hay eventos registrados que coincidan con el alcance seleccionado."]
    lines = [
        f"Se registraron {count} eventos en la métrica «{metric.label}»."
        for metric, count in statistics.metrics
        if count > 0
    ]
    busiest_day, busiest_count = max(
        statistics.by_day, key=lambda item: (item[1], -item[0].toordinal())
    )
    lines.append(
        f"El día con más eventos fue {busiest_day.isoformat()} con {busiest_count} eventos."
    )
    top_type, top_count = statistics.by_type[0]
    lines.append(f"El tipo de evento más frecuente fue {top_type} con {top_count} eventos.")
    lines.append(f"{statistics.missing_actor} eventos no tienen un actor identificable registrado.")
    lines.append(
        f"{statistics.missing_correlation} eventos no tienen identificador de correlación "
        "registrado."
    )
    return lines


def executive_summary(snapshot: ReportSnapshot) -> list[str]:
    period = snapshot.period
    zone = TIME_ZONE_LABELS[period.time_zone]
    stats = snapshot.statistics
    lines = [
        f"Este informe incluye {stats.total} eventos de seguridad registrados entre "
        f"{period.start_date.isoformat()} y {period.end_date.isoformat()} en la zona {zone}, "
        "según los filtros aplicados.",
        f"Los datos corresponden al corte {_format_moment(snapshot.cutoff, period)} y "
        "provienen de una única instantánea de la base de datos.",
    ]
    if stats.total:
        metric_text = "; ".join(f"{metric.label}: {count}" for metric, count in stats.metrics)
        lines.append(f"Métricas documentadas: {metric_text}.")
    lines.append(
        "El contenido se deriva de reglas deterministas sobre hechos registrados; no contiene "
        "conclusiones automáticas ni clasificaciones de amenazas."
    )
    return lines


def render_report_pdf(snapshot: ReportSnapshot) -> bytes:
    buffer = io.BytesIO()
    document = SimpleDocTemplate(
        buffer,
        pagesize=_PAGE_SIZE,
        leftMargin=14 * mm,
        rightMargin=14 * mm,
        topMargin=14 * mm,
        bottomMargin=16 * mm,
        title="Informe de auditoría de seguridad",
        author="FinanciAI",
        subject=f"Informe {snapshot.report_id}",
    )
    styles = _styles()
    story: list[Flowable] = []
    story.extend(_cover(snapshot, styles))
    story.append(PageBreak())
    story.extend(_summary_section(snapshot, styles))
    story.append(Spacer(1, 6 * mm))
    story.extend(_charts_section(snapshot, styles))
    story.append(PageBreak())
    story.extend(_event_table_section(snapshot, styles))

    def footer(canvas: Canvas, doc: SimpleDocTemplate) -> None:
        canvas.saveState()
        canvas.setFont("Helvetica", 7)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawString(
            14 * mm,
            8 * mm,
            f"FinanciAI · Informe de auditoría {snapshot.report_id} · "
            f"{snapshot.statistics.total} eventos incluidos",
        )
        canvas.drawRightString(_PAGE_SIZE[0] - 14 * mm, 8 * mm, f"Página {doc.page}")
        canvas.restoreState()

    document.build(story, onFirstPage=footer, onLaterPages=footer)
    return buffer.getvalue()


def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "title": ParagraphStyle(
            "ReportTitle", parent=base["Title"], textColor=_ACCENT, fontSize=22, leading=26
        ),
        "h1": ParagraphStyle(
            "ReportHeading", parent=base["Heading1"], textColor=_ACCENT, fontSize=15
        ),
        "h2": ParagraphStyle("ReportSubheading", parent=base["Heading2"], fontSize=11.5),
        "body": ParagraphStyle("ReportBody", parent=base["BodyText"], fontSize=9.5, leading=13),
        "small": ParagraphStyle("ReportSmall", parent=base["BodyText"], fontSize=7, leading=8.5),
        "note": ParagraphStyle(
            "ReportNote",
            parent=base["BodyText"],
            fontSize=8.5,
            leading=11,
            textColor=colors.HexColor("#444444"),
        ),
    }


def _p(text: str, style: ParagraphStyle) -> Paragraph:
    return Paragraph(escape(text), style)


def _cover(snapshot: ReportSnapshot, styles: dict[str, ParagraphStyle]) -> list[Flowable]:
    period = snapshot.period
    filters = snapshot.filters.as_dict()
    filter_text = (
        "; ".join(f"{_FILTER_LABELS[key]}: {value}" for key, value in filters.items())
        if filters
        else "Sin filtros adicionales (todos los eventos del periodo)"
    )
    rows = [
        ["Identificador del informe", str(snapshot.report_id)],
        [
            "Periodo del informe",
            f"{period.start_date.isoformat()} a {period.end_date.isoformat()} (inclusive)",
        ],
        ["Zona horaria de presentación", TIME_ZONE_LABELS[period.time_zone]],
        [
            "Intervalo UTC consultado",
            f"[{period.start_utc.isoformat()}, {period.end_utc.isoformat()})",
        ],
        ["Filtros aplicados", filter_text],
        ["Eventos incluidos", str(snapshot.statistics.total)],
        ["Corte de los datos", _format_moment(snapshot.cutoff, snapshot.period)],
        ["Generado", _format_moment(snapshot.generated_at, snapshot.period)],
        ["Auditor que genera", f"Cuenta {snapshot.acting_auditor_id}"],
    ]
    table = Table(
        [[_p(label, styles["body"]), _p(value, styles["body"])] for label, value in rows],
        colWidths=[70 * mm, 190 * mm],
    )
    table.setStyle(_grid_style(header=False))
    return [
        Spacer(1, 18 * mm),
        _p("Informe de auditoría de seguridad", styles["title"]),
        _p("FinanciAI · Portal del personal · Auditoría de seguridad", styles["note"]),
        Spacer(1, 10 * mm),
        table,
        Spacer(1, 8 * mm),
        _p(
            "Documento de solo lectura generado a partir de eventos de seguridad registrados. "
            "Las marcas de tiempo se almacenan en UTC y se presentan en la zona indicada. "
            "El periodo incluye la fecha inicial y excluye la medianoche posterior a la fecha "
            "final.",
            styles["note"],
        ),
    ]


def _summary_section(snapshot: ReportSnapshot, styles: dict[str, ParagraphStyle]) -> list[Flowable]:
    stats = snapshot.statistics
    story: list[Flowable] = [_p("Resumen ejecutivo", styles["h1"])]
    story.extend(_p(line, styles["body"]) for line in executive_summary(snapshot))
    story.append(Spacer(1, 5 * mm))
    story.append(_p("Estadísticas de seguridad", styles["h1"]))
    metric_rows = [["Métrica", "Conteo", "Tipos y resultados contados"]]
    for metric, count in stats.metrics:
        mapping = ", ".join(
            f"{event_type} ({'/'.join(outcomes)})" for event_type, outcomes in metric.mapping
        )
        metric_rows.append([metric.label, str(count), mapping])
    story.append(_table(metric_rows, [70 * mm, 25 * mm, 170 * mm], styles))
    story.append(Spacer(1, 3 * mm))
    story.append(
        _p(
            "Los inicios de sesión fallidos y las verificaciones MFA fallidas son medidas de "
            "eventos distintas; no son sumables ni representan personas o ataques distintos.",
            styles["note"],
        )
    )
    story.append(Spacer(1, 4 * mm))
    story.append(_p("Eventos por tipo y resultado", styles["h2"]))
    if stats.by_type_outcome:
        rows = [["Tipo de evento", "Resultado", "Eventos"]] + [
            [event_type, outcome, str(count)]
            for event_type, outcome, count in stats.by_type_outcome
        ]
        story.append(_table(rows, [110 * mm, 50 * mm, 30 * mm], styles))
    else:
        story.append(_p("Sin datos: no hay eventos en el alcance seleccionado.", styles["body"]))
    story.append(Spacer(1, 5 * mm))
    story.append(_p("Observaciones factuales", styles["h1"]))
    story.extend(_p(f"• {line}", styles["body"]) for line in observations(stats))
    story.append(Spacer(1, 3 * mm))
    story.append(_p("Reglas deterministas aplicadas", styles["h2"]))
    story.extend(_p(rule, styles["note"]) for rule in OBSERVATION_RULES)
    return story


def _charts_section(snapshot: ReportSnapshot, styles: dict[str, ParagraphStyle]) -> list[Flowable]:
    stats = snapshot.statistics
    story: list[Flowable] = [_p("Distribución de eventos por tipo", styles["h1"])]
    if stats.total == 0:
        story.append(_p("Sin datos: no se dibuja gráfica porque no hay eventos.", styles["body"]))
    else:
        shown = stats.by_type[:_MAX_CHART_TYPES]
        story.append(_distribution_chart(shown))
        if len(stats.by_type) > len(shown):
            story.append(
                _p(
                    f"La gráfica muestra los {len(shown)} tipos más frecuentes; la tabla "
                    "«Eventos por tipo y resultado» contiene todos los tipos.",
                    styles["note"],
                )
            )
    story.append(Spacer(1, 6 * mm))
    zone_label = TIME_ZONE_LABELS[snapshot.period.time_zone]
    story.append(_p(f"Actividad por día · {zone_label}", styles["h1"]))
    if stats.total == 0:
        story.append(_p("Sin datos: no se dibuja gráfica porque no hay eventos.", styles["body"]))
    else:
        story.append(_timeline_chart(stats.by_day))
    story.append(Spacer(1, 4 * mm))
    story.append(_p("Conteo diario (equivalente textual de la gráfica)", styles["h2"]))
    story.append(_daily_table(stats.by_day, styles))
    return story


def _distribution_chart(rows: Sequence[tuple[str, int]]) -> Drawing:
    height = 30 + 16 * len(rows)
    drawing = Drawing(260 * mm, height)
    chart = HorizontalBarChart()
    chart.x = 62 * mm
    chart.y = 12
    chart.width = 170 * mm
    chart.height = height - 22
    ordered = list(reversed(rows))
    chart.data = [[count for _, count in ordered]]
    chart.categoryAxis.categoryNames = [name for name, _ in ordered]
    chart.categoryAxis.labels.fontName = _CHART_FONT
    chart.categoryAxis.labels.fontSize = 7
    chart.categoryAxis.labels.boxAnchor = "e"
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontName = _CHART_FONT
    chart.valueAxis.labels.fontSize = 7
    chart.bars[0].fillColor = _ACCENT
    chart.bars[0].strokeColor = _ACCENT
    chart.barLabelFormat = "%d"
    chart.barLabels.fontName = _CHART_FONT
    chart.barLabels.fontSize = 7
    chart.barLabels.boxAnchor = "w"
    chart.barLabels.dx = 3
    drawing.add(chart)
    return drawing


def _timeline_chart(days: Sequence[tuple[date, int]]) -> Drawing:
    drawing = Drawing(260 * mm, 75 * mm)
    chart = VerticalBarChart()
    chart.x = 15 * mm
    chart.y = 18 * mm
    chart.width = 235 * mm
    chart.height = 50 * mm
    chart.data = [[count for _, count in days]]
    step = max(1, len(days) // 15)
    chart.categoryAxis.categoryNames = [
        day.strftime("%m-%d") if index % step == 0 else "" for index, (day, _) in enumerate(days)
    ]
    chart.categoryAxis.labels.fontName = _CHART_FONT
    chart.categoryAxis.labels.fontSize = 6.5
    chart.categoryAxis.labels.angle = 45
    chart.categoryAxis.labels.boxAnchor = "ne"
    chart.valueAxis.valueMin = 0
    chart.valueAxis.labels.fontName = _CHART_FONT
    chart.valueAxis.labels.fontSize = 7
    chart.bars[0].fillColor = _ACCENT
    chart.bars[0].strokeColor = _ACCENT
    if len(days) <= 31:
        chart.barLabelFormat = "%d"
        chart.barLabels.fontName = _CHART_FONT
        chart.barLabels.fontSize = 6
        chart.barLabels.nudge = 6
    drawing.add(chart)
    return drawing


def _daily_table(days: Sequence[tuple[date, int]], styles: dict[str, ParagraphStyle]) -> Table:
    columns = 6
    cells = [f"{day.isoformat()}: {count}" for day, count in days]
    rows_count = (len(cells) + columns - 1) // columns
    grid = [
        [
            _p(cells[column * rows_count + row], styles["small"])
            if column * rows_count + row < len(cells)
            else ""
            for column in range(columns)
        ]
        for row in range(rows_count)
    ]
    table = Table(grid, colWidths=[44 * mm] * columns, repeatRows=0)
    table.setStyle(_grid_style(header=False, label_column=False))
    return table


def _event_table_section(
    snapshot: ReportSnapshot, styles: dict[str, ParagraphStyle]
) -> list[Flowable]:
    zone_label = TIME_ZONE_LABELS[snapshot.period.time_zone]
    heading = _p(f"Tabla cronológica de eventos · {zone_label}", styles["h1"])
    note = _p(
        "Orden: marca de tiempo y luego identificador del evento, de más antiguo a más "
        f"reciente. Incluye los {snapshot.statistics.total} eventos del alcance en la "
        "instantánea; no es una página del navegador. «No registrado» indica un dato "
        "ausente en el evento.",
        styles["note"],
    )
    if not snapshot.events:
        return [
            heading,
            note,
            _p("Sin datos: no hay eventos en el alcance seleccionado.", styles["body"]),
        ]
    header = [
        "#",
        "Fecha y hora",
        "Tipo",
        "Resultado",
        "Actor",
        "Objetivo",
        "Correlación",
        "Contexto",
        "ID del evento",
    ]
    rows: list[list[Paragraph | str]] = [header]
    for index, event in enumerate(snapshot.events, start=1):
        target = (
            f"{event.target_type}:{event.target_id}"
            if event.target_type and event.target_id
            else event.target_type or "No registrado"
        )
        context = ", ".join(f"{key}={value}" for key, value in sorted(event.context.items()))
        rows.append(
            [
                str(index),
                _p(_format_local(event.occurred_at, snapshot.period), styles["small"]),
                _p(event.event_type, styles["small"]),
                _p(event.outcome, styles["small"]),
                _p(event.actor_id or "No registrado", styles["small"]),
                _p(target, styles["small"]),
                _p(event.correlation_id or "No registrado", styles["small"]),
                _p(context or "-", styles["small"]),
                _p(str(event.event_id), styles["small"]),
            ]
        )
    table = Table(
        rows,
        colWidths=[
            10 * mm,
            30 * mm,
            34 * mm,
            16 * mm,
            16 * mm,
            34 * mm,
            34 * mm,
            42 * mm,
            52 * mm,
        ],
        repeatRows=1,
    )
    table.setStyle(_grid_style(header=True, font_size=7))
    return [KeepTogether([heading, note]), Spacer(1, 2 * mm), table]


def _table(rows: list[list[str]], widths: list[float], styles: dict[str, ParagraphStyle]) -> Table:
    body: list[list[Paragraph | str]] = [rows[0]]
    body.extend([_p(cell, styles["body"]) for cell in row] for row in rows[1:])
    table = Table(body, colWidths=widths, repeatRows=1)
    table.setStyle(_grid_style(header=True))
    return table


def _grid_style(*, header: bool, font_size: float = 8.5, label_column: bool = True) -> TableStyle:
    commands: list[tuple[object, ...]] = [
        ("GRID", (0, 0), (-1, -1), 0.4, _GRID),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("LEFTPADDING", (0, 0), (-1, -1), 3),
        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
    ]
    if header:
        commands.extend(
            [
                ("BACKGROUND", (0, 0), (-1, 0), _HEADER_FILL),
                ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ]
        )
    elif label_column:
        commands.append(("BACKGROUND", (0, 0), (0, -1), _HEADER_FILL))
    return TableStyle(commands)


def _format_moment(moment: datetime, period: AuditPeriod) -> str:
    local = moment.astimezone(period.zone)
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {TIME_ZONE_LABELS[period.time_zone]}"


def _format_local(moment: datetime, period: AuditPeriod) -> str:
    return moment.astimezone(period.zone).strftime("%Y-%m-%d %H:%M:%S")
