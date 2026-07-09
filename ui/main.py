from fasthtml.common import *

def get_site_url(url: str) -> str:
    raw = url.strip()
    domain = raw.replace("https://", "").replace("http://", "").strip().rstrip("/")
    return raw if "/" in domain else f"sc-domain:{domain}"

from fhdaisy import *
from fhdaisy.core import daisy_hdrs
from seootter.sqlite_db import get_session
from seootter.models import Website, GSCAnalytics, add_or_update_website, TrackedKeyword, add_tracked_keyword, delete_tracked_keyword, get_tracked_keywords, get_url_mapping, sync_url_mapping, WuiltStore, WuiltProduct, WuiltPage, add_or_update_wuilt_store, delete_wuilt_store, get_wuilt_products, get_wuilt_pages, print_wuilt_stores, print_wuilt_products, print_wuilt_pages
from seootter.wuilt.sync import sync_wuilt_products, sync_wuilt_pages, sync_wuilt_store
from seootter.wuilt.client import WuiltClient
from seootter.wuilt.optimizer import batch_optimize_products, optimize_and_push, optimize_product
from seootter.gsc.queries import get_top_pages, get_top_queries, get_wins, get_country_breakdown, get_trends, get_page_analytics
from seootter.gsc.sync import get_missing_dates, store_single_date
from seootter.gsc_client import GSCAuth, get_date_range, get_verified_sites
from seootter.insights.trends import detect_query_trends
from seootter.insights.intent import classify_page_intents
from seootter.content.analysis import find_cannibalized
from seootter.index_tracking import get_index_status, get_not_indexed_by_reason, store_index_status, fetch_sitemap_urls, get_index_history
from seootter.schema_extractor import extract_faq_queries
from seootter.schema_validator import validate_page
from seootter.article import get_articles_by_website, Article
from seootter.report.generator import generate_seo_report
from seootter.article import Article, insert_article
from seootter.dspy_infer import infer_article_seo, get_article_content, predict_schemas
import importlib.util
import json, re
import pycountry
from dotenv import load_dotenv
from sqlmodel import select, func
from pathlib import Path
from threading import Thread
import time
from datetime import datetime
import csv, io

CONFIG_DIR = Path.home() / ".config" / "seootter"
SETTINGS_ENV_PATH = CONFIG_DIR / ".env"
REPORT_CACHE_DIR = CONFIG_DIR / "report_cache"

_sync_progress = {}
_index_check_progress = {}
_report_cache: dict[int, dict] = {}  # website_id → report_data, cleared on refresh
_report_progress: dict[int, dict] = {}  # website_id → {status, pct, msg}


def _report_cache_path(website_id: int) -> Path:
    return REPORT_CACHE_DIR / f"{website_id}.json"

def _load_cached_report(website_id: int) -> dict | None:
    data = _report_cache.get(website_id)
    if data is not None:
        return data
    path = _report_cache_path(website_id)
    if path.exists():
        try:
            data = json.loads(path.read_text())
            _report_cache[website_id] = data
            return data
        except (json.JSONDecodeError, OSError):
            return None
    return None

def _save_cached_report(website_id: int, data: dict):
    _report_cache[website_id] = data
    REPORT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _report_cache_path(website_id)
    path.write_text(json.dumps(data, default=str, ensure_ascii=False), encoding="utf-8")


# ── Export helpers ────────────────────────────────────────────────
def _export_response(data: str, filename: str, fmt: str):
    ct = "text/csv" if fmt == "csv" else "text/plain"
    return Response(data, media_type=ct,
                    headers={"Content-Disposition": f"attachment; filename={filename}.{fmt}"})

def to_csv(headers: list[str], rows: list[list]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers)
    for r in rows:
        w.writerow(r)
    return buf.getvalue()

def to_markdown(headers: list[str], rows: list[list]) -> str:
    sep = "|" + "|".join("---" for _ in headers) + "|"
    lines = ["|" + "|".join(headers) + "|", sep]
    for r in rows:
        lines.append("|" + "|".join(str(c) for c in r) + "|")
    return "\n".join(lines) + "\n"


def _report_stats(report_data: dict) -> FT:
    return Div(cls="stats shadow mb-6")(
        Div(cls="stat")(
            Div("Pages", cls="stat-title"),
            Div(str(report_data["total_pages"]), cls="stat-value"),
        ),
        Div(cls="stat")(
            Div("Pages with Issues", cls="stat-title"),
            Div(str(report_data["summary"]["pages_with_issues"]), cls="stat-value"),
            Div(cls="stat-desc")(f"{report_data['summary']['total_issues']} total issues"),
        ),
        Div(cls="stat")(
            Div("Duplicate Titles", cls="stat-title"),
            Div(str(report_data["summary"]["duplicate_titles_count"]), cls="stat-value"),
        ),
        Div(cls="stat")(
            Div("Duplicate Descriptions", cls="stat-title"),
            Div(str(report_data["summary"]["duplicate_descriptions_count"]), cls="stat-value"),
        ),
    )

def _report_issues_table(report_data: dict) -> FT:
    return Div(cls="overflow-x-auto")(
        Table(cls="table table-zebra")(
            Thead(Tr(Th("Page"), Th("Issues"))),
            Tbody(*[
                Tr(
                    Td(A(url, href=url, target="_blank", cls="link link-primary text-sm")),
                    Td(Span(", ".join(data["issues"]), cls="text-xs text-error")),
                ) for url, data in sorted(
                    report_data["issues"].items(),
                    key=lambda x: len(x[1]["issues"]),
                    reverse=True
                )[:50]
            ]),
        ),
    )


THEMES = ["dim", "light", "dark", "coffee", "night", "winter"]

THEME_SCRIPT = Script("""
function setTheme(t) {
    document.documentElement.setAttribute('data-theme', t);
    localStorage.setItem('theme', t);
    document.querySelectorAll('.theme-btn').forEach(b => {
        b.classList.toggle('active', b.getAttribute('data-theme') === t);
    });
}
document.addEventListener('DOMContentLoaded', function() {
    var saved = localStorage.getItem('theme') || 'dim';
    setTheme(saved);
});
""")

CUSTOM_STYLE = Style("""
.htmx-indicator { opacity: 0; transition: opacity 200ms ease-in; }
.htmx-request .htmx-indicator { opacity: 1; }
.htmx-request.htmx-indicator { opacity: 1; }
.drawer-content .container { animation: fadeIn 0.25s ease-out; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(6px); } to { opacity: 1; transform: translateY(0); } }
.theme-btn.active::before { content: "✓ "; }

/* ── Page header ─────────────────────────────────────── */
.page-header { margin-bottom: 1.75rem; }
.page-header h1 { font-size: 1.625rem; font-weight: 700; line-height: 1.2; margin: 0; letter-spacing: -0.01em; }
.page-header .subtitle { color: oklch(var(--bc) / 0.6); font-size: 0.875rem; margin-top: 0.25rem; }
.page-header .actions { display: flex; gap: 0.5rem; flex-wrap: wrap; }
.page-header-row { display: flex; flex-wrap: wrap; align-items: flex-start; gap: 0.75rem; }
.page-header-row .grow { flex: 1; min-width: 0; }

/* ── Stat card ───────────────────────────────────────── */
.stat-card {
  background: oklch(var(--b1));
  border: 1px solid oklch(var(--b3));
  border-radius: 0.75rem;
  padding: 1rem 1.25rem;
  transition: all 0.15s ease;
}
.stat-card:hover { border-color: oklch(var(--p) / 0.4); }
.stat-card .label { font-size: 0.75rem; color: oklch(var(--bc) / 0.6); font-weight: 500; text-transform: uppercase; letter-spacing: 0.04em; }
.stat-card .value { font-size: 1.5rem; font-weight: 700; margin-top: 0.25rem; line-height: 1; }
.stat-card .delta { font-size: 0.75rem; margin-top: 0.375rem; font-weight: 500; }
.stat-card .delta.pos { color: oklch(var(--su)); }
.stat-card .delta.neg { color: oklch(var(--er)); }

/* ── Empty state ─────────────────────────────────────── */
.empty-state {
  text-align: center;
  padding: 3rem 1.5rem;
  background: oklch(var(--b1));
  border: 1px dashed oklch(var(--b3));
  border-radius: 0.75rem;
}
.empty-state .icon { font-size: 2.5rem; margin-bottom: 0.75rem; opacity: 0.6; }
.empty-state h3 { font-size: 1.125rem; font-weight: 600; margin-bottom: 0.5rem; }
.empty-state p { color: oklch(var(--bc) / 0.6); font-size: 0.875rem; max-width: 32rem; margin: 0 auto 1.25rem; }

/* ── Status badge ────────────────────────────────────── */
.badge-soft-success { background: oklch(var(--su) / 0.15); color: oklch(var(--su)); border: 1px solid oklch(var(--su) / 0.2); }
.badge-soft-warning { background: oklch(var(--wa) / 0.15); color: oklch(var(--wa)); border: 1px solid oklch(var(--wa) / 0.2); }
.badge-soft-info { background: oklch(var(--in) / 0.15); color: oklch(var(--in)); border: 1px solid oklch(var(--in) / 0.2); }
.badge-soft-error { background: oklch(var(--er) / 0.15); color: oklch(var(--er)); border: 1px solid oklch(var(--er) / 0.2); }

/* ── Card / section ──────────────────────────────────── */
.section { background: oklch(var(--b1)); border: 1px solid oklch(var(--b3)); border-radius: 0.75rem; padding: 1.25rem 1.5rem; }
.section-title { display: flex; align-items: center; gap: 0.5rem; font-size: 1rem; font-weight: 600; margin: 0 0 0.875rem 0; }
.section-subtitle { color: oklch(var(--bc) / 0.6); font-size: 0.8125rem; margin-top: -0.5rem; margin-bottom: 0.875rem; }

/* ── Hint box ────────────────────────────────────────── */
.hint { display: flex; gap: 0.625rem; align-items: flex-start; background: oklch(var(--in) / 0.08); border: 1px solid oklch(var(--in) / 0.2); border-radius: 0.5rem; padding: 0.75rem 1rem; font-size: 0.8125rem; color: oklch(var(--bc) / 0.85); }
.hint .hint-icon { font-size: 1rem; line-height: 1.4; }
.hint strong { color: oklch(var(--bc)); font-weight: 600; }

/* ── Loading skeleton ────────────────────────────────── */
.skeleton { background: linear-gradient(90deg, oklch(var(--b2)) 0%, oklch(var(--b3)) 50%, oklch(var(--b2)) 100%); background-size: 200% 100%; animation: shimmer 1.5s infinite; border-radius: 0.375rem; }
@keyframes shimmer { 0% { background-position: -200% 0; } 100% { background-position: 200% 0; } }

/* ── Scrollbars ──────────────────────────────────────── */
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb { background: oklch(var(--b3)); border-radius: 5px; border: 2px solid transparent; background-clip: padding-box; }
::-webkit-scrollbar-thumb:hover { background: oklch(var(--bc) / 0.2); background-clip: padding-box; border: 2px solid transparent; }

/* ── Focus visible ───────────────────────────────────── */
*:focus-visible { outline: 2px solid oklch(var(--p)); outline-offset: 2px; border-radius: 0.25rem; }

/* ── Smooth transitions on links & buttons ───────────── */
a, button, .btn { transition: background-color 0.15s ease, color 0.15s ease, border-color 0.15s ease, transform 0.15s ease, box-shadow 0.15s ease; }

/* ── Code & monospace ────────────────────────────────── */
code, kbd { font-family: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace; font-size: 0.875em; }

/* ── Page transitions ────────────────────────────────── */
.page-fade { animation: pageFade 0.3s ease-out; }
@keyframes pageFade { from { opacity: 0; } to { opacity: 1; } }

/* ── Data table polish ───────────────────────────────── */
.table thead th { font-size: 0.75rem; text-transform: uppercase; letter-spacing: 0.04em; color: oklch(var(--bc) / 0.6); font-weight: 600; }
.table tbody td { vertical-align: middle; }
.table-hover tbody tr { transition: background-color 0.1s ease; }
.table-zebra tbody tr:nth-child(even) { background-color: oklch(var(--b2) / 0.3); }
""")

THEME_STYLE = Style("""
[data-theme="dim"] {
color-scheme: dark;
--color-base-100: oklch(30.857% 0.023 264.149);
--color-base-200: oklch(28.036% 0.019 264.182);
--color-base-300: oklch(26.346% 0.018 262.177);
--color-base-content: oklch(82.901% 0.031 222.959);
--color-primary: oklch(86.133% 0.141 139.549);
--color-primary-content: oklch(17.226% 0.028 139.549);
--color-secondary: oklch(73.375% 0.165 35.353);
--color-secondary-content: oklch(14.675% 0.033 35.353);
--color-accent: oklch(74.229% 0.133 311.379);
--color-accent-content: oklch(14.845% 0.026 311.379);
--color-neutral: oklch(24.731% 0.02 264.094);
--color-neutral-content: oklch(82.901% 0.031 222.959);
--color-info: oklch(86.078% 0.142 206.182);
--color-info-content: oklch(17.215% 0.028 206.182);
--color-success: oklch(86.171% 0.142 166.534);
--color-success-content: oklch(17.234% 0.028 166.534);
--color-warning: oklch(86.163% 0.142 94.818);
--color-warning-content: oklch(17.232% 0.028 94.818);
--color-error: oklch(82.418% 0.099 33.756);
--color-error-content: oklch(16.483% 0.019 33.756);
--radius-selector: 1rem;
--radius-field: 0.5rem;
--radius-box: 1rem;
--size-selector: 0.25rem;
--size-field: 0.25rem;
--border: 1px;
--depth: 0;
--noise: 0;
}
[data-theme="coffee"] {
color-scheme: dark;
--color-base-100: oklch(24% 0.023 329.708);
--color-base-200: oklch(21% 0.021 329.708);
--color-base-300: oklch(16% 0.019 329.708);
--color-base-content: oklch(72.354% 0.092 79.129);
--color-primary: oklch(71.996% 0.123 62.756);
--color-primary-content: oklch(14.399% 0.024 62.756);
--color-secondary: oklch(34.465% 0.029 199.194);
--color-secondary-content: oklch(86.893% 0.005 199.194);
--color-accent: oklch(42.621% 0.074 224.389);
--color-accent-content: oklch(88.524% 0.014 224.389);
--color-neutral: oklch(16.51% 0.015 326.261);
--color-neutral-content: oklch(83.302% 0.003 326.261);
--color-info: oklch(79.49% 0.063 184.558);
--color-info-content: oklch(15.898% 0.012 184.558);
--color-success: oklch(74.722% 0.072 131.116);
--color-success-content: oklch(14.944% 0.014 131.116);
--color-warning: oklch(88.15% 0.14 87.722);
--color-warning-content: oklch(17.63% 0.028 87.722);
--color-error: oklch(77.318% 0.128 31.871);
--color-error-content: oklch(15.463% 0.025 31.871);
--radius-selector: 1rem;
--radius-field: 0.5rem;
--radius-box: 1rem;
--size-selector: 0.25rem;
--size-field: 0.25rem;
--border: 1px;
--depth: 0;
--noise: 0;
}
[data-theme="night"] {
color-scheme: dark;
--color-base-100: oklch(20.768% 0.039 265.754);
--color-base-200: oklch(19.314% 0.037 265.754);
--color-base-300: oklch(17.86% 0.034 265.754);
--color-base-content: oklch(84.153% 0.007 265.754);
--color-primary: oklch(75.351% 0.138 232.661);
--color-primary-content: oklch(15.07% 0.027 232.661);
--color-secondary: oklch(68.011% 0.158 276.934);
--color-secondary-content: oklch(13.602% 0.031 276.934);
--color-accent: oklch(72.36% 0.176 350.048);
--color-accent-content: oklch(14.472% 0.035 350.048);
--color-neutral: oklch(27.949% 0.036 260.03);
--color-neutral-content: oklch(85.589% 0.007 260.03);
--color-info: oklch(68.455% 0.148 237.251);
--color-info-content: oklch(0% 0 0);
--color-success: oklch(78.452% 0.132 181.911);
--color-success-content: oklch(15.69% 0.026 181.911);
--color-warning: oklch(83.242% 0.139 82.95);
--color-warning-content: oklch(16.648% 0.027 82.95);
--color-error: oklch(71.785% 0.17 13.118);
--color-error-content: oklch(14.357% 0.034 13.118);
--radius-selector: 1rem;
--radius-field: 0.5rem;
--radius-box: 1rem;
--size-selector: 0.25rem;
--size-field: 0.25rem;
--border: 1px;
--depth: 0;
--noise: 0;
}
[data-theme="winter"] {
color-scheme: light;
--color-base-100: oklch(100% 0 0);
--color-base-200: oklch(97.466% 0.011 259.822);
--color-base-300: oklch(93.268% 0.016 262.751);
--color-base-content: oklch(41.886% 0.053 255.824);
--color-primary: oklch(56.86% 0.255 257.57);
--color-primary-content: oklch(91.372% 0.051 257.57);
--color-secondary: oklch(42.551% 0.161 282.339);
--color-secondary-content: oklch(88.51% 0.032 282.339);
--color-accent: oklch(59.939% 0.191 335.171);
--color-accent-content: oklch(11.988% 0.038 335.171);
--color-neutral: oklch(19.616% 0.063 257.651);
--color-neutral-content: oklch(83.923% 0.012 257.651);
--color-info: oklch(88.127% 0.085 214.515);
--color-info-content: oklch(17.625% 0.017 214.515);
--color-success: oklch(80.494% 0.077 197.823);
--color-success-content: oklch(16.098% 0.015 197.823);
--color-warning: oklch(89.172% 0.045 71.47);
--color-warning-content: oklch(17.834% 0.009 71.47);
--color-error: oklch(73.092% 0.11 20.076);
--color-error-content: oklch(14.618% 0.022 20.076);
--radius-selector: 1rem;
--radius-field: 0.5rem;
--radius-box: 1rem;
--size-selector: 0.25rem;
--size-field: 0.25rem;
--border: 1px;
--depth: 0;
--noise: 0;
}
""")

page_hdrs = daisy_hdrs + (THEME_SCRIPT, CUSTOM_STYLE, THEME_STYLE)


# ── UI helper components ──────────────────────────────────────────────
def page_header(title: str, subtitle: str = "", back: str = "", actions: list | None = None):
    """Consistent page header with optional breadcrumb back link and action buttons."""
    children = []
    if back:
        children.append(A(f"← {back}", href=back, cls="text-sm text-base-content/60 hover:text-primary mb-2 inline-flex items-center gap-1"))
    children.append(Div(cls="page-header-row")(
        Div(cls="grow")(
            H1(title, cls="text-2xl font-bold"),
            P(subtitle, cls="subtitle") if subtitle else "",
        ),
        Div(cls="page-header actions")(*actions) if actions else "",
    ))
    return Div(cls="page-header page-fade")(*children)


def stat_card(label: str, value, delta: str = "", delta_positive: bool = True, icon: str = "", href: str = ""):
    """Polished stat card with label, value, optional delta and icon."""
    inner = Div(
        Div(cls="flex items-center justify-between")(
            Span(label, cls="label"),
            Span(icon, cls="text-lg opacity-60") if icon else "",
        ),
        Div(str(value), cls="value"),
        Span(delta, cls=f"delta {'pos' if delta_positive else 'neg'}") if delta else "",
    )
    if href:
        return A(inner, href=href, cls="stat-card no-underline")
    return Div(cls="stat-card")(inner)


def empty_state(icon: str, title: str, description: str, cta_text: str = "", cta_href: str = ""):
    """Friendly empty state with icon, title, description, and optional CTA button."""
    return Div(cls="empty-state")(
        Div(icon, cls="icon"),
        H3(title),
        P(description),
        A(cta_text, href=cta_href, cls="btn btn-primary btn-sm") if cta_text and cta_href else "",
    )


def hint_box(text: str, icon: str = "💡"):
    """A subtle info/hint callout for helpful context."""
    if isinstance(text, str):
        return Div(cls="hint")(
            Span(icon, cls="hint-icon"),
            Span(text),
        )
    return Div(cls="hint")(
        Span(icon, cls="hint-icon"),
        Div(*text) if isinstance(text, list) else text,
    )


def section(title: str, subtitle: str = "", *children):
    """A content section with title and optional subtitle."""
    return Div(cls="section mb-6")(
        H2(title, cls="section-title") if title else "",
        P(subtitle, cls="section-subtitle") if subtitle else "",
        *children,
    )


def status_badge(status: str, size: str = "sm"):
    """Colored status badge for published/draft/active/etc."""
    s = status.upper()
    color_map = {
        "PUBLISHED": "badge-soft-success", "ACTIVE": "badge-soft-success", "PUBLISH": "badge-soft-success",
        "DRAFT": "badge-soft-warning", "PENDING": "badge-soft-warning", "ARCHIVED": "badge-soft-info",
        "ERROR": "badge-soft-error", "FAILED": "badge-soft-error",
    }
    color = color_map.get(s, "badge-soft")
    return Span(status, cls=f"badge {color} badge-{size}")


def loading_indicator(text: str = "Loading..."):
    """Standard HTMX loading indicator."""
    return Span(cls="htmx-indicator inline-flex items-center gap-2 text-sm text-base-content/60")(
        Span(cls="loading loading-spinner loading-xs"),
        Span(text),
    )


def toast(message: str, kind: str = "info"):
    """A small dismissable toast notification (rendered inline)."""
    icon_map = {"success": "✅", "error": "❌", "warning": "⚠️", "info": "ℹ️"}
    color_map = {"success": "alert-success", "error": "alert-error", "warning": "alert-warning", "info": "alert-info"}
    return Div(cls=f"alert {color_map.get(kind, 'alert-info')} mb-4")(
        Span(icon_map.get(kind, "ℹ️")),
        Span(message),
    )


def _sidebar():
    with get_session() as session:
        site_count = len(session.exec(select(Website)).all())
        store_count = len(session.exec(select(WuiltStore)).all())
    return Aside(cls="w-64 min-h-full bg-base-200 border-r border-base-300 flex flex-col")(
        Div(cls="p-5 border-b border-base-300")(
            A(href="/", cls="flex items-center gap-2 no-underline")(
                Span("🦦", cls="text-2xl"),
                Div()(
                    Div("SEO Otter", cls="font-bold text-base leading-tight"),
                    Div("SEO made simple", cls="text-xs text-base-content/50"),
                ),
            ),
        ),
        Ul(cls="menu p-3 gap-1 grow")(
            Li(cls="menu-title text-xs tracking-wider opacity-50 px-4 py-2")("Workspace"),
            Li(A(href="/", cls="flex items-center gap-3 px-4 py-2 rounded-lg transition-colors duration-150 hover:bg-base-300")(
                Span("📊", cls="text-lg"), Span("Dashboard"),
                Span(str(site_count), cls="badge badge-soft badge-sm ml-auto") if site_count else "",
            )),
            Li(cls="menu-title text-xs tracking-wider opacity-50 px-4 py-2 mt-3")("E-commerce"),
            Li(A(href="/wuILT", cls="flex items-center gap-3 px-4 py-2 rounded-lg transition-colors duration-150 hover:bg-base-300")(
                Span("🏪", cls="text-lg"), Span("Wuilt Stores"),
                Span(str(store_count), cls="badge badge-soft badge-sm ml-auto") if store_count else "",
            )),
            Li(cls="menu-title text-xs tracking-wider opacity-50 px-4 py-2 mt-3")("System"),
            Li(A(href="/settings", cls="flex items-center gap-3 px-4 py-2 rounded-lg transition-colors duration-150 hover:bg-base-300")(
                Span("⚙️", cls="text-lg"), Span("Settings"),
            )),
        ),
        Div(cls="p-4 border-t border-base-300 text-xs text-base-content/40")(
            P("SEO Otter v0.0.1", cls="mb-1"),
            P("Made with ♥ for SEO", cls="opacity-70"),
        ),
    )


def _navbar():
    return Div(cls="navbar bg-base-100 border-b border-base-300 px-4 h-14 min-h-14")(
        Div(cls="navbar-start gap-2")(
            Label(for_="drawer-toggle", cls="btn btn-ghost btn-square btn-sm lg:hidden")("☰"),
        ),
        Div(cls="navbar-end gap-1")(
            Details(cls="dropdown dropdown-end")(
                Summary(cls="btn btn-ghost btn-sm gap-2")(
                    Span("🎨", cls="text-base"),
                    Span("Theme", cls="text-sm hidden sm:inline"),
                ),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box z-[1] p-2 shadow-lg min-w-36 border border-base-300")(
                    Li(P("Color theme", cls="text-xs text-base-content/50 px-3 py-1")),
                    *[Li(Button(
                        {"dim": "🌑 Dim", "light": "☀️ Light", "dark": "🌙 Dark", "coffee": "☕ Coffee", "night": "🌃 Night", "winter": "❄️ Winter"}.get(t, t.title()),
                        cls="theme-btn justify-start", data_theme=t,
                        onclick=f"setTheme('{t}')",
                    )) for t in THEMES],
                ),
            ),
        ),
    )


def _body_wrap(content, req):
    return Div(cls="min-h-screen bg-base-200 text-base-content")(
        Div(cls="drawer lg:drawer-open")(
            Input(type="checkbox", id="drawer-toggle", cls="drawer-toggle"),
            Div(cls="drawer-content flex flex-col min-h-screen")(
                _navbar(),
                Div(cls="max-w-7xl w-full mx-auto px-4 sm:px-6 py-6")(
                    *content,
                ),
                Footer(cls="footer footer-center bg-base-100 border-t border-base-300 p-4 text-sm text-base-content/40")(
                    Span("SEO Otter — Google Search Console Intelligence"),
                ),
            ),
            Div(cls="drawer-side z-30")(
                Label(for_="drawer-toggle", cls="drawer-overlay"),
                _sidebar(),
            ),
        ),
    )


app, rt = fast_app(
    hdrs=page_hdrs,
    body_wrap=_body_wrap,
    pico=False,
)


def _run_sync(id: int, site_url: str, days: int, custom_start: str = "", custom_end: str = ""):
    with get_session() as session:
        secrets = CONFIG_DIR / "client_secrets.json"
        auth = GSCAuth(secrets_file=str(secrets))
        try:
            auth.get_credentials()
        except ValueError:
            _sync_progress[id] = {"status": "error", "msg": "GSC not authenticated"}
            return
        start, end = resolve_dates(days, custom_start, custom_end)
        dates = get_missing_dates(session, site_url, start, end)
        total = len(dates)
        if total == 0:
            _sync_progress[id] = {"status": "done", "days": 0, "records": 0}
            return
        _sync_progress[id] = {"status": "running", "total": total, "done": 0, "records": 0}
        for date in dates:
            try:
                count = store_single_date(session, auth, site_url, date)
                _sync_progress[id].update(done=_sync_progress[id]["done"] + 1, records=_sync_progress[id]["records"] + count)
            except Exception as e:
                _sync_progress[id] = {"status": "error", "msg": str(e)}
                return
            time.sleep(1)
        p = _sync_progress[id]
        _sync_progress[id] = {"status": "done", "days": p["done"], "records": p["records"]}


def _run_index_check(id: int, site_url: str, sitemap_url: str):
    try:
        urls = fetch_sitemap_urls(sitemap_url)
    except Exception as e:
        _index_check_progress[id] = {"status": "error", "msg": str(e)}
        return
    total = len(urls)
    if total == 0:
        _index_check_progress[id] = {"status": "done", "total": 0, "successful": 0, "failed": 0}
        return
    _index_check_progress[id] = {"status": "running", "total": total, "done": 0, "successful": 0, "failed": 0}
    secrets = CONFIG_DIR / "client_secrets.json"
    auth = GSCAuth(secrets_file=str(secrets))
    try:
        auth.get_credentials()
    except ValueError:
        _index_check_progress[id] = {"status": "error", "msg": "GSC not authenticated"}
        return
    with get_session() as session:
        for i, url in enumerate(urls, 1):
            try:
                store_index_status(session, auth, site_url, url)
                _index_check_progress[id]["successful"] += 1
            except Exception:
                _index_check_progress[id]["failed"] += 1
            _index_check_progress[id]["done"] = i
            time.sleep(1)
    p = _index_check_progress[id]
    _index_check_progress[id] = {"status": "done", "total": total, "successful": p["successful"], "failed": p["failed"]}


def render_nav_cards(id: int):
    items = [
        (report, "SEO Report", "Full site audit", "📋", True),
        (keywords, "Keywords", "Search queries & trends", "🔑", True),
        (top_pages, "Top Pages", "Pages ranked by metrics", "📄", True),
        (wins, "Wins", "Opportunities to capture", "🏆", True),
        (index_status, "Index Status", "Coverage & errors", "🔍", True),
        (canb, "Cannibalization", "Duplicate content", "🔗", False),
        (schema_check, "Schema Check", "Structured data", "✅", False),
        (faq, "FAQ", "Schema questions", "❓", False),
        (serpwatcher, "SERPWatcher", "Tracked keywords", "📊", False),
        (countries, "Countries", "Geo breakdown", "🌍", False),
        (articles, "Articles", "SEO inference & metadata", "📝", False),
    ]
    return Div(cls="grid grid-cols-2 sm:grid-cols-3 lg:grid-cols-4 xl:grid-cols-5 gap-3")(
        *[A(
            Div(cls="card-body px-4 py-4")(
                Div(cls="flex items-start justify-between mb-2")(
                    Span(icon, cls="text-2xl"),
                    Span("→", cls="text-base-content/30 group-hover:text-primary transition-colors"),
                ),
                Div(label, cls="font-semibold text-sm"),
                P(desc, cls="text-xs text-base-content/50 mt-0.5 line-clamp-2"),
            ),
            href=route.to(id=id),
            cls="card card-border bg-base-100 hover:bg-base-200 hover:border-primary hover:shadow-md hover:-translate-y-0.5 transition-all duration-200 no-underline group",
        ) for route, label, desc, icon, _primary in items]
    )


SITE_TYPE_ICONS = {"quarto": "📘", "astro": "🚀", "hugo": "⚡", "wordpress": "🔵", "other": "🌐"}

FAVICON_CACHE = Path.home() / ".config" / "seootter" / "favicons"

def _favicon_url(domain: str, size: int = 32) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz={size}&default=emoji"

def render_site_card(w):
    domain = w.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
    fallback_emoji = SITE_TYPE_ICONS.get(w.site_type or "other", "🌐")
    site_type = (w.site_type or "other").title()
    return A(
        Div(cls="card-body p-5")(
            Div(cls="flex items-start justify-between mb-2")(
                Div(cls="flex items-center gap-3 min-w-0")(
                    Div(cls="w-10 h-10 flex items-center justify-center bg-base-200 rounded-lg shrink-0")(
                        Img(src=_favicon_url(domain), alt="", cls="w-6 h-6 rounded",
                            loading="lazy", style="display:block",
                            onerror=f"this.style.display='none';this.nextElementSibling.style.display='block'"),
                        Span(fallback_emoji, cls="text-xl", style="display:none"),
                    ),
                    Div(cls="min-w-0")(
                        H3(w.name or domain, cls="card-title text-base truncate"),
                        P(domain, cls="text-sm text-base-content/50 truncate"),
                    ),
                ),
                Span(cls=f"badge badge-soft badge-sm shrink-0")(site_type),
            ),
            P(w.desc, cls="text-sm text-base-content/60 line-clamp-2 mb-3") if w.desc else "",
            Div(cls="flex items-center gap-3 text-xs text-base-content/40 pt-2 border-t border-base-200")(
                Span(cls="flex items-center gap-1")(Span("🌐"), Span(w.lang.upper())),
                Span("·"),
                Span(cls="flex items-center gap-1")(Span("🆔"), Span(f"ID {w.id}")),
            ),
        ),
        href=site.to(id=w.id),
        cls="card card-border bg-base-100 hover:bg-base-200 hover:shadow-lg hover:-translate-y-1 transition-all duration-200 no-underline group",
    )


def render_add_form():
    return Card(cls="card-border bg-base-100")(
        CardBody(
            Div(cls="flex items-start gap-3 mb-2")(
                Span("➕", cls="text-2xl"),
                Div()(
                    H2("Add a new website", cls="card-title text-xl"),
                    P("Add a website to start tracking its Google Search Console data and SEO performance.", cls="text-sm text-base-content/60 mt-1"),
                ),
            ),
            Form(
                Div(cls="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4 mt-4")(
                    Fieldset(FieldsetLegend(cls="text-xs font-medium")("URL *"),
                        Input(name="url", placeholder="https://example.com", required=True, cls="input w-full"),
                        P("The full URL of your site", cls="text-xs text-base-content/50 mt-1")),
                    Fieldset(FieldsetLegend(cls="text-xs font-medium")("Name *"),
                        Input(name="name", placeholder="My Site", required=True, cls="input w-full"),
                        P("A friendly name for this site", cls="text-xs text-base-content/50 mt-1")),
                    Fieldset(FieldsetLegend(cls="text-xs font-medium")("Site Type *"),
                        Select(
                            Option("Quarto", value="quarto", selected=True),
                            Option("Astro", value="astro"),
                            Option("Hugo", value="hugo"),
                            Option("WordPress", value="wordpress"),
                            name="site_type", required=True, cls="select w-full"),
                        P("Determines how content is mapped", cls="text-xs text-base-content/50 mt-1")),
                    Fieldset(FieldsetLegend(cls="text-xs font-medium")("Description"),
                        Input(name="desc", placeholder="Optional description", cls="input w-full")),
                    Fieldset(FieldsetLegend(cls="text-xs font-medium")("Language"),
                        Select(
                            Option("English (en)", value="en", selected=True),
                            Option("Arabic (ar)", value="ar"),
                            Option("French (fr)", value="fr"),
                            Option("Spanish (es)", value="es"),
                            Option("German (de)", value="de"),
                            Option("Italia (it)", value="it"),
                            Option("Portugal (pt)", value="pt"),
                            Option("Chinese (zh)", value="zh"),
                            name="lang", cls="select w-full"),
                        P("Two-letter language code", cls="text-xs text-base-content/50 mt-1")),
                    Fieldset(FieldsetLegend(cls="text-xs font-medium")("Content Directory"),
                        Input(name="content_dir", placeholder="/path/to/content", cls="input w-full"),
                        P("Local path to your content files (for SEO reports)", cls="text-xs text-base-content/50 mt-1")),
                ),
                Div(cls="mt-6 flex items-center justify-between gap-2 pt-4 border-t border-base-200")(
                    P("After adding, you'll be able to sync GSC data from the site dashboard.", cls="text-xs text-base-content/50"),
                    Div(cls="flex gap-2")(
                        Btn("Clear", cls="-ghost", type="reset"),
                        Btn("Add Website →", cls="-primary", type="submit"),
                    ),
                ),
                action=add_website.to(), method="post",
            ),
        ),
    )


@rt
def index():
    with get_session() as session:
        websites = session.exec(select(Website)).all()
        stores = session.exec(select(WuiltStore)).all()
        total = len(websites)
        total_stores = len(stores)

        last_sync_date = None
        if websites:
            from sqlmodel import func as _func
            for w in websites:
                domain = w.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
                site_url = get_site_url(w.url)
                d = session.exec(select(_func.max(GSCAnalytics.date)).where(GSCAnalytics.site_url == site_url)).first()
                if d:
                    if isinstance(d, str):
                        from datetime import datetime as _dt
                        try:
                            d = _dt.strptime(d, "%Y-%m-%d").date()
                        except (ValueError, TypeError):
                            d = None
                    if d and (last_sync_date is None or d > last_sync_date):
                        last_sync_date = d

        last_sync_str = last_sync_date.strftime("%b %d, %Y") if hasattr(last_sync_date, 'strftime') else "Never"

        return Titled("SEO Otter Dashboard",
            Div(cls="page-header page-fade")(
                H1("Dashboard", cls="text-2xl font-bold"),
                P("Monitor your sites' Google Search performance", cls="subtitle"),
            ),
            Div(cls="grid grid-cols-1 sm:grid-cols-3 gap-4 mb-6")(
                stat_card("Websites", total, icon="🌐"),
                stat_card("Wuilt Stores", total_stores, icon="🏪"),
                stat_card("Latest data", last_sync_str, icon="📅"),
            ),
            Div(cls="flex items-center justify-between mb-4")(
                H2("Your websites", cls="text-lg font-semibold"),
                Span(f"{total} site{'s' if total != 1 else ''}", cls="badge badge-soft badge-sm"),
            ) if websites else "",
            Div(cls="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-4")(
                *map(render_site_card, websites),
            ) if websites else empty_state(
                "🚀",
                "Welcome to SEO Otter",
                "Add your first website below to start syncing Google Search Console data and unlock SEO insights. You can also manage Wuilt e-commerce stores from the sidebar.",
            ),
            Div(cls="mt-8")(
                hint_box([
                    Span(Strong("Tip: "), "Use the sidebar to navigate between your websites, Wuilt stores, and settings. Each site dashboard has tools for keywords, wins, content analysis, and more.", cls=""),
                ], icon="💡"),
            ) if websites else "",
            Divider(cls="my-8"),
            render_add_form(),
        )


@rt
def add_website(url: str, name: str, site_type: str, desc: str = "", lang: str = "en", content_dir: str = ""):
    with get_session() as session:
        add_or_update_website(session, url=url, name=name, site_type=site_type, desc=desc, lang=lang, content_dir=content_dir)
    return RedirectResponse("/", status_code=303)


@rt
def delete_website(id: int):
    with get_session() as session:
        website = session.get(Website, id)
        if website:
            session.delete(website)
            session.commit()
    return RedirectResponse("/", status_code=303)


@rt("/site/{id}")
def site_redirect(id: int):
    return RedirectResponse(site.to(id=id), status_code=301)


def render_sync_widget(id: int, days: int = 90, custom_start: str = "", custom_end: str = ""):
    days = int(days)
    prog = _sync_progress.get(id)
    
    db_dates_text = ""
    with get_session() as session:
        website = session.get(Website, id)
        if website:
            from sqlalchemy import text
            domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
            site_url = get_site_url(website.url)
            res = session.exec(text("SELECT min(date), max(date) FROM gscpropertytotals WHERE site_url = :url").bindparams(url=site_url)).first()
            if res and res[0] and res[1]:
                db_dates_text = f"Data: {res[0]} to {res[1]}"
            else:
                db_dates_text = "No data yet"

    if prog and prog.get("status") == "running":
        total = prog.get("total", 0)
        done = prog.get("done", 0)
        pct = int(done / total * 100) if total > 0 else 0
        return Div(
            Progress(cls="-primary w-full h-2", value=str(pct), max="100"),
            Span(f"Syncing GSC: {pct}% ({done}/{total} dates)", cls="text-xs font-semibold text-primary block mt-1"),
            hx_get=sync_widget.to(id=id, days=days, custom_start=custom_start, custom_end=custom_end),
            hx_trigger="every 2s",
            hx_target="this",
            hx_swap="outerHTML",
        )
    if prog and prog.get("status") == "done":
        _sync_progress[id] = None  # Reset progress status so button shows next time
        days_synced = prog.get('days', 0)
        msg = f"✅ Sync complete! ({days_synced} dates)" if days_synced > 0 else "✅ Up to date!"
        return Div(
            Span(msg, cls="text-xs text-success font-semibold mr-2"),
            A("🔄 Refresh", href=f"/site?id={id}&days={days}&custom_start={custom_start}&custom_end={custom_end}", cls="btn btn-xs btn-success btn-soft"),
        )
    if prog and prog.get("status") == "error":
        _sync_progress[id] = None  # Reset progress status so try again shows
        return Div(
            Span(f"❌ Error: {prog.get('msg', 'Unknown')}", cls="text-xs text-error font-semibold mr-2"),
            Btn("🔄 Try Again",
                cls="btn btn-xs btn-error btn-soft",
                hx_post=sync_start.to(id=id, days=days, custom_start=custom_start, custom_end=custom_end),
                hx_target="#sync-container",
                hx_swap="outerHTML",
            )
        )
    return Div(
        Div(db_dates_text, cls="text-xs text-base-content/50 whitespace-nowrap mr-3 font-medium"),
        Div(
            Btn("🔄 Sync GSC Data",
                cls="btn-outline btn-primary btn-sm",
                hx_post=sync_start.to(id=id, days=days, custom_start=custom_start, custom_end=custom_end),
                hx_target="#sync-container",
                hx_swap="outerHTML",
                hx_indicator="#sync-loading"
            ),
            Div(Span(cls="loading loading-spinner loading-xs inline-block align-middle mr-1"),
                "Fetching data...", 
                id="sync-loading", 
                cls="htmx-indicator text-xs text-primary font-semibold mt-1 absolute left-0 whitespace-nowrap"
            ),
            cls="relative"
        ),
        cls="flex items-center"
    )


@rt
def sync_start(id: int, days: int = 90, custom_start: str = "", custom_end: str = ""):
    days = int(days)
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
    _sync_progress[id] = {"status": "running", "done": 0, "total": 0, "records": 0}
    Thread(target=_run_sync, args=(id, site_url, days, custom_start, custom_end), daemon=True).start()
    return render_sync_widget(id, days, custom_start, custom_end)


@rt
def sync_widget(id: int, days: int = 90, custom_start: str = "", custom_end: str = ""):
    return render_sync_widget(id, days, custom_start, custom_end)



_latest_db_date_engine = None

def _latest_db_date() -> str:
    global _latest_db_date_engine
    try:
        from sqlmodel import Session, text
        import os
        db_url = os.getenv("SEEOOTTER_DB_URL", "")
        if not db_url:
            return ""
        if _latest_db_date_engine is None:
            from sqlmodel import create_engine
            _latest_db_date_engine = create_engine(db_url)
        with Session(_latest_db_date_engine) as session:
            return session.exec(text("SELECT MAX(date) FROM gscanalytics")).scalar()
    except Exception as ex:
        import logging
        logging.getLogger(__name__).warning(f"_latest_db_date failed: {ex}")
        return ""

def resolve_dates(days: int, custom_start: str, custom_end: str) -> tuple[str, str]:
    from seootter.gsc_client import get_date_range
    if days == 1:
        latest = _latest_db_date()
        if latest:
            return latest, latest
        return get_date_range("today")
    elif days == 0:
        return custom_start, custom_end
    else:
        return get_date_range("last_days", days=days)

def render_custom_date_inputs(current_days: int, custom_start: str, custom_end: str):
    if current_days != 0:
        return ""
    return Div(
        Input(type="date", name="custom_start", value=custom_start, cls="input input-sm input-bordered w-32"),
        Input(type="date", name="custom_end", value=custom_end, cls="input input-sm input-bordered w-32"),
        Button("Apply", type="submit", cls="btn btn-sm btn-primary"),
        cls="flex items-center gap-2 ml-2"
    )

DAYS_OPTIONS = [(1, "Today"), (3, "3 Days"), (7, "7 Days"), (15, "15 Days"), (30, "30 Days"), (90, "3 Months"), (180, "6 Months"), (240, "8 Months"), (365, "12 Months"), (480, "16 Months"), (0, "Custom")]

def get_days_label(days: int) -> str:
    return dict(DAYS_OPTIONS).get(days, f"{days} days")

@rt
def site(id: int, days: int = 30, custom_start: str = "", custom_end: str = ""):
    days = int(days)
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))

        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)

        metrics = get_site_metrics(session, site_url, days=days, custom_start=custom_start, custom_end=custom_end)
        start, end = resolve_dates(days, custom_start, custom_end)
        tp = get_top_pages(session, site_url, start, end, limit=10)

        delete_modal_id = f"delete-modal-{id}"

        return Title(website.name), Main(cls="container")(
            A("← Dashboard", href="/", cls="text-sm text-base-content/60 hover:text-primary mb-3 inline-flex items-center gap-1"),
            Div(cls="page-header page-fade")(
                Div(cls="page-header-row")(
                    Div(cls="flex items-center gap-3 grow min-w-0")(
                        Img(src=_favicon_url(domain, 48), alt="", cls="w-12 h-12 rounded-lg bg-base-200 p-1 hidden sm:block",
                            loading="lazy",
                            onerror=f"this.style.display='none'"),
                        Div(cls="min-w-0")(
                            H1(website.name, cls="text-2xl font-bold truncate"),
                            P(website.url, cls="text-sm text-base-content/50 truncate"),
                        ),
                    ),
                    Div(cls="page-header actions")(
                        Form(
                            Input(type="hidden", name="id", value=str(id)),
                            Select(
                                *[Option(label, value=str(val), selected=(days == val)) for val, label in DAYS_OPTIONS],
                                cls="select select-bordered select-sm mr-2",
                                name="days",
                                onchange="this.form.submit()"
                            ),
                            render_custom_date_inputs(days, custom_start, custom_end),
                            action="/site", method="get", cls="flex items-center"
                        ),
                        Button("🗑 Delete", cls="btn btn-soft btn-error btn-sm mr-2",
                               onclick=f"document.getElementById('{delete_modal_id}').showModal()"),
                        Div(id="sync-container", cls="inline-block align-middle")(
                            render_sync_widget(id, days, custom_start, custom_end)
                        ),
                    ),
                ),
            ),
            Dialog(id=delete_modal_id, cls="modal")(
                Div(cls="modal-box")(
                    Div(cls="flex items-center gap-3 mb-3")(
                        Span("⚠️", cls="text-2xl"),
                        H3("Delete this website?", cls="font-bold text-lg"),
                    ),
                    P(f"This will permanently delete ", Span(website.name, cls="font-semibold"), " and all of its associated data including GSC analytics, tracked keywords, and SEO reports. This action cannot be undone.", cls="text-base-content/60 mb-6"),
                    Div(cls="modal-action")(
                        Form(method="dialog")(Button("Cancel", cls="btn btn-ghost")),
                        Form(method="post", action=delete_website.to(), cls="inline")(
                            Input(type="hidden", name="id", value=str(id)),
                            Button("Yes, delete forever", cls="btn btn-error", type="submit"),
                        ),
                    ),
                ),
                Form(method="dialog", cls="modal-backdrop")(Button("close")),
            ),
            render_metrics(metrics, session, site_url, days=days),
            Div(cls="section mt-6")(
                Div(cls="flex items-center justify-between mb-3")(
                    Div()(
                        H2("📄 Top Pages", cls="section-title"),
                        P(f"Your highest-trafficking pages over the last {get_days_label(days)}", cls="section-subtitle"),
                    ),
                    A("View all →", href=top_pages.to(id=id), cls="btn btn-ghost btn-sm"),
                ),
                render_top_pages_table(tp),
            ) if tp else Div(cls="section mt-6")(
                Div(cls="flex items-center justify-between mb-3")(
                    Div()(
                        H2("📄 Top Pages", cls="section-title"),
                        P(f"Your highest-trafficking pages over the last {get_days_label(days)}", cls="section-subtitle"),
                    ),
                    A("Sync now →", href="#", cls="btn btn-primary btn-sm",
                      onclick=f"document.getElementById('sync-btn-area').querySelector('button[hx_post]').click()"),
                ),
                empty_state("📊", "No GSC data yet", "Sync your Google Search Console data to see your top-performing pages here.", cta_text="Sync Now", cta_href="#"),
            ),
            Div(cls="section mt-6")(
                Div(cls="flex items-center justify-between mb-4")(
                    Div()(
                        H2("🛠 Site Tools", cls="section-title"),
                        P("Explore SEO insights for this website", cls="section-subtitle"),
                    ),
                ),
                render_nav_cards(id),
            ),
        ),


def get_site_metrics(session, site_url, days=30, custom_start="", custom_end=""):
    from seootter.gsc.queries import get_property_totals
    start, end = resolve_dates(days, custom_start, custom_end)
    totals = get_property_totals(session, site_url, start, end)
    return {
        "clicks": totals["clicks"],
        "impressions": totals["impressions"],
        "avg_position": round(totals["avg_position"], 1),
        "avg_ctr": round(totals["avg_ctr"] * 100, 2),
    }


METRIC_ICONS = {
    "clicks": "👆",
    "impressions": "👁️",
    "avg_position": "📍",
    "avg_ctr": "📈",
}

def render_metrics(metrics, session=None, site_url=None, days=30):
    lbl = get_days_label(days)
    items = [
        ("clicks", f"{metrics['clicks']:,}", f"Clicks ({lbl})", "👆"),
        ("impressions", f"{metrics['impressions']:,}", f"Impressions ({lbl})", "👁️"),
        ("avg_position", f"{metrics['avg_position']:.1f}", "Avg Position", "📍"),
        ("avg_ctr", f"{metrics['avg_ctr']:.1f}%", "Avg CTR", "📈"),
    ]
    has_data = metrics['clicks'] > 0 or metrics['impressions'] > 0
    return Div(cls="grid grid-cols-2 lg:grid-cols-4 gap-4")(
        *[stat_card(label, value, icon=icon) for key, value, label, icon in items],
    ) if has_data else Div(cls="grid grid-cols-2 lg:grid-cols-4 gap-4")(
        *[stat_card(label, value, icon=icon) for key, value, label, icon in items],
    )


def render_top_pages_table(rows):
    if not rows:
        return P("No page data available.", cls="text-sm text-base-content/50 italic")
    return Div(cls="overflow-x-auto")(
        Table(cls="table table-zebra table-hover")(
            Thead(Tr(
                Th("#", cls="w-8"),
                Th("Page"),
                Th("Clicks", cls="text-right"),
                Th("Impr", cls="text-right"),
                Th("Pos", cls="text-right"),
                Th("CTR", cls="text-right"),
            )),
            Tbody(*[
                Tr(
                    Td(Span(str(i + 1), cls="text-base-content/40 text-xs"), cls="font-mono"),
                    Td(
                        A(r["page"], href=r["page"], target="_blank", cls="link link-hover text-sm truncate inline-block max-w-md",
                          title=r["page"]),
                    ),
                    Td(f"{r['total_clicks']:,}", cls="text-right font-medium"),
                    Td(f"{r['total_impressions']:,}", cls="text-right text-base-content/70"),
                    Td(Span(f"{r['avg_position']:.1f}", cls="badge badge-ghost badge-sm"), cls="text-right"),
                    Td(Span(f"{r['avg_ctr'] * 100:.1f}%", cls=("text-success" if r['avg_ctr'] * 100 >= 5 else "text-base-content/70")), cls="text-right"),
                ) for i, r in enumerate(rows[:10])
            ]),
        ),
    )


SORT_OPTIONS = [
    ("clicks", "Most Clicks"),
    ("impressions", "Most Impressions"),
    ("ctr", "Best CTR"),
    ("position", "Best Position"),
]

# DAYS_OPTIONS is defined globally above


def render_top_pages_filters(id: int, current_sort: str, current_days: int, current_country: str, countries: list[tuple[str, str]], custom_start: str = "", custom_end: str = ""):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Sort:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(label, href=top_pages.to(id=id, sort=key, days=current_days, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if key == current_sort else ""))
              for key, label in SORT_OPTIONS]
        ),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=top_pages.to(id=id, sort=current_sort, days=d, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Country:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="sort", value=current_sort, type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Select(
                *[Option(label, value=v, selected=(v == current_country)) for v, label in countries],
                name="country", cls="select select-sm w-44",
                onchange="this.form.submit()",
            ),
            render_custom_date_inputs(current_days, custom_start, custom_end),
            action=top_pages.to(), method="get"
        ),
    )


def render_top_pages_full(rows):
    if not rows:
        return P("No top pages data")
    return Table(
        Thead(Tr(Th("#"), Th("Page"), Th("Clicks"), Th("Impr"), Th("Pos"), Th("CTR"))),
        Tbody(*[
            Tr(
                Td(str(i + 1)),
                Td(r["page"][:60] + "..." if len(r["page"]) > 60 else r["page"]),
                Td(f"{r['total_clicks']:,}"),
                Td(f"{r['total_impressions']:,}"),
                Td(f"{r['avg_position']:.1f}"),
                Td(f"{r['avg_ctr'] * 100:.1f}%"),
            ) for i, r in enumerate(rows[:50])
        ]),
        cls="-zebra",
    )


@rt
def top_pages(id: int, sort: str = "clicks", days: int = 30, country: str = "", export: str = "", custom_start: str = "", custom_end: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        start, end = resolve_dates(days, custom_start, custom_end)
        rows = get_top_pages(session, site_url, start, end, limit=200, country=country or None)

        key = {"clicks": "total_clicks", "impressions": "total_impressions",
               "ctr": "avg_ctr", "position": "avg_position"}.get(sort, "total_clicks")
        reverse = sort != "position"
        rows.sort(key=lambda r: r[key], reverse=reverse)

        if export:
            h = ["Page", "Clicks", "Impressions", "Position", "CTR"]
            d = [[r["page"], r["total_clicks"], r["total_impressions"],
                   round(r["avg_position"], 1), f"{r['avg_ctr']*100:.1f}%"] for r in rows[:50]]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "top_pages", export)

        q = (
            select(GSCAnalytics.country, func.sum(GSCAnalytics.impressions).label("total"))
            .where(GSCAnalytics.site_url == site_url, GSCAnalytics.country != None)
            .group_by(GSCAnalytics.country)
            .order_by(func.sum(GSCAnalytics.impressions).desc())
            .limit(15)
        )
        countries = [("", "All Countries")]
        for r in session.exec(q):
            try:
                name = pycountry.countries.get(alpha_3=r.country.upper()).name
            except:
                name = r.country.upper()
            countries.append((r.country, name))

    label = dict(SORT_OPTIONS).get(sort, "Top Pages")
    days_label = dict(DAYS_OPTIONS).get(days, f"{days} days")
    country_name = dict(countries).get(country, "All Countries")
    return Title(f"{label} - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1(label, cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=top_pages.to(id=id, sort=sort, days=days, country=country, export="csv"))),
                    Li(A("Markdown", href=top_pages.to(id=id, sort=sort, days=days, country=country, export="md"))),
                ),
            ),
        ),
        P(f"{website.url} — {days_label}, {country_name}", cls="text-base-content/60 mb-4"),
        render_top_pages_filters(id, sort, days, country, countries),
        render_top_pages_full(rows),
    ),


INTENTS = ["", "informational", "navigational", "transactional", "commercial", "comparison"]
INTENT_LABELS = {"": "All", "informational": "Info", "navigational": "Nav", "transactional": "Trans",
                 "commercial": "Comm", "comparison": "Comp"}
INTENT_COLORS = {"informational": "badge-info", "navigational": "badge-accent",
                 "transactional": "badge-success", "commercial": "badge-warning", "comparison": "badge-primary"}
TREND_ICONS = {"rising": "📈", "declining": "📉", "stable": "→"}
TREND_COLORS = {"rising": "text-success", "declining": "text-error", "stable": "text-base-content/60"}


def render_keywords_filters(id: int, current_intent: str, green_only: bool, issues_only: bool,
                            current_days: int, current_country: str, countries: list[tuple[str, str]]):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Intent:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(INTENT_LABELS[k], href=keywords.to(id=id, intent=k, green_only=green_only, issues_only=issues_only, days=current_days, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if k == current_intent else ""))
              for k in INTENTS]
        ),
        A("🟢 Green", href=keywords.to(id=id, intent=current_intent, green_only=not green_only, issues_only=issues_only, days=current_days, country=current_country),
          cls="btn btn-xs" + (" btn-success" if green_only else " btn-outline")),
        A("⚠ Issues", href=keywords.to(id=id, intent=current_intent, green_only=green_only, issues_only=not issues_only, days=current_days, country=current_country),
          cls="btn btn-xs" + (" btn-warning" if issues_only else " btn-outline")),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=keywords.to(id=id, intent=current_intent, green_only=green_only, issues_only=issues_only, days=d, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Country:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="intent", value=current_intent, type="hidden"),
            Input(name="green_only", value=str(green_only).lower(), type="hidden"),
            Input(name="issues_only", value=str(issues_only).lower(), type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Select(*[Option(label, value=v, selected=(v == current_country)) for v, label in countries],
                   name="country", cls="select select-sm w-44", onchange="this.form.submit()"),
            action=keywords.to(), method="get",
        ),
    )


def render_keyword_row(r: dict):
    trend = r.get("trend", "stable")
    intent = r.get("intent", "informational")
    return Tr(
        Td(A(r["query"], href=f"https://www.google.com/search?q={r['query']}", target="_blank", cls="link link-primary font-medium")),
        Td(f"{r['total_clicks']:,}"),
        Td(f"{r['total_impressions']:,}"),
        Td(f"{r['avg_position']:.1f}"),
        Td(Span(TREND_ICONS.get(trend, "→"), cls=TREND_COLORS.get(trend, ""))),
        Td(Span(INTENT_LABELS.get(intent, intent), cls=f"badge {INTENT_COLORS.get(intent, 'badge-ghost')} badge-sm")),
        Td("🟢" if r.get("green") else "—"),
        Td("⚠" if r.get("cannibal") else "—"),
        hx_get=keyword_pages.to(id=r["_site_id"], query=r["query"], days=r["_days"], country=r["_country"]),
        hx_target="#keyword-detail",
        hx_swap="innerHTML",
        cls="cursor-pointer hover:bg-base-200",
    )


def render_keywords_table(rows: list[dict]):
    if not rows:
        return Div(id="keywords-rows")(P("No matching keywords.", cls="text-sm text-base-content/60"))
    return Div(id="keywords-rows")(
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Keyword"), Th("Clicks"), Th("Impr"), Th("Pos"), Th("Trend"), Th("Intent"), Th("🟢"), Th("⚠"))),
                Tbody(*map(render_keyword_row, rows)),
                cls="-zebra",
            ),
        ),
    )


@rt
def keywords(id: int, intent: str = "", green_only: bool = False, issues_only: bool = False,
             days: int = 30, country: str = "", export: str = "", custom_start: str = "", custom_end: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        start, end = resolve_dates(days, custom_start, custom_end)

        rows = get_top_queries(session, site_url, start, end, country=country or None, limit=500)
        trends = detect_query_trends(session, site_url, days=days, limit=500)
        intents = classify_page_intents(session, site_url, start, end, country=country or None, limit=500)
        cannib = find_cannibalized(session, id, site_url, start, end)
        cannib_queries = {c["query"] for c in cannib.get("gsc_matches", [])}

        trend_map = {t["query"]: t for t in trends}
        intent_map = {i["query"]: i["intent"] for i in intents}

        table_rows = []
        for r in rows:
            tr = trend_map.get(r["query"], {})
            is_rising = tr.get("trend") == "rising"
            is_green = is_rising and tr.get("recent_impressions", 0) > 500 and tr.get("recent_position", 100) > 8
            row = dict(r, trend=tr.get("trend", "stable"), intent=intent_map.get(r["query"], "informational"),
                       green=is_green, cannibal=r["query"] in cannib_queries,
                       _site_id=id, _days=days, _country=country)
            table_rows.append(row)

        if intent:
            table_rows = [r for r in table_rows if r["intent"] == intent]
        if green_only:
            table_rows = [r for r in table_rows if r["green"]]
        if issues_only:
            table_rows = [r for r in table_rows if r["green"] or r["cannibal"]]

        if export:
            h = ["Keyword", "Clicks", "Impressions", "Position", "CTR", "Trend", "Intent"]
            d = [[r["query"], r.get("total_clicks", r.get("clicks", 0)),
                   r.get("total_impressions", r.get("impressions", 0)),
                   round(r.get("avg_position", r.get("position", 0)), 1),
                   f"{r.get('avg_ctr', r.get('ctr', 0))*100:.1f}%",
                   r["trend"], r["intent"]] for r in table_rows]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "keywords", export)

        q = (select(GSCAnalytics.country, func.sum(GSCAnalytics.impressions).label("total"))
             .where(GSCAnalytics.site_url == site_url, GSCAnalytics.country != None)
             .group_by(GSCAnalytics.country).order_by(func.sum(GSCAnalytics.impressions).desc()).limit(15))
        countries = [("", "All Countries")]
        for rc in session.exec(q):
            try:
                name = pycountry.countries.get(alpha_3=rc.country.upper()).name
            except:
                name = rc.country.upper()
            countries.append((rc.country, name))

    return Title(f"Keywords - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("Keywords", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=keywords.to(id=id, intent=intent, green_only=green_only, issues_only=issues_only, days=days, country=country, export="csv"))),
                    Li(A("Markdown", href=keywords.to(id=id, intent=intent, green_only=green_only, issues_only=issues_only, days=days, country=country, export="md"))),
                ),
            ),
        ),
        P(f"{website.url} — {dict(DAYS_OPTIONS).get(days, f'{days} days')}, {dict(countries).get(country, 'All Countries')}",
          cls="text-base-content/60 mb-4"),
        render_keywords_filters(id, intent, green_only, issues_only, days, country, countries),
        render_keywords_table(table_rows),
        Div(id="keyword-detail", cls="mt-4"),
    ),


@rt
def keywords_rows(id: int, intent: str = "", green_only: bool = False, issues_only: bool = False,
                  days: int = 30, country: str = ""):
    """HTMX fragment: just the table rows for filter refreshes."""
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        start, end = resolve_dates(days, custom_start, custom_end)

        rows = get_top_queries(session, site_url, start, end, country=country or None, limit=500)
        trends = detect_query_trends(session, site_url, days=days, limit=500)
        intents = classify_page_intents(session, site_url, start, end, country=country or None, limit=500)
        cannib = find_cannibalized(session, id, site_url, start, end)
        cannib_queries = {c["query"] for c in cannib.get("gsc_matches", [])}

        trend_map = {t["query"]: t for t in trends}
        intent_map = {i["query"]: i["intent"] for i in intents}

        table_rows = []
        for r in rows:
            tr = trend_map.get(r["query"], {})
            is_rising = tr.get("trend") == "rising"
            is_green = is_rising and tr.get("recent_impressions", 0) > 500 and tr.get("recent_position", 100) > 8
            row = dict(r, trend=tr.get("trend", "stable"), intent=intent_map.get(r["query"], "informational"),
                       green=is_green, cannibal=r["query"] in cannib_queries,
                       _site_id=id, _days=days, _country=country)
            table_rows.append(row)

        if intent:
            table_rows = [r for r in table_rows if r["intent"] == intent]
        if green_only:
            table_rows = [r for r in table_rows if r["green"]]
        if issues_only:
            table_rows = [r for r in table_rows if r["green"] or r["cannibal"]]

    return render_keywords_table(table_rows)


@rt
def keyword_pages(id: int, query: str, days: int = 30, country: str = "", custom_start: str = "", custom_end: str = ""):
    """HTMX fragment: pages ranking for a keyword."""
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        start, end = resolve_dates(days, custom_start, custom_end)
        pages = session.exec(
            select(GSCAnalytics.page, func.sum(GSCAnalytics.clicks).label("clicks"),
                   func.sum(GSCAnalytics.impressions).label("impressions"),
                   func.avg(GSCAnalytics.position).label("position"))
            .where(GSCAnalytics.site_url == site_url, GSCAnalytics.query == query,
                   GSCAnalytics.date.between(start, end))
            .group_by(GSCAnalytics.page).order_by(func.sum(GSCAnalytics.clicks).desc()).limit(10)
        ).all()
    return Div(
        H3(f"Pages for: {query}", cls="text-lg font-semibold mb-2"),
        Table(
            Thead(Tr(Th("Page"), Th("Clicks"), Th("Impr"), Th("Pos"))),
            Tbody(*[Tr(Td(p.page[:80] + "..." if len(p.page) > 80 else p.page),
                       Td(f"{p.clicks:,}"), Td(f"{p.impressions:,}"), Td(f"{p.position:.1f}"))
                    for p in pages]) if pages else Tr(Td("No data", colspan="4")),
            cls="-zebra",
        ) if pages else P("No ranking pages found.", cls="text-sm text-base-content/60"),
        cls="border border-base-300 rounded-lg p-4",
    )


def render_wins_filters(id: int, current_intent: str, current_days: int, current_country: str, countries: list[tuple[str, str]], custom_start: str = "", custom_end: str = ""):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Intent:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(INTENT_LABELS[k], href=wins.to(id=id, intent=k, days=current_days, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if k == current_intent else ""))
              for k in INTENTS]
        ),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=wins.to(id=id, intent=current_intent, days=d, country=current_country),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Country:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="intent", value=current_intent, type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Select(*[Option(label, value=v, selected=(v == current_country)) for v, label in countries],
                   name="country", cls="select select-sm w-44", onchange="this.form.submit()"),
            render_custom_date_inputs(current_days, custom_start, custom_end),
            action=wins.to(), method="get"
        ),
    )


def render_wins_rows(rows: list[dict]):
    if not rows:
        return Div(id="wins-rows")(P("No opportunities found. Try a wider date range.", cls="text-sm text-base-content/60"))
    return Div(id="wins-rows")(
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Keyword"), Th("Clicks"), Th("Impr"), Th("Pos"), Th("CTR"), Th("Trend"), Th("Intent"))),
                Tbody(*[
                    Tr(
                        Td(A(r["query"], href=f"https://www.google.com/search?q={r['query']}", target="_blank", cls="link link-primary font-medium")),
                        Td(f"{r['total_clicks']:,}"),
                        Td(f"{r['total_impressions']:,}"),
                        Td(Span(f"{r['avg_position']:.1f}", cls="font-semibold text-warning")),
                        Td(f"{r['avg_ctr'] * 100:.1f}%"),
                        Td(Span(TREND_ICONS.get(r.get("trend", "stable"), "→"), cls=TREND_COLORS.get(r.get("trend", "")))),
                        Td(Span(INTENT_LABELS.get(r.get("intent", "informational"), ""), cls=f"badge {INTENT_COLORS.get(r.get('intent', ''), 'badge-ghost')} badge-sm")),
                    ) for r in rows
                ]),
                cls="-zebra",
            ),
        ),
    )


@rt
def wins(id: int, intent: str = "", days: int = 30, country: str = "", rows_only: bool = False, export: str = "", custom_start: str = "", custom_end: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return (P("Website not found") if rows_only
                    else Titled("Website not found", P("Website not found")))
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        start, end = resolve_dates(days, custom_start, custom_end)

        rows = get_wins(session, site_url, start, end, country=country or None, limit=200)
        trends = detect_query_trends(session, site_url, days=days, limit=500)
        intents = classify_page_intents(session, site_url, start, end, country=country or None, limit=500)
        trend_map = {t["query"]: t for t in trends}
        intent_map = {i["query"]: i["intent"] for i in intents}
        for r in rows:
            r["trend"] = trend_map.get(r["query"], {}).get("trend", "stable")
            r["intent"] = intent_map.get(r["query"], "informational")

        if intent:
            rows = [r for r in rows if r["intent"] == intent]

        if export:
            h = ["Keyword", "Clicks", "Impressions", "Position", "CTR", "Trend", "Intent"]
            d = [[r["query"], r["total_clicks"], r["total_impressions"],
                   round(r["avg_position"], 1), f"{r['avg_ctr']*100:.1f}%",
                   r["trend"], r["intent"]] for r in rows]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "wins", export)

        if rows_only:
            return render_wins_rows(rows)

        q = (select(GSCAnalytics.country, func.sum(GSCAnalytics.impressions).label("total"))
             .where(GSCAnalytics.site_url == site_url, GSCAnalytics.country != None)
             .group_by(GSCAnalytics.country).order_by(func.sum(GSCAnalytics.impressions).desc()).limit(15))
        countries = [("", "All Countries")]
        for rc in session.exec(q):
            try:
                name = pycountry.countries.get(alpha_3=rc.country.upper()).name
            except:
                name = rc.country.upper()
            countries.append((rc.country, name))

    return Title(f"Wins - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("Wins", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=wins.to(id=id, intent=intent, days=days, country=country, export="csv"))),
                    Li(A("Markdown", href=wins.to(id=id, intent=intent, days=days, country=country, export="md"))),
                ),
            ),
        ),
        P(f"High-impression, low-ranking keyword opportunities for {website.url}",
          cls="text-base-content/60 mb-4"),
        render_wins_filters(id, intent, days, country, countries),
        render_wins_rows(rows),
    ),


def render_index_stats(total, indexed, not_indexed):
    return Stats(
        Stat(StatTitle("Total Pages"), StatValue(str(total))),
        Stat(StatTitle("Indexed"), StatValue(str(indexed), cls="text-success")),
        Stat(StatTitle("Not Indexed"), StatValue(str(not_indexed), cls="text-error")),
        cls="shadow w-full mb-6",
    )


def render_index_reason_group(id: int, reason, pages):
    count = len(pages)
    group_id = f"index-group-{hash(reason) % 10000}"
    return Div(cls="mb-6")(
        H2(f"⚠ {reason} ({count})", cls="text-lg font-semibold mb-2"),
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Page URL"), Th("Verdict"), Th("Last Crawl"), Th("Indexing"), Th("Robots"))),
                Tbody(*[
                    Tr(
                        Td(A(p.page_url[:60] + ("..." if len(p.page_url) > 60 else ""),
                             href=p.page_url, target="_blank",
                             cls="link link-primary text-sm")),
                        Td(Span(p.verdict, cls="badge badge-sm badge-error")),
                        Td(p.last_crawl_time[:10] if p.last_crawl_time else "—"),
                        Td(p.indexing_state or "—"),
                        Td(p.robots_txt_state or "—"),
                        hx_get=index_page_history.to(id=id, page_url=p.page_url),
                        hx_target=f"#history-{group_id}",
                        hx_swap="innerHTML",
                        cls="cursor-pointer hover:bg-base-200",
                    ) for p in pages
                ]),
                cls="-zebra",
            ),
        ),
        Div(id=f"history-{group_id}", cls="mt-2"),
    )


def render_index_check_progress(id: int):
    prog = _index_check_progress.get(id)
    if not prog:
        return P("No check in progress", cls="text-sm")
    if prog["status"] == "fetching":
        return Div(
            Progress(cls="-primary w-full", value="0", max="100"),
            Span("Fetching sitemap...", cls="text-sm"),
            hx_get=index_check_progress.to(id=id),
            hx_trigger="every 2s",
            hx_target="this",
            hx_swap="outerHTML",
        )
    if prog["status"] == "running":
        pct = int(prog["done"] / prog["total"] * 100) if prog["total"] else 0
        return Div(
            Progress(cls="-primary w-full", value=str(pct), max="100"),
            Span(f"Checked {prog['done']}/{prog['total']} pages ({pct}%) — {prog['successful']} ok, {prog['failed']} failed", cls="text-sm"),
            hx_get=index_check_progress.to(id=id),
            hx_trigger="every 2s",
            hx_target="this",
            hx_swap="outerHTML",
        )
    if prog["status"] == "done":
        return Div(
            Progress(cls="-success w-full", value="100", max="100"),
            Span(f"Done: {prog['successful']} pages checked, {prog['failed']} failed. ", cls="text-sm text-success"),
            Link("Refresh", href=index_status.to(id=id), cls="-primary ml-2"),
        )
    if prog["status"] == "error":
        return Alert(Span(prog["msg"]), cls="-error")


@rt
def index_check_start(id: int, sitemap_url: str):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
    _index_check_progress[id] = {"status": "fetching"}
    Thread(target=_run_index_check, args=(id, site_url, sitemap_url), daemon=True).start()
    return Div(
        Progress(cls="-primary w-full", value="0", max="100"),
        Span("Checking sitemap...", cls="text-sm"),
        hx_get=index_check_progress.to(id=id),
        hx_trigger="every 2s",
        hx_target="this",
        hx_swap="outerHTML",
    )


@rt
def index_check_progress(id: int):
    return render_index_check_progress(id)


@rt
def index_page_history(id: int, page_url: str):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        history = get_index_history(session, page_url)
    if not history:
        return P("No history for this page.", cls="text-sm text-base-content/60")
    return Div(cls="border border-base-300 rounded-lg p-3 bg-base-200")(
        H3(f"History: {page_url[:80]}{'...' if len(page_url) > 80 else ''}", cls="text-sm font-semibold mb-2"),
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Checked At"), Th("Verdict"), Th("Coverage State"), Th("Last Crawl"), Th("Indexing"), Th("Robots"))),
                Tbody(*[
                    Tr(
                        Td(h.checked_at.strftime("%Y-%m-%d %H:%M") if h.checked_at else "—"),
                        Td(Span(h.verdict, cls="badge badge-sm " + ("badge-success" if h.verdict == "PASS" else "badge-error"))),
                        Td(h.coverage_state or "—"),
                        Td(h.last_crawl_time[:10] if h.last_crawl_time else "—"),
                        Td(h.indexing_state or "—"),
                        Td(h.robots_txt_state or "—"),
                    ) for h in history
                ]),
                cls="-zebra text-xs",
            ),
        ),
    )


@rt
def index_status(id: int):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)

        all_pages = get_index_status(session, site_url)
        indexed = [p for p in all_pages if p.verdict == "PASS"]
        not_indexed = [p for p in all_pages if p.verdict != "PASS"]
        grouped = get_not_indexed_by_reason(session, site_url)

    return Title(f"Index Status - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        H1("Index Status", cls="text-2xl font-bold mb-1"),
        P(f"{website.url}", cls="text-base-content/60 mb-4"),
        Div(cls="mb-6 p-4 border border-base-300 rounded-lg")(
            H2("Check Indexing", cls="text-lg font-semibold mb-2"),
            P("Fetch all pages from a sitemap and re-check their indexing status via GSC.", cls="text-sm text-base-content/60 mb-3"),
            Form(
                Input(name="id", value=str(id), type="hidden"),
                Div(cls="flex gap-2")(
                    Input(name="sitemap_url", placeholder="https://example.com/sitemap.xml", required=True,
                          cls="input input-bordered flex-1"),
                    Btn("Start Check", cls="-primary", type="submit"),
                ),
                hx_post=index_check_start.to(),
                hx_target="#index-check-area",
                hx_swap="innerHTML",
            ),
            Div(id="index-check-area", cls="mt-2"),
        ),
        render_index_stats(len(all_pages), len(indexed), len(not_indexed)),
        Div(cls="mb-6 p-4 border border-base-300 rounded-lg")(
            H2("Page History", cls="text-lg font-semibold mb-2"),
            P("Look up full index check history for a specific page URL.", cls="text-sm text-base-content/60 mb-3"),
            Form(
                Input(name="id", value=str(id), type="hidden"),
                Div(cls="flex gap-2")(
                    Input(name="page_url", placeholder="https://example.com/page", required=True,
                          cls="input input-bordered flex-1"),
                    Btn("View History", cls="-primary", type="submit"),
                ),
                hx_post=index_page_history.to(),
                hx_target="#page-history-area",
                hx_swap="innerHTML",
            ),
            Div(id="page-history-area", cls="mt-2"),
        ),
        Div(cls="space-y-4")(
            *[render_index_reason_group(id, reason, pages) for reason, pages in grouped.items()]
        ) if grouped else (P("All pages are indexed.", cls="text-sm text-success") if all_pages else P("No index tracking data yet.", cls="text-sm text-base-content/60")),
    ),


COUNTRY_SORT_OPTIONS = [
    ("clicks", "Most Clicks"),
    ("impressions", "Most Impressions"),
    ("ctr", "Best CTR"),
    ("position", "Best Position"),
]


def _country_name(code):
    try:
        return pycountry.countries.get(alpha_3=code.upper()).name
    except AttributeError:
        return code.upper()


def _is_underperforming(r, site_avg_ctr):
    return (r["impressions"] > 1000 and r["avg_ctr"] and site_avg_ctr and
            r["avg_ctr"] < site_avg_ctr * 0.5)


def _render_ctr_vs_site(ctr, site_avg_ctr, impressions):
    if not ctr or not site_avg_ctr:
        return Span("—", cls="text-base-content/40")
    ratio = ctr / site_avg_ctr
    if ratio < 0.5 and impressions > 1000:
        return Span(f"{ratio:.1f}x", cls="text-error font-semibold")
    elif ratio > 1.5:
        return Span(f"{ratio:.1f}x", cls="text-success font-semibold")
    return Span(f"{ratio:.1f}x", cls="text-base-content/60")


def render_country_stats(rows, site_avg_ctr):
    total_clicks = sum(r["clicks"] for r in rows)
    total_impressions = sum(r["impressions"] for r in rows)
    underperforming = sum(1 for r in rows if _is_underperforming(r, site_avg_ctr))
    return Stats(
        Stat(StatTitle("Countries"), StatValue(str(len(rows)))),
        Stat(StatTitle("Total Clicks"), StatValue(f"{total_clicks:,}")),
        Stat(StatTitle("Total Impressions"), StatValue(f"{total_impressions:,}")),
        Stat(StatTitle("Underperforming"), StatValue(str(underperforming), cls="text-error")),
        cls="shadow w-full mb-6",
    )


def render_country_filters(id, current_sort, current_days, current_page_url):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Sort:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(label, href=countries.to(id=id, sort=key, days=current_days, page_url=current_page_url),
                cls="join-item btn btn-xs" + (" btn-primary" if key == current_sort else ""))
              for key, label in COUNTRY_SORT_OPTIONS]
        ),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=countries.to(id=id, sort=current_sort, days=d, page_url=current_page_url),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Page:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="sort", value=current_sort, type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Input(name="page_url", value=current_page_url,
                  placeholder="https://...", cls="input input-bordered input-xs w-44"),
            action=countries.to(), method="get",
        ),
    )


def render_country_table(rows, site_avg_ctr):
    if not rows:
        return P("No country data for this period.", cls="text-sm text-base-content/60")
    total_clicks = sum(r["clicks"] for r in rows)
    return Div(cls="overflow-x-auto")(
        Table(
            Thead(Tr(Th("#"), Th("Country"), Th("Clicks"), Th("Share"), Th("Impr"), Th("Pos"), Th("CTR"), Th("vs Site Avg"))),
            Tbody(*[
                Tr(
                    Td(str(i + 1)),
                    Td(_country_name(r["country"])),
                    Td(f"{r['clicks']:,}"),
                    Td(f"{(r['clicks'] / total_clicks * 100):.1f}%" if total_clicks else "0%"),
                    Td(f"{r['impressions']:,}"),
                    Td(f"{r['avg_position']:.1f}" if r["avg_position"] else "—"),
                    Td(f"{r['avg_ctr'] * 100:.1f}%" if r["avg_ctr"] else "—"),
                    Td(_render_ctr_vs_site(r["avg_ctr"], site_avg_ctr, r["impressions"])),
                    cls="hover:bg-base-200" + (" bg-warning/10" if _is_underperforming(r, site_avg_ctr) else ""),
                ) for i, r in enumerate(rows)
            ]),
            cls="-zebra",
        ),
    )


@rt
def countries(id: int, sort: str = "clicks", days: int = 30, page_url: str = "", export: str = "", custom_start: str = "", custom_end: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        start, end = resolve_dates(days, custom_start, custom_end)
        rows = get_country_breakdown(session, site_url, start, end,
                                     page_url=page_url or None, limit=50)

        key = {"clicks": "clicks", "impressions": "impressions",
               "ctr": "avg_ctr", "position": "avg_position"}.get(sort, "clicks")
        reverse = sort != "position"
        rows.sort(key=lambda r: r[key] or 0, reverse=reverse)

        total_clicks = sum(r["clicks"] for r in rows)
        total_impressions = sum(r["impressions"] for r in rows)
        site_avg_ctr = (total_clicks / total_impressions) if total_impressions else None

        if export:
            h = ["Country", "Clicks", "Share", "Impressions", "Position", "CTR"]
            d = []
            for r in rows:
                try:
                    cname = pycountry.countries.get(alpha_3=r["country"].upper()).name
                except:
                    cname = r["country"].upper()
                share = f"{r['clicks']/total_clicks*100:.1f}%" if total_clicks else "0%"
                ctr = f"{r['avg_ctr']*100:.1f}%" if r.get("avg_ctr") is not None else ""
                d.append([cname, r["clicks"], share, r["impressions"],
                          round(r["avg_position"], 1), ctr])
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "countries", export)

    return Title(f"Country Breakdown - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("Country Breakdown", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=countries.to(id=id, sort=sort, days=days, page_url=page_url, export="csv"))),
                    Li(A("Markdown", href=countries.to(id=id, sort=sort, days=days, page_url=page_url, export="md"))),
                ),
            ),
        ),
        P(f"{website.url} — {dict(DAYS_OPTIONS).get(days, f'{days} days')}", cls="text-base-content/60 mb-4"),
        render_country_filters(id, sort, days, page_url),
        render_country_stats(rows, site_avg_ctr),
        render_country_table(rows, site_avg_ctr),
    ),


def _enrich_page_metrics(session, site_url, query, pages, start, end):
    if not pages:
        return {}
    result = session.exec(
        select(
            GSCAnalytics.page,
            func.sum(GSCAnalytics.clicks).label("clicks"),
            func.sum(GSCAnalytics.impressions).label("impressions"),
            func.avg(GSCAnalytics.position).label("avg_position"),
        ).where(
            GSCAnalytics.site_url == site_url,
            GSCAnalytics.query == query,
            GSCAnalytics.date.between(start, end),
            GSCAnalytics.page.in_(pages),
        ).group_by(GSCAnalytics.page)
    ).all()
    return {r.page: {"clicks": r.clicks or 0, "impressions": r.impressions or 0,
                     "avg_position": round(r.avg_position, 1) if r.avg_position else None}
            for r in result}


def render_cannibal_group(group, metrics_map, source_label):
    kw = group.get("keyword") or group.get("query", "")
    pages = group.get("pages", [])
    return Div(cls="mb-4 p-4 border border-base-300 rounded-lg")(
        Div(cls="flex items-center justify-between mb-2")(
            H3(kw, cls="font-semibold text-base"),
            Span(f"{len(pages)} pages · {source_label}", cls="text-xs text-base-content/40"),
        ),
        Div(cls="overflow-x-auto")(
            Table(
                Thead(Tr(Th("Page"), Th("Clicks"), Th("Impr"), Th("Pos"))),
                Tbody(*[
                    Tr(
                        Td(A(p[:70] + ("..." if len(p) > 70 else ""),
                             href=p, target="_blank", cls="link link-primary text-sm")),
                        Td(f"{metrics_map.get(p, {}).get('clicks', 0):,}"),
                        Td(f"{metrics_map.get(p, {}).get('impressions', 0):,}"),
                        Td(str(metrics_map.get(p, {}).get("avg_position") or "—")),
                    ) for p in pages
                ]),
                cls="-zebra text-sm",
            ),
        ),
    )


@rt
def canb(id: int, days: int = 30, export: str = "", custom_start: str = "", custom_end: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        start, end = resolve_dates(days, custom_start, custom_end)
        cannib = find_cannibalized(session, id, site_url, start, end)
        exact = cannib.get("exact_matches", [])
        gsc = cannib.get("gsc_matches", [])

        for group in exact:
            group["metrics"] = _enrich_page_metrics(session, site_url, group["keyword"],
                                                    group["pages"], start, end)
        for group in gsc:
            group["metrics"] = _enrich_page_metrics(session, site_url, group["query"],
                                                    group["pages"], start, end)

        if export:
            h = ["Source", "Keyword", "Page", "Clicks", "Impressions", "Position"]
            d = []
            for g in exact:
                kw = g.get("keyword", "")
                for p in g.get("pages", []):
                    m = g["metrics"].get(p, {})
                    d.append(["exact", kw, p, m.get("clicks", 0), m.get("impressions", 0),
                              m.get("avg_position", "—")])
            for g in gsc:
                kw = g.get("query", "")
                for p in g.get("pages", []):
                    m = g["metrics"].get(p, {})
                    d.append(["gsc", kw, p, m.get("clicks", 0), m.get("impressions", 0),
                              m.get("avg_position", "—")])
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "cannibalization", export)

    total = len(exact) + len(gsc)
    return Title(f"Cannibalization - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("Cannibalization", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=canb.to(id=id, days=days, export="csv"))),
                    Li(A("Markdown", href=canb.to(id=id, days=days, export="md"))),
                ),
            ),
        ),
        P(f"{website.url} — {dict(DAYS_OPTIONS).get(days, f'{days} days')}", cls="text-base-content/60 mb-4"),
        Stats(
            Stat(StatTitle("Total Groups"), StatValue(str(total))),
            Stat(StatTitle("Exact Keyword Matches"), StatValue(str(len(exact)))),
            Stat(StatTitle("GSC Ranking Overlap"), StatValue(str(len(gsc)))),
            cls="shadow w-full mb-6",
        ) if total else P("No cannibalization detected.", cls="text-sm text-success"),
        Div(cls="space-y-4")(
            *[render_cannibal_group(g, g["metrics"], "focus keyword")
              for g in exact],
            *[render_cannibal_group(g, g["metrics"], "GSC overlap")
              for g in gsc],
        ) if total else "",
    ),


FAQ_SORT_OPTIONS = [
    ("impressions", "Most Impressions"),
    ("clicks", "Most Clicks"),
    ("position", "Best Position"),
]


def render_faq_filters(id, current_sort, current_days, current_page_url):
    return Div(cls="flex flex-wrap items-center gap-2 mb-4")(
        Span("Sort:", cls="text-sm font-medium"),
        Div(cls="join")(
            *[A(label, href=faq.to(id=id, sort=key, days=current_days, page_url=current_page_url),
                cls="join-item btn btn-xs" + (" btn-primary" if key == current_sort else ""))
              for key, label in FAQ_SORT_OPTIONS]
        ),
        Span("Period:", cls="text-sm font-medium ml-2"),
        Div(cls="join")(
            *[A(label, href=faq.to(id=id, sort=current_sort, days=d, page_url=current_page_url),
                cls="join-item btn btn-xs" + (" btn-primary" if d == current_days else ""))
              for d, label in DAYS_OPTIONS]
        ),
        Span("Page:", cls="text-sm font-medium ml-2"),
        Form(
            Input(name="id", value=str(id), type="hidden"),
            Input(name="sort", value=current_sort, type="hidden"),
            Input(name="days", value=str(current_days), type="hidden"),
            Input(name="page_url", value=current_page_url,
                  placeholder="https://...", cls="input input-bordered input-xs w-44"),
            render_custom_date_inputs(days, custom_start, custom_end),
            action=faq.to(), method="get"
        ),
    )


def render_faq_table(rows):
    if not rows:
        return P("No FAQ-type queries found for this period.", cls="text-sm text-base-content/60")
    return Div(cls="overflow-x-auto")(
        Table(
            Thead(Tr(Th("#"), Th("Question"), Th("Impr"), Th("Clicks"), Th("Pos"))),
            Tbody(*[
                Tr(
                    Td(str(i + 1)),
                    Td(r["query"], cls="font-medium"),
                    Td(f"{r['total_impressions']:,}"),
                    Td(f"{r['total_clicks']:,}"),
                    Td(f"{r['avg_position']:.1f}"),
                ) for i, r in enumerate(rows)
            ]),
            cls="-zebra",
        ),
    )


@rt
def faq(id: int, sort: str = "impressions", days: int = 30, page_url: str = "", export: str = "", custom_start: str = "", custom_end: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        start, end = resolve_dates(days, custom_start, custom_end)
        rows = get_top_queries(session, site_url, start, end,
                               page_path=page_url or None, limit=200,
                               sort_by="impressions")
        faqs = extract_faq_queries(rows)
        faq_set = set(faqs)
        faq_rows = [r for r in rows if r["query"] in faq_set]

        key = {"impressions": "total_impressions", "clicks": "total_clicks",
               "position": "avg_position"}.get(sort, "total_impressions")
        reverse = sort != "position"
        faq_rows.sort(key=lambda r: r[key] or 0, reverse=reverse)

        if export:
            h = ["Question", "Impressions", "Clicks", "Position"]
            d = [[r["query"], r["total_impressions"], r["total_clicks"],
                   round(r["avg_position"], 1)] for r in faq_rows]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "faq", export)

    total_imp = sum(r["total_impressions"] for r in faq_rows)
    total_clicks = sum(r["total_clicks"] for r in faq_rows)
    days_label = dict(DAYS_OPTIONS).get(days, f"{days} days")
    page_label = f" — {page_url}" if page_url else ""
    return Title(f"FAQ Opportunities - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        Div(cls="flex items-center gap-2 mb-4")(
            H1("FAQ Schema Opportunities", cls="text-2xl font-bold mb-1"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=faq.to(id=id, sort=sort, days=days, page_url=page_url, export="csv"))),
                    Li(A("Markdown", href=faq.to(id=id, sort=sort, days=days, page_url=page_url, export="md"))),
                ),
            ),
        ),
        P(f"Questions people search for that could become FAQ schema on your pages — {days_label}{page_label}",
          cls="text-base-content/60 mb-4"),
        Stats(
            Stat(StatTitle("FAQ Queries"), StatValue(str(len(faq_rows)))),
            Stat(StatTitle("Total Impressions"), StatValue(f"{total_imp:,}")),
            Stat(StatTitle("Total Clicks"), StatValue(f"{total_clicks:,}")),
            cls="shadow w-full mb-6",
        ),
        render_faq_filters(id, sort, days, page_url),
        render_faq_table(faq_rows),
    ),


def _render_mover_row(m):
    change = m.get("change", 0)
    is_improvement = change < 0
    icon = "▲" if is_improvement else "▼"
    color = "text-success" if is_improvement else "text-error"
    return Div(cls="flex items-center justify-between text-sm")(
        Span(m["query"], cls="truncate max-w-[200px]"),
        Div(cls="flex items-center gap-2")(
            Span(f"Pos {m.get('last', 0):.1f}", cls="text-base-content/70"),
            Span(f"{icon} {abs(change):.1f}", cls=f"{color} font-medium"),
        )
    )


def _compute_dominance_index(keywords_data):
    if not keywords_data:
        return 0, "No data"
    positions = [d.get("avg_position") for d in keywords_data if d.get("avg_position")]
    if not positions:
        return 0, "No position data"
    avg_pos = sum(positions) / len(positions)
    score = max(0, min(100, round(100 - (avg_pos - 1) * 100 / 99)))
    label = "Excellent" if score >= 80 else "Good" if score >= 60 else "Fair" if score >= 40 else "Needs Work"
    return score, label


def _render_position_history(history, start_date=None, end_date=None, max_bars=40):
    if not history:
        return P("No position data yet.", cls="text-xs text-base-content/40")
    
    from datetime import datetime, timedelta

    if not start_date:
        start_date = history[0]["date"]
    if not end_date:
        end_date = history[-1]["date"]
        
    start_dt = datetime.strptime(start_date, "%Y-%m-%d")
    end_dt = datetime.strptime(end_date, "%Y-%m-%d")
    total_days = (end_dt - start_dt).days + 1
    if total_days <= 0:
        total_days = 1
        
    num_bars = min(max_bars, total_days)
    buckets = [[] for _ in range(num_bars)]
    
    for h in history:
        h_dt = datetime.strptime(h["date"], "%Y-%m-%d")
        if h_dt < start_dt or h_dt > end_dt:
            continue
        days_from_start = (h_dt - start_dt).days
        bucket_idx = int((days_from_start / total_days) * num_bars)
        bucket_idx = min(bucket_idx, num_bars - 1)
        buckets[bucket_idx].append(h)
        
    recent = []
    for i, bucket in enumerate(buckets):
        valid_pos = [h["avg_position"] for h in bucket if h.get("avg_position") is not None]
        avg_pos = sum(valid_pos) / len(valid_pos) if valid_pos else None
        bucket_days = total_days / num_bars
        mid_days = (i + 0.5) * bucket_days
        mid_date = (start_dt + timedelta(days=mid_days)).strftime("%Y-%m-%d")
        recent.append({"avg_position": avg_pos, "date": mid_date})

    positions = [h.get("avg_position") for h in recent if h.get("avg_position")]
    min_pos = min(positions) if positions else 1
    max_pos = max(positions) if positions else 20
    pos_range = max(max_pos - min_pos, 5)
    
    return Div(cls="w-max")(
        Div(cls="flex gap-0.5 items-end h-7", style="position: relative;")(
            *[_render_position_bar(h.get("avg_position"), h.get("date", ""), min_pos, pos_range)
              for h in recent]
        ),
        Div(cls="flex justify-between text-[10px] text-base-content/30 mt-0.5")(
            Span(start_date[5:]),
            Span(f"pos {min_pos:.0f}–{max_pos:.0f}"),
            Span(end_date[5:]),
        ),
    )


def _render_position_bar(pos, date, min_pos=1, pos_range=10):
    if not pos:
        return Div(cls="w-3 h-1 bg-base-300 rounded", data_tip="no data")
    rel = (pos - min_pos) / pos_range if pos_range else 0
    height = max(3, min(28, int(28 - rel * 24)))
    color = "bg-success" if pos <= 3 else "bg-warning" if pos <= 10 else "bg-error"
    return Div(cls=f"tooltip w-3 {color} rounded-sm", style=f"height: {height}px",
               data_tip=f"{date}: #{pos:.1f}")


@rt
def serpwatcher_add_keyword(id: int, keyword: str):
    keyword = keyword.strip()
    with get_session() as session:
        existing = get_tracked_keywords(session, id)
        if any(k.keyword.lower() == keyword.lower() for k in existing):
            return Div(
                Span(f"'{keyword}' is already tracked", cls="text-sm text-warning"),
                hx_get=serpwatcher_keywords_list.to(id=id),
                hx_target="#serpwatcher-keywords",
                hx_swap="innerHTML",
                hx_trigger="load",
            )
        add_tracked_keyword(session, id, keyword)
    return Div(
        Span(f"Added '{keyword}'", cls="text-sm text-success"),
        hx_get=serpwatcher_keywords_list.to(id=id),
        hx_target="#serpwatcher-keywords",
        hx_swap="innerHTML",
        hx_trigger="load",
    )


@rt
def serpwatcher_delete_keyword(id: int, keyword_id: int):
    with get_session() as session:
        delete_tracked_keyword(session, keyword_id)
    return Div(
        hx_get=serpwatcher_keywords_list.to(id=id),
        hx_target="#serpwatcher-keywords",
        hx_swap="innerHTML",
        hx_trigger="load",
    )


@rt
def serpwatcher_keywords_list(id: int, days: int = 30, custom_start: str = "", custom_end: str = "", sort: str = "date"):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        keywords = get_tracked_keywords(session, id)
        if not keywords:
            return P("No keywords being tracked. Add one above.", cls="text-sm text-base-content/60")

        start, end = resolve_dates(days, custom_start, custom_end)
        kw_set = {k.keyword.lower() for k in keywords}
        trends = get_trends(session, site_url, start, end, dimension="query")
        from collections import defaultdict
        by_kw = defaultdict(list)
        for r in trends:
            if r["query"] and r["query"].lower() in kw_set:
                by_kw[r["query"].lower()].append({"date": r["date"], "avg_position": round(r["avg_position"], 1) if r["avg_position"] else None,
                                           "clicks": r["clicks"] or 0, "impressions": r["impressions"] or 0})

    kw_data = []
    for kw in keywords:
        hist = by_kw.get(kw.keyword.lower(), [])
        hist.sort(key=lambda h: h["date"])
        latest = hist[-1] if hist else {}
        first = hist[0] if hist else {}
        cur_pos = latest.get("avg_position")
        avg_pos = round(sum(h["avg_position"] for h in hist if h["avg_position"]) / len([h for h in hist if h["avg_position"]]), 1) if any(h["avg_position"] for h in hist) else None
        total_clicks = sum(h["clicks"] for h in hist)
        total_impressions = sum(h["impressions"] for h in hist)

        diff = 0
        if cur_pos and first.get("avg_position"):
            diff = cur_pos - first["avg_position"]

        kw_data.append({
            "kw": kw,
            "hist": hist,
            "cur_pos": cur_pos or 999,
            "avg_pos": avg_pos,
            "total_clicks": total_clicks,
            "total_impressions": total_impressions,
            "diff": diff,
            "first": first,
            "latest": latest
        })

    if sort == "pos_asc":
        kw_data.sort(key=lambda x: x["cur_pos"])
    elif sort == "pos_desc":
        kw_data.sort(key=lambda x: (x["cur_pos"] != 999, x["cur_pos"]), reverse=True)
    elif sort == "gainers":
        kw_data.sort(key=lambda x: x["diff"])
    elif sort == "losers":
        kw_data.sort(key=lambda x: x["diff"], reverse=True)
    elif sort == "clicks":
        kw_data.sort(key=lambda x: x["total_clicks"], reverse=True)
    elif sort == "impressions":
        kw_data.sort(key=lambda x: x["total_impressions"], reverse=True)
    elif sort == "alpha":
        kw_data.sort(key=lambda x: x["kw"].keyword.lower())
    else:
        kw_data.sort(key=lambda x: x["kw"].id, reverse=True)

    sort_options = [
        ("date", "Date Added (Newest)"),
        ("pos_asc", "Highest Position"),
        ("pos_desc", "Lowest Position"),
        ("gainers", "Top Gainers"),
        ("losers", "Top Losers"),
        ("clicks", "Most Clicks"),
        ("impressions", "Most Impressions"),
        ("alpha", "Alphabetical"),
    ]

    header = Div(cls="flex items-center justify-between mb-4")(
        H3(f"{len(keywords)} Tracked Keywords", cls="font-semibold text-lg text-base-content/80"),
        Select(
            *[Option(label, value=val, selected=(sort == val)) for val, label in sort_options],
            cls="select select-bordered select-sm w-48",
            name="sort",
            hx_get=serpwatcher_keywords_list.to(id=id, days=days, custom_start=custom_start, custom_end=custom_end),
            hx_target="#serpwatcher-keywords",
            hx_swap="innerHTML"
        )
    )

    cards = []
    for d in kw_data:
        kw = d["kw"]
        hist = d["hist"]
        cur_pos = d["cur_pos"] if d["cur_pos"] != 999 else None
        avg_pos = d["avg_pos"]
        total_clicks = d["total_clicks"]
        total_impressions = d["total_impressions"]
        diff = d["diff"]
        first = d["first"]

        trend_icon = Span("—", cls="text-base-content/30 text-xs")
        if cur_pos and first.get("avg_position") and cur_pos != first["avg_position"]:
            cls_color = "text-success" if diff < 0 else "text-error"
            icon = "▲" if diff < 0 else "▼"
            trend_icon = Span(f"{icon} {abs(diff):.1f}", cls=f"{cls_color} text-xs font-semibold")

        cards.append(Div(cls="card card-bordered bg-base-100 mb-3")(
            Div(cls="card-body p-4")(
                Div(cls="flex items-center justify-between")(
                    Div(cls="flex items-center gap-3")(
                        H3(kw.keyword, cls="card-title text-base font-semibold"),
                        Span(f"now #{cur_pos:.1f}" if cur_pos else "no rank", cls="badge badge-sm " + ("badge-success" if cur_pos and cur_pos <= 3 else "badge-warning" if cur_pos and cur_pos <= 10 else "badge-error" if cur_pos else "badge-ghost")),
                        trend_icon,
                    ),
                    Btn("✕", cls="btn btn-ghost btn-xs text-error",
                        hx_post=serpwatcher_delete_keyword.to(id=id, keyword_id=kw.id),
                        hx_target="#serpwatcher-keywords",
                        hx_swap="innerHTML",
                    ),
                ),
                Div(cls="flex gap-4 text-xs text-base-content/60 mt-1")(
                    Span(f"avg {avg_pos}" if avg_pos else ""),
                    Span(f"{total_clicks:,} clicks"),
                    Span(f"{total_impressions:,} impressions"),
                    Span(f"{days}d range"),
                ),
                Div(cls="mt-2"),
                _render_position_history(hist, start, end),
            ),
        ))

    return Div(header, Div(*cards))


@rt
def serpwatcher(id: int, days: int = 30, custom_start: str = "", custom_end: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        keywords = get_tracked_keywords(session, id)

        all_data = []
        if keywords:
            start, end = resolve_dates(days, custom_start, custom_end)
            kw_set = {k.keyword.lower() for k in keywords}
            trends = get_trends(session, site_url, start, end, dimension="query")
            query_positions = {}
            for r in trends:
                if r["query"] and r["query"].lower() in kw_set and r["avg_position"]:
                    if r["query"].lower() not in query_positions:
                        query_positions[r["query"].lower()] = []
                    query_positions[r["query"].lower()].append(r["avg_position"])
            for q, positions in query_positions.items():
                all_data.append({"query": q, "avg_position": round(sum(positions) / len(positions), 1)})

    dominance_score, dominance_label = _compute_dominance_index(all_data)
    days_label = dict(DAYS_OPTIONS).get(days, f"{days} days")

    top3 = sum(1 for d in all_data if d["avg_position"] and d["avg_position"] <= 3)
    top10 = sum(1 for d in all_data if d["avg_position"] and d["avg_position"] <= 10)
    losing = sum(1 for d in all_data if d["avg_position"] and d["avg_position"] > 20)

    gainers = []
    losers = []
    if keywords:
        start, end = resolve_dates(days, custom_start, custom_end)
        kw_set = {k.keyword.lower() for k in keywords}
        trends2 = get_trends(session, site_url, start, end, dimension="query")
        kw_trend = {}
        for r in trends2:
            if r["query"] and r["query"].lower() in kw_set and r["avg_position"]:
                kw_trend.setdefault(r["query"], []).append(r["avg_position"])
        movers = []
        for q, pos_list in kw_trend.items():
            if len(pos_list) >= 2:
                movers.append({"query": q, "first": pos_list[0], "last": pos_list[-1],
                               "change": pos_list[-1] - pos_list[0]})
        gainers = sorted([m for m in movers if m["change"] < 0], key=lambda x: x["change"])
        losers = sorted([m for m in movers if m["change"] > 0], key=lambda x: x["change"], reverse=True)

    return Title(f"SERPWatcher - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4"),
        H1("SERPWatcher", cls="text-2xl font-bold mb-1"),
        P(f"Track keyword positions over time — {days_label}", cls="text-base-content/60 mb-4"),
        Div(cls="grid grid-cols-1 md:grid-cols-4 gap-4 mb-6")(
            Div(cls="stat shadow rounded-lg p-4")(
                Div("Tracked Keywords", cls="stat-title"),
                Div(str(len(keywords)), cls="stat-value"),
            ),
            Div(cls="stat shadow rounded-lg p-4")(
                Div("Dominance Index", cls="stat-title"),
                Div(str(dominance_score), cls="stat-value"),
                Div(dominance_label, cls="stat-desc"),
            ),
            Div(cls="stat shadow rounded-lg p-4")(
                Div("Rank Distribution", cls="stat-title"),
                Div(cls="flex gap-3 text-sm mt-1")(
                    Span(f"🥇 {top3} top 3"),
                    Span(f"📋 {top10 - top3} top 10"),
                    Span(f"⚠️ {losing} >20"),
                ),
            ),
            Div(cls="stat shadow rounded-lg p-4")(
                Div("Period", cls="stat-title"),
                Form(
                    Input(type="hidden", name="id", value=str(id)),
                    Select(
                        *[Option(label, value=str(val), selected=(days == val)) for val, label in DAYS_OPTIONS],
                        cls="select select-bordered select-sm w-full mt-2",
                        name="days",
                        onchange="this.form.submit()"
                    ),
                    render_custom_date_inputs(days, custom_start, custom_end),
                    action="/serpwatcher", method="get", cls="flex flex-col gap-2 mt-2"
                ),
            ),
        ),
        Div(cls="grid grid-cols-1 md:grid-cols-3 gap-4 mb-6")(
            Div(cls="p-4 border border-base-300 rounded-lg")(
                H2("Add Keyword", cls="text-lg font-semibold mb-1"),
                P("Track any keyword to monitor its position trend in GSC.", cls="text-xs text-base-content/50 mb-2"),
                Form(
                    Input(name="id", value=str(id), type="hidden"),
                    Div(cls="flex gap-2")(
                        Input(name="keyword", placeholder="e.g. seo tools", required=True,
                              cls="input input-bordered flex-1"),
                        Btn("Track", cls="-primary", type="submit"),
                    ),
                    hx_post=serpwatcher_add_keyword.to(),
                    hx_target="#serpwatcher-add-result",
                    hx_swap="innerHTML",
                ),
                Div(id="serpwatcher-add-result"),
            ),
            Div(cls="p-4 border border-base-300 rounded-lg md:col-span-2")(
                H2("Top Movers", cls="text-lg font-semibold mb-1"),
                P("Biggest position changes in this period.", cls="text-xs text-base-content/50 mb-4"),
                Div(cls="grid grid-cols-1 sm:grid-cols-2 gap-4")(
                    Div(
                        H3("Gainers", cls="text-sm font-medium text-success mb-2"),
                        Div(cls="space-y-1 max-h-64 overflow-y-auto")(
                            *[_render_mover_row(m) for m in gainers[:5]] if gainers else [P("No gainers yet.", cls="text-xs text-base-content/40")],
                            Div(cls="space-y-1 mt-1")(
                                *[Div(cls="hidden")(_render_mover_row(m)) for m in gainers[5:]]
                            ) if len(gainers) > 5 else "",
                            Div(cls="text-xs text-primary cursor-pointer mt-2 hover:underline",
                                onclick="let h=this.previousElementSibling.querySelectorAll('.hidden'); if(this.innerText.includes('more')){ for(let i=0;i<50&&i<h.length;i++) h[i].classList.remove('hidden'); if(h.length<=50) this.innerText='▲ See less'; } else { let all=this.previousElementSibling.children; for(let i=0;i<all.length;i++) all[i].classList.add('hidden'); this.innerText='▼ See more'; }")(
                                "▼ See more"
                            ) if len(gainers) > 5 else ""
                        )
                    ),
                    Div(
                        H3("Losers", cls="text-sm font-medium text-error mb-2"),
                        Div(cls="space-y-1 max-h-64 overflow-y-auto")(
                            *[_render_mover_row(m) for m in losers[:5]] if losers else [P("No losers yet.", cls="text-xs text-base-content/40")],
                            Div(cls="space-y-1 mt-1")(
                                *[Div(cls="hidden")(_render_mover_row(m)) for m in losers[5:]]
                            ) if len(losers) > 5 else "",
                            Div(cls="text-xs text-primary cursor-pointer mt-2 hover:underline",
                                onclick="let h=this.previousElementSibling.querySelectorAll('.hidden'); if(this.innerText.includes('more')){ for(let i=0;i<50&&i<h.length;i++) h[i].classList.remove('hidden'); if(h.length<=50) this.innerText='▲ See less'; } else { let all=this.previousElementSibling.children; for(let i=0;i<all.length;i++) all[i].classList.add('hidden'); this.innerText='▼ See more'; }")(
                                "▼ See more"
                            ) if len(losers) > 5 else ""
                        )
                    )
                ) if (gainers or losers) else P("Track keywords to see movers.", cls="text-xs text-base-content/40")
            ),
        ),
        Div(id="serpwatcher-keywords")(
            Div(
                Span("Loading...", cls="text-sm text-base-content/60"),
                hx_get=serpwatcher_keywords_list.to(id=id, days=days, custom_start=custom_start, custom_end=custom_end),
                hx_trigger="load",
                hx_swap="innerHTML",
            ),
        ),
    ),


def _schema_status_badge(s):
    if s.get("error"):
        return Span("Fetch Failed", cls="badge badge-error badge-sm")
    if not s["summary"]["total_schemas"]:
        return Span("No Schema", cls="badge badge-ghost badge-sm")
    valid = s["summary"]["valid_count"]
    total = s["summary"]["total_schemas"]
    cls = "badge-success" if valid == total else "badge-warning" if valid > 0 else "badge-error"
    return Span(f"{valid}/{total} valid", cls=f"badge {cls} badge-sm")


@rt
def schema_check(id: int, tab: str = "pages"):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Website not found", P("Website not found"))
    tab_pages = "tab tab-active" if tab == "pages" else "tab"
    tab_url = "tab tab-active" if tab == "url" else "tab"
    return Title(f"Schema Check - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4 block"),
        H1("Schema Checker", cls="text-2xl font-bold mb-1"),
        P("Validate structured data against Google Rich Results spec.", cls="text-base-content/60 mb-4"),
        Div(cls="tabs tabs-bordered mb-6")(
            A("Site Pages", href=schema_check.to(id=id, tab="pages"), cls=tab_pages),
            A("Live URL", href=schema_check.to(id=id, tab="url"), cls=tab_url),
        ),
        Div(id="schema-content")(
            _render_schema_tab(id, tab),
        ),
    ),


def _render_schema_tab(id: int, tab: str):
    if tab == "url":
        return Div(
            Form(
                Input(name="id", value=str(id), type="hidden"),
                Div(cls="flex gap-2")(
                    Input(name="url", placeholder="https://example.com/page", required=True,
                          cls="input input-bordered flex-1"),
                    Btn("Validate", cls="-primary", type="submit"),
                ),
                hx_post=schema_check_url.to(),
                hx_target="#schema-result",
                hx_swap="innerHTML",
                hx_indicator="#schema-spinner",
            ),
            Div("Fetching and validating...",
                id="schema-spinner", cls="htmx-indicator mt-2 text-sm text-base-content/40"),
            Div(id="schema-result", cls="mt-4"),
        )
    with get_session() as session:
        from seootter.models import URLMapping, get_url_mapping
        articles = get_articles_by_website(session, id)
        mapping = get_url_mapping(session, id)
    seen = set()
    pages = []
    for a in articles:
        if a.url and a.url not in seen:
            seen.add(a.url)
            pages.append({"url": a.url, "label": a.url.rsplit("/", 1)[-1] or a.url, "id": f"art-{a.id}"})
    for m in mapping:
        if m.url not in seen:
            seen.add(m.url)
            pages.append({"url": m.url, "label": m.url.rsplit("/", 1)[-1] or m.url, "id": f"map-{m.id}"})
    if not pages:
        return P("No pages found. Run `seo-otter-report` via CLI first to sync URL mapping, or add articles.",
                 cls="text-sm text-base-content/60")
    return Div(
        Div(cls="flex items-center justify-between mb-4")(
            Span(f"{len(pages)} pages", cls="text-sm text-base-content/60"),
            Div(cls="flex gap-2")(
                Btn("Predict All", cls="btn btn-primary btn-sm",
                    hx_post=schema_predict_all.to(id=id),
                    hx_target="#schema-all-results",
                    hx_swap="innerHTML",
                    hx_indicator="#predict-all-spinner"),
                Btn("Validate All", cls="btn btn-outline btn-sm",
                    hx_post=schema_check_validate_all.to(id=id),
                    hx_target="#schema-all-results",
                    hx_swap="innerHTML",
                    hx_indicator="#validate-all-spinner"),
            ),
        ),
        Div(cls="space-y-3")(
            *[Div(cls="card card-compact bg-base-100 border border-base-300")(
                Div(cls="card-body")(
                    Div(cls="flex items-center justify-between gap-2")(
                        A(p["url"], href=p["url"], target="_blank",
                          cls="link link-primary text-sm truncate"),
                        Div(cls="flex gap-1 shrink-0")(
                            Btn("Validate", cls="btn btn-outline btn-xs",
                                hx_post=schema_check_page.to(id=id, page_url=p["url"]),
                                hx_target=f"#schema-result-{p['id']}",
                                hx_swap="innerHTML"),
                            Btn("Predict", cls="btn btn-outline btn-xs",
                                hx_post=schema_predict.to(id=id, page_url=p["url"]),
                                hx_target=f"#schema-result-{p['id']}",
                                hx_swap="innerHTML"),
                        ),
                    ),
                    Div(id=f"schema-result-{p['id']}"),
                ),
            ) for p in pages],
        ),
        Div(id="predict-all-spinner", cls="htmx-indicator text-sm text-base-content/40 inline-flex items-center gap-1 mt-3")(
            Span(cls="loading loading-spinner loading-xs"),
            "Predicting schemas for all pages...",
        ),
        Div(id="validate-all-spinner", cls="htmx-indicator text-sm text-base-content/40 inline-flex items-center gap-1 mt-3")(
            Span(cls="loading loading-spinner loading-xs"),
            "Validating all pages...",
        ),
        Div(id="schema-all-results", cls="mt-4"),
    )


@rt
def schema_check_page(id: int, page_url: str):
    try:
        result = validate_page(page_url)
        return _render_schema_result(result)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Error: {e}"),
        )


@rt
def schema_predict(id: int, page_url: str):
    try:
        from trafilatura import fetch_url, extract
        html = fetch_url(page_url)
        if not html:
            return Div(cls="alert alert-warning text-sm mt-2")(
                Span("Could not fetch page content"),
            )
        content = extract(html, output_format="markdown", favor_recall=True,
                          include_tables=True, include_links=False, include_images=False)
        if not content:
            return Div(cls="alert alert-warning text-sm mt-2")(
                Span("Could not extract content from page"),
            )
        result = predict_schemas(content)
    except Exception as e:
        return Div(cls="alert alert-error text-sm mt-2")(
            Span(f"Prediction failed: {e}"),
        )
    items = result.get("suggestions", [])
    reasoning = result.get("reasoning", "")
    return Div(cls="border-t border-base-200 pt-3 mt-3 space-y-2")(
        Div(cls="flex flex-wrap gap-x-4 gap-y-1")(
            *[Div(cls="flex items-center gap-2")(
                Span(item["type"], cls="text-xs font-medium w-20"),
                Progress(cls="-primary h-2", value=str(item["score"]), max="100"),
                Span(f"{item['score']}%", cls="text-xs text-base-content/60 w-8"),
            ) for item in items],
        ),
        P(reasoning, cls="text-xs text-base-content/50 mt-1") if reasoning else "",
    )


@rt
def schema_check_validate_all(id: int):
    try:
        with get_session() as session:
            from seootter.models import get_url_mapping
            articles = get_articles_by_website(session, id)
            mapping = get_url_mapping(session, id)
        seen = set()
        urls = []
        for a in articles:
            if a.url and a.url not in seen:
                seen.add(a.url)
                urls.append(a.url)
        for m in mapping:
            if m.url not in seen:
                seen.add(m.url)
                urls.append(m.url)
        cards = []
        for url in urls:
            result = validate_page(url)
            cards.append(Div(cls="card card-compact bg-base-100 border border-base-300")(
                Div(cls="card-body")(
                    A(url, href=url, target="_blank", cls="link link-primary text-sm font-semibold mb-2"),
                    _render_schema_result(result),
                ),
            ))
        if not cards:
            return P("No pages found.", cls="text-sm text-base-content/60")
        return Div(cls="space-y-3")(*cards)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Error: {e}"),
        )


@rt
def schema_predict_all(id: int):
    try:
        with get_session() as session:
            from seootter.models import get_url_mapping
            articles = get_articles_by_website(session, id)
            mapping = get_url_mapping(session, id)
        seen = set()
        urls = []
        for a in articles:
            if a.url and a.url not in seen:
                seen.add(a.url)
                urls.append(a.url)
        for m in mapping:
            if m.url not in seen:
                seen.add(m.url)
                urls.append(m.url)
        cards = []
        for url in urls:
            try:
                from trafilatura import fetch_url, extract
                html = fetch_url(url)
                content = extract(html, output_format="markdown", favor_recall=True,
                                  include_tables=True, include_links=False, include_images=False) if html else ""
                if not content:
                    pred = {"suggestions": [], "reasoning": "Could not fetch or extract content"}
                else:
                    pred = predict_schemas(content)
            except Exception as e:
                pred = {"suggestions": [], "reasoning": str(e)}
            suggestions = pred.get("suggestions", [])
            cards.append(Div(cls="card card-compact bg-base-100 border border-base-300")(
                Div(cls="card-body")(
                    A(url, href=url, target="_blank", cls="link link-primary text-sm font-semibold mb-2"),
                    Div(cls="space-y-1")(
                        Div(cls="flex flex-wrap gap-x-4 gap-y-1")(
                            *[Div(cls="flex items-center gap-2")(
                                Span(item["type"], cls="text-xs font-medium w-20"),
                                Progress(cls="-primary h-2", value=str(item["score"]), max="100"),
                                Span(f"{item['score']}%", cls="text-xs text-base-content/60 w-8"),
                            ) for item in suggestions],
                        ),
                        P(pred.get("reasoning", ""), cls="text-xs text-base-content/50 mt-1") if pred.get("reasoning") else "",
                    ) if suggestions else P(pred.get("reasoning", ""), cls="text-xs text-warning"),
                ),
            ))
        if not cards:
            return P("No pages found.", cls="text-sm text-base-content/60")
        return Div(cls="space-y-3")(*cards)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Error: {e}"),
        )


@rt
def schema_check_url(id: int, url: str):
    try:
        result = validate_page(url)
        return _render_schema_result(result)
    except Exception as e:
        return Div(cls="alert alert-error text-sm")(
            Span(f"Error: {e}"),
        )


def _render_schema_result(result: dict):
    if result.get("error"):
        return Div(cls="alert alert-error text-sm mt-2")(
            Span(f"Failed to fetch URL (HTTP {result['fetch_status']}): {result['error']}"),
        )
    summary = result.get("summary", {})
    total = summary.get("total_schemas", 0)
    valid = summary.get("valid_count", 0)
    return Div(cls="border-t border-base-200 pt-3 mt-3 space-y-2")(
        Div(cls="flex flex-wrap items-center gap-x-4 gap-y-1 text-sm")(
            _schema_status_badge(result),
            Span(f"{total} schema(s) found", cls="font-medium"),
            Span(f"Types: {', '.join(summary.get('types_found', [])) or 'none'}", cls="text-base-content/60"),
            Span(cls="badge badge-xs " + ("badge-success" if summary.get('has_google_supported') else "badge-ghost"))(
                "Google supported" if summary.get('has_google_supported') else "Not supported"
            ),
        ),
        *[_render_schema_block(s) for s in result.get("schemas_found", [])],
    )


def _render_schema_block(s: dict):
    status_icon = Span("✓", cls="text-success font-bold") if s["is_valid"] else Span("✗", cls="text-error font-bold")
    items = []
    if s["fields_missing_required"]:
        items.append(Div(cls="text-sm text-error")(
            Span("Missing required: "),
            Span(", ".join(s["fields_missing_required"]), cls="font-mono"),
        ))
    if s["fields_missing_recommended"]:
        items.append(Div(cls="text-sm text-warning")(
            Span("Missing recommended: "),
            Span(", ".join(s["fields_missing_recommended"]), cls="font-mono"),
        ))
    for w in s["warnings"]:
        items.append(Div(w, cls="text-sm text-warning"))
    return Div(cls="collapse collapse-arrow border border-base-300 rounded-lg")(
        Input(type="checkbox", cls="peer"),
        Div(cls="collapse-title text-sm font-medium flex items-center gap-2 min-h-0 py-2")(
            status_icon,
            Span(f"{s['type']} ({s['format']})"),
            Span(cls="badge badge-xs " + ("badge-success" if s["google_supported"] else "badge-ghost"))(
                "Google supported" if s["google_supported"] else "Not supported"
            ),
        ),
        Div(cls="collapse-content")(
            *items,
            Pre(cls="mt-2 p-3 bg-base-200 rounded text-xs overflow-x-auto max-h-48 overflow-y-auto")(
                json.dumps(s["raw"], indent=2, ensure_ascii=False),
            ) if s.get("raw") else "",
        ),
    )




def load_or_setup_mapper(session, website):
    "Try to load mapper, return ('ok', mapping) or ('missing', None) or ('error', domain, message)."
    domain = website.url.removeprefix("https://").removeprefix("http://").strip().rstrip("/")
    mapper_path = Path.home() / ".config" / "seootter" / "mappers" / domain / "mapper.py"
    if not mapper_path.exists():
        return ("missing", domain, mapper_path)
    try:
        spec = importlib.util.spec_from_file_location("mapper", mapper_path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        mapping = module.get_url_file_mapping()
        sync_url_mapping(session, website.id, mapping)
        for url, file_path in mapping.items():
            existing = session.exec(select(Article).where(
                Article.website_id == website.id, Article.url == url
            )).first()
            if not existing:
                insert_article(session, Article(website_id=website.id, file_path=file_path, url=url))
        return ("ok", domain, mapping)
    except Exception as e:
        return ("error", domain, str(e))


@rt
def report(id: int, refresh: bool = False, export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Not found", P("Website not found"))
        domain = website.url.removeprefix("https://").removeprefix("http://").strip().rstrip("/")
        if refresh:
            from seootter.models import URLMapping
            for m in session.exec(select(URLMapping).where(URLMapping.website_id == id)):
                session.delete(m)
            session.commit()
            _report_cache.pop(id, None)
            REPORT_CACHE_DIR.joinpath(f"{id}.json").unlink(missing_ok=True)
        status, domain_or_result, result = load_or_setup_mapper(session, website)
        if status == "missing":
            return _report_mapper_setup(id, domain_or_result, result, website)
        if status == "error":
            return Titled("Mapper Error",
                A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4 block"),
                H1("Mapper Failed", cls="text-2xl font-bold mb-2"),
                P(f"Failed to load mapper for {website.url}:", cls="text-base-content/60 mb-2"),
                Div(cls="mockup-code mb-4")(
                    *[Div(cls="px-4")(Span(line, cls="text-error")) for line in result.split("\n")],
                ),
                P("Check your mapper file or internet connection and try again.",
                  cls="text-base-content/60"))

        cached = _load_cached_report(id)
        if cached is not None:
            if export:
                return _report_export_response(cached, export)
            return _report_page(website, domain, cached)

        if export:
            return Div(P("Report not yet generated. Please load the report first.", cls="text-warning"))

        # Loading shell – HTMX fetches content in background
        return Title(f"SEO Report - {website.name}"), Main(cls="container")(
            Div(cls="flex items-center gap-2 mb-4")(
                A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary"),
                Span(cls="grow"),
                A("Edit Mapper", href=edit_mapper.to(id=id), cls="btn btn-outline btn-sm"),
                A("Refresh from Sitemap", href=report.to(id=id, refresh=True),
                  cls="btn btn-outline btn-sm"),
            ),
            H1("SEO Report", cls="text-2xl font-bold mb-1"),
            P(f"{website.url}", cls="text-base-content/60 mb-4"),
            Div(id="report-content", hx_get=f"/report_content?id={id}", hx_trigger="load")(
                Div(cls="flex items-center gap-3 justify-center py-12")(
                    Span(cls="loading loading-spinner loading-lg"),
                    Span("Generating report...", cls="text-base-content/60"),
                ),
            ),
        ),


@rt
def report_content(id: int, refresh: bool = False, export: str = ""):
    if refresh:
        _report_cache.pop(id, None)
        REPORT_CACHE_DIR.joinpath(f"{id}.json").unlink(missing_ok=True)

    if not refresh:
        cached = _load_cached_report(id)
        if cached is not None:
            if export:
                return _report_export_response(cached, export)
            return _report_content_fragment(cached)

    if not refresh:
        prog = _report_progress.get(id)
        if prog and prog.get("status") == "running":
            pct = prog.get("pct", 0)
            msg = prog.get("msg", "Running...")
            return Div(
                Progress(value=str(pct), max="100", cls="w-full"),
                Span(f"{pct}% — {msg}", cls="text-sm ml-2"),
                hx_get=report_progress.to(id=id),
                hx_trigger="every 1s",
                hx_target="this",
                hx_swap="outerHTML",
            )

    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return P("Website not found")
        is_quarto = website.site_type == "quarto"
        domain = website.url.removeprefix("https://").removeprefix("http://").strip().rstrip("/")
        status, domain_or_result, result = load_or_setup_mapper(session, website)
        if status != "ok":
            return Div(
                P("Mapper not configured.", cls="text-warning"),
                A("Set up mapper", href=report.to(id=id), cls="link link-primary"),
            )
    _report_progress[id] = {"status": "running", "pct": 0, "msg": "Starting..."}
    Thread(target=_run_report, args=(id, is_quarto), daemon=True).start()
    return Div(
        Progress(value="0", max="100", cls="w-full"),
        Span("Starting...", cls="text-sm ml-2"),
        hx_get=report_progress.to(id=id),
        hx_trigger="every 1s",
        hx_target="this",
        hx_swap="outerHTML",
    )


def _run_report(id: int, is_quarto: bool):
    def cb(current: int, total: int, msg: str):
        pct = int(current / total * 100) if total else 0
        _report_progress[id] = {"status": "running", "pct": pct, "msg": msg}
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            _report_progress[id] = {"status": "error", "pct": 0, "msg": "Website not found"}
            return
        domain = website.url.removeprefix("https://").removeprefix("http://").strip().rstrip("/")
        report_data = generate_seo_report(
            session=session,
            website_id=id,
            domain=domain,
            is_quarto=is_quarto,
            title_is_h1=is_quarto,
            progress_callback=cb,
        )
        _save_cached_report(id, report_data)
        _report_progress[id] = {"status": "done", "pct": 100, "msg": "Done"}


@rt
def report_progress(id: int):
    prog = _report_progress.get(id)
    if not prog:
        return P("No report in progress", cls="text-sm")
    if prog["status"] == "running":
        return Div(
            Progress(value=str(prog["pct"]), max="100", cls="w-full"),
            Span(f"{prog['pct']}% — {prog['msg']}", cls="text-sm ml-2"),
            hx_get=report_progress.to(id=id),
            hx_trigger="every 1s",
            hx_target="this",
            hx_swap="outerHTML",
        )
    if prog["status"] == "done":
        return Div(
            hx_get=report_content.to(id=id),
            hx_trigger="load",
            hx_target="this",
            hx_swap="outerHTML",
        )(
            Div(cls="flex items-center gap-3 justify-center py-12")(
                Span(cls="loading loading-spinner loading-lg"),
                Span("Loading report...", cls="text-base-content/60"),
            ),
        )
    if prog["status"] == "error":
        return Alert(Span(prog.get("msg", "Unknown error")), cls="alert-error")


def _report_export_response(report_data: dict, fmt: str) -> Response:
    issues = report_data.get("issues", {})
    h = ["Page", "Issues"]
    d = [[url, ", ".join(data["issues"])] for url, data in sorted(
        issues.items(), key=lambda x: len(x[1]["issues"]), reverse=True)[:50]]
    return _export_response(to_csv(h, d) if fmt == "csv" else to_markdown(h, d), "seo_report", fmt)


def _report_page(website: Website, domain: str, report_data: dict) -> FT:
    return Title(f"SEO Report - {website.name}"), Main(cls="container")(
        Div(cls="flex items-center gap-2 mb-4")(
            A("← Back to Dashboard", href=site.to(id=website.id), cls="link link-primary"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=report.to(id=website.id, export="csv"))),
                    Li(A("Markdown", href=report.to(id=website.id, export="md"))),
                ),
            ),
            Button("Regenerate", cls="btn btn-outline btn-sm",
                   hx_get=f"/report_content?id={website.id}&refresh=1", hx_target="#report-content",
                   hx_swap="innerHTML"),
            A("Edit Mapper", href=edit_mapper.to(id=website.id), cls="btn btn-outline btn-sm"),
            A("Refresh from Sitemap", href=report.to(id=website.id, refresh=True),
              cls="btn btn-outline btn-sm"),
        ),
        H1("SEO Report", cls="text-2xl font-bold mb-1"),
        P(f"{website.url}", cls="text-base-content/60 mb-4"),
        Div(id="report-content")(
            _report_stats(report_data),
            _report_issues_table(report_data),
        ),
    ),


def _report_content_fragment(report_data: dict) -> FT:
    return Div(
        _report_stats(report_data),
        _report_issues_table(report_data),
    )


def _report_mapper_setup(id: int, domain_or_result: str, result, website: Website) -> FT:
    mapper_path = result
    domain = domain_or_result
    local_mode = "direct" if website.site_type in ("quarto", "nbdev") else "slug" if website.site_type == "astro" else "slug"

    local_code = """from seootter.content_mapper import map_all_urls_to_files
from seootter.index_tracking import fetch_sitemap_urls


def get_url_file_mapping() -> dict[str, str]:
    urls = fetch_sitemap_urls("https://{0}/sitemap.xml")
    return map_all_urls_to_files(
        base_path="/path/to/your/content",
        site_url="https://{0}",
        urls=urls,
        mode="{1}",
    )""".format(domain, local_mode)

    fetch_code = """from seootter.index_tracking import fetch_sitemap_urls


def get_url_file_mapping() -> dict[str, str]:
    urls = fetch_sitemap_urls("https://{0}/sitemap.xml")
    return {{url: "::fetch::" for url in urls}}""".format(domain)

    return Title(f"Setup Mapper - {website.name}"), Main(cls="container")(
        A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary mb-4 block"),
        H1("Mapper Setup", cls="text-2xl font-bold mb-2"),
        P(f"To generate an SEO report for {website.url}, you need a mapper file.",
          cls="text-base-content/60 mb-1"),
        P(Span("The file will be saved to:", cls="font-semibold"), cls="mb-4"),
        Div(cls="mockup-code mb-6 text-sm")(
            *[Div(cls="px-4")(
                Span(f"~/.config/seootter/mappers/{domain}/mapper.py", cls="text-primary")
            )],
        ),
        Form(method="POST", action="/save_mapper")(
            Input(type="hidden", name="id", value=str(id)),
            Input(type="hidden", name="domain", value=domain),
            Div(cls="tabs tabs-bordered mb-4")(
                Input(type="radio", name="mode_cls", cls="tab", aria_label="Local Files",
                      value="local", checked=True,
                      onchange="document.getElementById('tc').value=lc"),
                Input(type="radio", name="mode_cls", cls="tab", aria_label="CMS / Remote",
                      value="fetch",
                      onchange="document.getElementById('tc').value=fc"),
            ),
            Textarea(local_code,
                     id="tc", name="code",
                     cls="textarea textarea-bordered font-mono text-xs w-full mb-4",
                     rows=20, style="min-height:400px"),
            Input(type="hidden", id="lc", value=local_code),
            Input(type="hidden", id="fc", value=fetch_code),
            Input(type="hidden", name="mode", id="hf-mode", value="fetch"),
            Button("Save & Generate Report",
                   type="submit", cls="btn btn-primary"),
        ),
        Script("""
const lc = document.getElementById('lc').value;
const fc = document.getElementById('fc').value;
document.querySelectorAll('input[name="mode_cls"]').forEach(r => {
    r.addEventListener('change', function() {
        document.getElementById('tc').value = this.value === 'local' ? lc : fc;
        document.getElementById('hf-mode').value = this.value;
    });
});
"""),
    )


@rt
def edit_mapper(id: int):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Not found", P("Website not found"))
        domain = website.url.removeprefix("https://").removeprefix("http://").strip().rstrip("/")
        local_mode = "direct" if website.site_type in ("quarto", "nbdev") else "slug"
        name = website.name
        url = website.url
        mapper_path = Path.home() / ".config" / "seootter" / "mappers" / domain / "mapper.py"
        existing_code = mapper_path.read_text() if mapper_path.exists() else ""
    return Title(f"Edit Mapper - {name}"), Main(cls="container")(
        A("← Back to Report", href=report.to(id=id), cls="link link-primary mb-4 block"),
        H1("Edit Mapper", cls="text-2xl font-bold mb-2"),
        P(f"Edit the URL-to-file mapping for {url}.", cls="text-base-content/60 mb-4"),
        Form(method="POST", action="/save_mapper")(
            Input(type="hidden", name="id", value=str(id)),
            Input(type="hidden", name="domain", value=domain),
            Textarea(existing_code, name="code",
                     cls="textarea textarea-bordered font-mono text-xs w-full mb-4",
                     rows=20, style="min-height:400px"),
            Input(type="hidden", name="mode", value="local"),
            Div(cls="flex gap-2")(
                Button("Save & Regenerate Report", type="submit", cls="btn btn-primary"),
                A("Reset Setup", href=delete_mapper.to(id=id), cls="btn btn-ghost",
                  onclick="return confirm('Reset mapper setup? This will delete the current mapper file and let you re-choose.')"),
            ),
        ),
    )


@rt
def save_mapper(id: int, domain: str, code: str, mode: str = "local"):
    domain = domain.strip().rstrip("/")
    mapper_dir = Path.home() / ".config" / "seootter" / "mappers" / domain
    mapper_dir.mkdir(parents=True, exist_ok=True)
    mapper_path = mapper_dir / "mapper.py"
    mapper_path.write_text(code)
    _report_cache.pop(id, None)
    REPORT_CACHE_DIR.joinpath(f"{id}.json").unlink(missing_ok=True)
    with get_session() as session:
        from seootter.models import URLMapping
        for m in session.exec(select(URLMapping).where(URLMapping.website_id == id)):
            session.delete(m)
        session.commit()
    return RedirectResponse(f"/report?id={id}", status_code=303)


@rt
def delete_mapper(id: int):
    with get_session() as session:
        website = session.get(Website, id)
        if website:
            domain = website.url.removeprefix("https://").removeprefix("http://").strip().rstrip("/")
            mapper_path = Path.home() / ".config" / "seootter" / "mappers" / domain / "mapper.py"
            if mapper_path.exists():
                mapper_path.unlink()
    _report_cache.pop(id, None)
    REPORT_CACHE_DIR.joinpath(f"{id}.json").unlink(missing_ok=True)
    return RedirectResponse(f"/report?id={id}", status_code=303)


@rt
def articles(id: int, export: str = ""):
    with get_session() as session:
        website = session.get(Website, id)
        if not website:
            return Titled("Not found", P("Website not found"))
        articles_list = get_articles_by_website(session, id)

        if export:
            h = ["URL", "Focus Keyword", "Target Goal"]
            d = [[a.url or a.file_path, a.focus_keyword or "", a.target_goal or ""] for a in articles_list]
            return _export_response(to_csv(h, d) if export == "csv" else to_markdown(h, d), "articles", export)

    return Title(f"Articles - {website.name}"), Main(cls="container")(
        Div(cls="flex items-center gap-2 mb-4")(
            A("← Back to Dashboard", href=site.to(id=id), cls="link link-primary"),
            Span(cls="grow"),
            Div(cls="dropdown dropdown-end ml-auto")(
                Label("Export ▾", cls="btn btn-outline btn-sm", tabindex="0"),
                Ul(cls="dropdown-content menu bg-base-100 rounded-box shadow z-10 w-32")(
                    Li(A("CSV", href=articles.to(id=id, export="csv"))),
                    Li(A("Markdown", href=articles.to(id=id, export="md"))),
                ),
            ),
        ),
        H1("Article SEO Metadata", cls="text-2xl font-bold mb-1"),
        P(f"{website.url} — {len(articles_list)} articles", cls="text-base-content/60 mb-4"),
        Div(cls="overflow-x-auto")(
            Table(cls="table table-zebra")(
                Thead(Tr(Th("File / URL"), Th("Focus Keyword"), Th("Target Goal"), Th("Actions"))),
                Tbody(*[
                    Tr(
                        Td(A(a.url or a.file_path, href=a.url or "#", target="_blank",
                             cls="link link-primary text-sm truncate max-w-[300px] block")),
                        Td(a.focus_keyword or "—", cls="text-sm"),
                        Td(a.target_goal or "—", cls="text-sm max-w-[200px] truncate"),
                        Td(Div(cls="flex gap-1")(
                            Btn("Infer", cls="btn btn-outline btn-xs",
                                hx_post=article_infer.to(article_id=a.id),
                                hx_target=f"#infer-result-{a.id}",
                                hx_swap="innerHTML"),
                        )),
                        id=f"article-row-{a.id}",
                    ) for a in articles_list
                ]),
            ),
        ),
        *[Div(id=f"infer-result-{a.id}", cls="mb-4") for a in articles_list],
    ),


@rt
def article_infer(article_id: int):
    with get_session() as session:
        article = session.get(Article, article_id)
        if not article:
            return P("Article not found", cls="text-error")
        try:
            content = get_article_content(article)
        except Exception as e:
            return P(f"Failed to load content: {e}", cls="text-error")
        if not content:
            return Div(cls="alert alert-error text-sm")(
                Span(f"Could not load content (file_path={article.file_path}, url={article.url})"),
            )
        try:
            result = infer_article_seo(content)
        except Exception as e:
            return Div(cls="alert alert-error text-sm")(Span(f"Inference failed: {e}"))
    kw = ", ".join(result.get("secondary_keywords", []))
    return Div(cls="border border-primary rounded-lg p-4 mb-4 bg-base-200", id=f"infer-form-{article_id}")(
        H3("Inferred SEO Metadata", cls="text-sm font-semibold mb-3"),
        Form(
            Input(type="hidden", name="article_id", value=str(article_id)),
            Div(cls="grid grid-cols-1 gap-3")(
                Fieldset(FieldsetLegend("Focus Keyword"),
                    Input(name="focus_keyword", value=result.get("focus_keyword", ""),
                          cls="input input-bordered input-sm w-full")),
                Fieldset(FieldsetLegend("Secondary Keywords (comma-separated)"),
                    Input(name="secondary_keywords", value=kw,
                          cls="input input-bordered input-sm w-full")),
                Fieldset(FieldsetLegend("Target Goal"),
                    Textarea(result.get("target_goal", ""), name="target_goal",
                             cls="textarea textarea-bordered textarea-sm w-full", rows=2)),
            ),
            Div(cls="mt-3 flex justify-end")(
                Btn("Save", cls="btn btn-primary btn-sm", type="submit"),
            ),
            hx_post=article_save.to(),
            hx_target=f"#article-row-{article_id}",
            hx_swap="outerHTML",
        ),
    )


@rt
def article_save(article_id: int, focus_keyword: str = "", secondary_keywords: str = "", target_goal: str = ""):
    with get_session() as session:
        article = session.get(Article, article_id)
        if not article:
            return P("Article not found", cls="text-error")
        kw_list = [k.strip() for k in secondary_keywords.split(",") if k.strip()]
        article.focus_keyword = focus_keyword or None
        article.secondary_keywords = kw_list or None
        article.target_goal = target_goal or None
        article.last_optimized = datetime.now()
        session.add(article)
        session.commit()
        session.refresh(article)

    return Tr(
        Td(A(article.url or article.file_path, href=article.url or "#", target="_blank",
             cls="link link-primary text-sm truncate max-w-[300px] block")),
        Td(article.focus_keyword or "—", cls="text-sm"),
        Td(article.target_goal or "—", cls="text-sm max-w-[200px] truncate"),
        Td(Div(cls="flex gap-1")(
            Btn("Infer", cls="btn btn-outline btn-xs",
                hx_post=article_infer.to(article_id=article.id),
                hx_target=f"#infer-result-{article.id}",
                hx_swap="innerHTML"),
        )),
        id=f"article-row-{article.id}",
    )


_SETTINGS_KEYS = ["LLM_MODEL", "LLM_API_KEY", "LLM_API_BASE", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "SEEOOTTER_DB_URL"]


def _read_settings_env() -> dict[str, str]:
    vals = {k: "" for k in _SETTINGS_KEYS}
    if not SETTINGS_ENV_PATH.exists():
        return vals
    for line in SETTINGS_ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            key = key.strip()
            if key in vals:
                vals[key] = val.strip().strip("\"'")
    return vals


def _write_settings_env(data: dict[str, str]):
    SETTINGS_ENV_PATH.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS_ENV_PATH.exists():
        lines = SETTINGS_ENV_PATH.read_text().splitlines()
        written = set()
        new_lines = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                new_lines.append(line)
                continue
            if "=" in stripped:
                key = stripped.split("=", 1)[0].strip()
                if key in data:
                    new_lines.append(f"{key}={data[key]}")
                    written.add(key)
                else:
                    new_lines.append(line)
        for k, v in data.items():
            if k not in written:
                new_lines.append(f"{k}={v}")
        SETTINGS_ENV_PATH.write_text("\n".join(new_lines) + "\n")
    else:
        SETTINGS_ENV_PATH.write_text("\n".join(f"{k}={v}" for k, v in data.items()) + "\n")


@rt
def settings(msg: str = ""):
    vals = _read_settings_env()
    # fall back to current process env for values not yet saved
    for k in _SETTINGS_KEYS:
        if not vals[k]:
            vals[k] = os.getenv(k, "")
    return Titled("Settings",
        P("Environment variables and API credentials.", cls="text-base-content/60 mb-4"),
        Div(id="settings-msg", cls="alert alert-success mb-4")(
            Span(msg),
        ) if msg else "",
        Form(method="POST", action="/settings_save", cls="max-w-xl")(
            Fieldset(
                FieldsetLegend("LLM Configuration (DSPy / LiteLLM)"),
                P("Uses LiteLLM format: ", cls="text-xs text-base-content/60 mb-2"),
                Pre("  openai/gpt-4o\n  groq/llama-3.3-70b-versatile\n  anthropic/claude-sonnet-4-20250514\n  deepseek/deepseek-chat\n  together/meta-llama/Llama-3.3-70B-Instruct-Turbo",
                    cls="text-xs mb-3"),
                Label("Model", cls="text-sm font-medium"),
                Input(name="LLM_MODEL", value=vals.get("LLM_MODEL", "groq/llama-3.3-70b-versatile"),
                      placeholder="groq/llama-3.3-70b-versatile",
                      cls="input input-bordered w-full mb-2"),
                Div(cls="grid grid-cols-1 md:grid-cols-2 gap-2 mb-2")(
                    Div(
                        Label("API Key", cls="text-sm font-medium"),
                        Input(name="LLM_API_KEY", value=vals.get("LLM_API_KEY", ""),
                              type="password", cls="input input-bordered w-full"),
                    ),
                    Div(
                        Label("API Base URL (optional)", cls="text-sm font-medium"),
                        Input(name="LLM_API_BASE", value=vals.get("LLM_API_BASE", ""),
                              placeholder="https://api.groq.com/openai/v1",
                              cls="input input-bordered w-full"),
                    ),
                ),
                Div(cls="flex items-center gap-2")(
                    Btn("Test LLM", cls="btn btn-outline btn-sm",
                        hx_post=settings_test_llm.to(),
                        hx_include="[name='LLM_MODEL'],[name='LLM_API_KEY'],[name='LLM_API_BASE']",
                        hx_target="#llm-test-result",
                        hx_swap="innerHTML"),
                    Span(id="llm-test-result", cls="text-sm"),
                ),
                cls="border border-base-300 rounded-lg p-4 mb-4",
            ),
            Fieldset(
                FieldsetLegend("Google OAuth (for GSC API)"),
                Div(cls="grid grid-cols-1 md:grid-cols-2 gap-3 mb-2")(
                    Div(
                        Label("Client ID", cls="text-sm font-medium"),
                        Input(name="GOOGLE_CLIENT_ID", value=vals.get("GOOGLE_CLIENT_ID", ""),
                              type="text", cls="input input-bordered w-full"),
                    ),
                    Div(
                        Label("Client Secret", cls="text-sm font-medium"),
                        Input(name="GOOGLE_CLIENT_SECRET", value=vals.get("GOOGLE_CLIENT_SECRET", ""),
                              type="password", cls="input input-bordered w-full"),
                    ),
                ),
                Div(cls="flex items-center gap-2")(
                    Btn("Test GSC", cls="btn btn-outline btn-sm",
                        hx_post=settings_test_gsc.to(),
                        hx_include="[name='GOOGLE_CLIENT_ID'],[name='GOOGLE_CLIENT_SECRET']",
                        hx_target="#gsc-test-result",
                        hx_swap="innerHTML"),
                    Span(id="gsc-test-result", cls="text-sm"),
                ),
                cls="border border-base-300 rounded-lg p-4 mb-4",
            ),
            Fieldset(
                FieldsetLegend("Database"),
                Label("SEEOOTTER_DB_URL", cls="text-sm font-medium"),
                Input(name="SEEOOTTER_DB_URL", value=vals.get("SEEOOTTER_DB_URL", ""),
                      type="text", cls="input input-bordered w-full"),
                cls="border border-base-300 rounded-lg p-4 mb-4",
            ),
            Div(cls="flex justify-end")(
                Btn("Save Settings", cls="btn btn-primary", type="submit"),
            ),
        ),
    )


@rt
def settings_save(LLM_MODEL: str = "", LLM_API_KEY: str = "", LLM_API_BASE: str = "",
                  GOOGLE_CLIENT_ID: str = "", GOOGLE_CLIENT_SECRET: str = "",
                  SEEOOTTER_DB_URL: str = ""):
    # keep existing env values as fallback when form fields are empty
    LLM_MODEL = LLM_MODEL or os.getenv("LLM_MODEL", "groq/llama-3.3-70b-versatile")
    LLM_API_KEY = LLM_API_KEY or os.getenv("LLM_API_KEY") or os.getenv("GROQ_API_KEY", "")
    LLM_API_BASE = LLM_API_BASE or os.getenv("LLM_API_BASE", "")
    GOOGLE_CLIENT_ID = GOOGLE_CLIENT_ID or os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET = GOOGLE_CLIENT_SECRET or os.getenv("GOOGLE_CLIENT_SECRET", "")
    SEEOOTTER_DB_URL = SEEOOTTER_DB_URL or os.getenv("SEEOOTTER_DB_URL", "")
    data = {
        "LLM_MODEL": LLM_MODEL,
        "LLM_API_KEY": LLM_API_KEY,
        "LLM_API_BASE": LLM_API_BASE,
        "GOOGLE_CLIENT_ID": GOOGLE_CLIENT_ID,
        "GOOGLE_CLIENT_SECRET": GOOGLE_CLIENT_SECRET,
        "SEEOOTTER_DB_URL": SEEOOTTER_DB_URL,
    }
    _write_settings_env(data)
    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        _write_gsc_secrets(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    load_dotenv(SETTINGS_ENV_PATH, override=True)
    return RedirectResponse("/settings?msg=Settings+saved+successfully", status_code=303)


def _write_gsc_secrets(client_id: str, client_secret: str):
    """Write GOOGLE_CLIENT_ID/SECRET to ~/.config/seootter/client_secrets.json for GSCAuth."""
    import json
    secrets = {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
            "redirect_uris": ["http://localhost"],
        }
    }
    path = CONFIG_DIR / "client_secrets.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(secrets, indent=2))


@rt
def settings_test_llm(LLM_MODEL: str = "", LLM_API_KEY: str = "", LLM_API_BASE: str = ""):
    try:
        import dspy
        kwargs = dict(model=LLM_MODEL, api_key=LLM_API_KEY)
        if LLM_API_BASE:
            kwargs["api_base"] = LLM_API_BASE
        lm = dspy.LM(**kwargs)
        dspy.configure(lm=lm)
        response = lm("Reply with exactly: LLM is working!")
        text = response[0] if isinstance(response, list) else str(response)
        return Div(cls="text-success text-sm")(
            Span(f"✓ {text[:200]}"),
        )
    except Exception as e:
        return Div(cls="text-error text-sm")(
            Span(f"✗ {e}"),
        )


@rt
def settings_test_gsc(GOOGLE_CLIENT_ID: str = "", GOOGLE_CLIENT_SECRET: str = ""):
    secrets = CONFIG_DIR / "client_secrets.json"
    if not secrets.exists():
        if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
            return Div(cls="text-warning text-sm")(
                Span("No client_secrets.json found. Enter credentials and Save first."),
            )
        _write_gsc_secrets(GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET)
    try:
        auth = GSCAuth(secrets_file=str(secrets))
        creds = auth.get_credentials()
        sites = get_verified_sites(auth)
        if sites:
            return Div(cls="text-success text-sm")(
                Span("✓ Authenticated. Verified sites:"),
                Ul(*[Li(f"{s['site_url']} — {s['permission_level']}") for s in sites],
                   cls="mt-1 text-xs"),
            )
        return Div(cls="text-warning text-sm")(
            Span("✓ Authenticated but no verified sites found."),
        )
    except ValueError as e:
        return Div(cls="text-warning text-sm")(
            Span(f"✗ {e}"),
        )
    except Exception as e:
        return Div(cls="text-error text-sm")(
            Span(f"✗ {e}"),
        )


# ── Wuilt Store Management ────────────────────────────────────────

@rt("/wuILT/add")
def get():
    return Title("Add Wuilt Store"), Main(cls="container")(
        A("← All Stores", href="/wuILT", cls="text-sm text-base-content/60 hover:text-primary mb-3 inline-flex items-center gap-1"),
        Div(cls="page-header page-fade")(
            H1("🏪 Add Wuilt Store", cls="text-2xl font-bold"),
            P("Connect a Wuilt e-commerce store to sync products and optimize SEO", cls="subtitle"),
        ),
        Div(cls="max-w-2xl")(
            hint_box("You'll find your Store ID and API Key in your Wuilt dashboard under Settings → API. The store domain should match the domain in Google Search Console.", icon="💡"),
            Div(cls="section mt-4")(
                Form(method="post", action="/wuILT/add")(
                    Div(cls="grid grid-cols-1 md:grid-cols-2 gap-4")(
                        Fieldset(FieldsetLegend(cls="text-xs font-medium")("Store Name *"),
                            Input(name="name", placeholder="My Store", required=True, cls="input w-full"),
                            P("A friendly display name", cls="text-xs text-base-content/50 mt-1")),
                        Fieldset(FieldsetLegend(cls="text-xs font-medium")("Store ID *"),
                            Input(name="store_id", placeholder="Store_...", required=True, cls="input w-full font-mono"),
                            P("From Wuilt → Settings → API", cls="text-xs text-base-content/50 mt-1")),
                        Fieldset(FieldsetLegend(cls="text-xs font-medium")("API Key *"),
                            Input(name="api_key", type="password", required=True, cls="input w-full"),
                            P("Stored securely, never displayed again", cls="text-xs text-base-content/50 mt-1")),
                        Fieldset(FieldsetLegend(cls="text-xs font-medium")("Locale"),
                            Select(
                                Option("Arabic (ar)", value="ar", selected=True),
                                Option("English (en)", value="en"),
                                name="locale", cls="select w-full"),
                            P("Default locale for product URLs", cls="text-xs text-base-content/50 mt-1")),
                        Fieldset(FieldsetLegend(cls="text-xs font-medium")("Store Domain"),
                            Input(name="store_domain", placeholder="example.com", cls="input w-full"),
                            P("Used to correlate with GSC data", cls="text-xs text-base-content/50 mt-1"),
                            cls="md:col-span-2"),
                    ),
                    Div(cls="mt-6 flex items-center justify-between gap-2 pt-4 border-t border-base-200")(
                        P("After adding, you can sync products and pages from the store dashboard.", cls="text-xs text-base-content/50"),
                        Div(cls="flex gap-2")(
                            A("Cancel", href="/wuILT", cls="btn btn-ghost"),
                            Btn("Add Store →", cls="btn btn-primary", type="submit"),
                        ),
                    ),
                ),
            ),
        ),
    )


@rt("/wuILT/add")
def post(name: str, store_id: str, api_key: str, locale: str = "ar", store_domain: str = ""):
    with get_session() as session:
        add_or_update_wuilt_store(session, store_id=store_id, name=name, api_key=api_key, locale=locale, store_domain=store_domain)
        session.commit()
    return RedirectResponse("/wuILT", status_code=303)


@rt("/wuILT")
def wuilt_list():
    with get_session() as session:
        stores = session.exec(select(WuiltStore)).all()
        total = len(stores)
        return Title("Wuilt Stores"), Main(cls="container")(
            page_header(
                "🏪 Wuilt Stores",
                f"{total} store{'s' if total != 1 else ''} connected" if stores else "Connect your Wuilt e-commerce stores to optimize product SEO",
                actions=[A("+ Add Store", href="/wuILT/add", cls="btn btn-primary btn-sm")],
            ),
            Div(cls="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4")(
                *[A(
                    Div(cls="card-body p-5")(
                        Div(cls="flex items-start justify-between mb-3")(
                            Div(cls="flex items-center gap-3 min-w-0")(
                                Div(cls="w-10 h-10 rounded-lg bg-primary/10 flex items-center justify-center text-xl shrink-0")("🏪"),
                                Div(cls="min-w-0")(
                                    H3(s.name, cls="card-title text-base truncate"),
                                    P(s.store_id, cls="text-xs text-base-content/50 font-mono truncate"),
                                ),
                            ),
                            Span(s.locale.upper(), cls="badge badge-soft badge-sm shrink-0"),
                        ),
                        Div(cls="flex items-center justify-between text-xs text-base-content/50 pt-3 border-t border-base-200")(
                            Div(cls="flex items-center gap-1 truncate")(
                                Span("🌐"),
                                Span(s.store_domain or "No domain", cls="truncate"),
                            ),
                            Span("→", cls="text-base-content/30 group-hover:text-primary"),
                        ),
                    ),
                    href=f"/wuILT/{s.id}",
                    cls="card card-border bg-base-100 hover:bg-base-200 hover:shadow-lg hover:-translate-y-1 transition-all duration-200 no-underline group",
                ) for s in stores]
            ) if stores else empty_state(
                "🏪",
                "No Wuilt stores yet",
                "Connect your first Wuilt e-commerce store to sync products, optimize SEO, and track Google Search Console performance for your storefront.",
                cta_text="Add Your First Store",
                cta_href="/wuILT/add",
            ),
            Div(cls="mt-6")(
                hint_box("Stores connect via Wuilt's GraphQL API. You'll need your Store ID and API key from your Wuilt dashboard.", icon="💡"),
            ) if not stores else "",
        )


@rt("/wuILT/{store_pk:int}")
def wuilt_store(store_pk: int):
    with get_session() as session:
        store = session.get(WuiltStore, store_pk)
        if not store:
            return Titled("Store not found", P("Store not found"))
        products = get_wuilt_products(session, store_pk)
        pages = get_wuilt_pages(session, store_pk)
        website, site_url, base_url = _resolve_store_site(store, session)
        store_metrics = get_site_metrics(session, site_url, days=30) if site_url else None
    store_delete_modal_id = f"store-delete-modal-{store_pk}"
    try:
        store_seo = sync_wuilt_store(api_key=store.api_key, store_id=store.store_id, locale=store.locale)
    except Exception:
        store_seo = None
    try:
        client = WuiltClient(api_key=store.api_key, store_id=store.store_id, locale=store.locale)
        collections = client.get_collections()
    except Exception:
        collections = []
    return Title(store.name), Main(cls="container")(
        A("← All Stores", href="/wuILT", cls="text-sm text-base-content/60 hover:text-primary mb-3 inline-flex items-center gap-1"),
        Div(cls="page-header page-fade")(
            Div(cls="page-header-row")(
                Div(cls="flex items-center gap-3 grow min-w-0")(
                    Div(cls="w-12 h-12 rounded-lg bg-primary/10 flex items-center justify-center text-2xl shrink-0")("🏪"),
                    Div(cls="min-w-0")(
                        H1(store.name, cls="text-2xl font-bold truncate"),
                        P(Span("Store ID: ", cls="text-base-content/50"), Span(store.store_id, cls="font-mono text-xs"), cls="text-sm truncate"),
                    ),
                ),
                Div(cls="page-header actions")(
                    Button("🗑 Delete", cls="btn btn-soft btn-error btn-sm",
                           onclick=f"document.getElementById('{store_delete_modal_id}').showModal()"),
                    A("📦 Sync Products", href=f"/wuILT/{store_pk}/sync", cls="btn btn-outline btn-primary btn-sm"),
                    A("📄 Sync Pages", href=f"/wuILT/{store_pk}/sync-pages", cls="btn btn-outline btn-secondary btn-sm"),
                    A("✨ Optimize All", href=f"/wuILT/{store_pk}/optimize", cls="btn btn-primary btn-sm"),
                ),
            ),
        ),
        Dialog(id=store_delete_modal_id, cls="modal")(
            Div(cls="modal-box")(
                Div(cls="flex items-center gap-3 mb-3")(
                    Span("⚠️", cls="text-2xl"),
                    H3("Delete this store?", cls="font-bold text-lg"),
                ),
                P("This will permanently delete ", Span(store.name, cls="font-semibold"), " and all of its products, pages, and SEO data. This action cannot be undone.", cls="text-base-content/60 mb-6"),
                Div(cls="modal-action")(
                    Form(method="dialog")(Button("Cancel", cls="btn btn-ghost")),
                    Form(method="post", action=f"/wuILT/{store_pk}/delete", cls="inline")(
                        Button("Yes, delete forever", cls="btn btn-error", type="submit"),
                    ),
                ),
            ),
            Form(method="dialog", cls="modal-backdrop")(Button("close")),
        ),
        Div(cls="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6")(
            stat_card("Products", len(products), icon="📦", href=f"#products"),
            stat_card("Collections", len(collections), icon="📚", href=f"#collections"),
            stat_card("Pages", len(pages), icon="📄", href=f"#pages"),
            stat_card("Locale", store.locale.upper(), icon="🌐"),
        ),
        Div(cls="section mb-6")(
            Div(cls="flex items-center justify-between mb-4")(
                Div()(
                    H2("🏠 Homepage SEO", cls="section-title"),
                    P("How your store appears in search engine results", cls="section-subtitle"),
                ),
                A("📊 GSC Data →", href=f"/wuILT/{store_pk}/homepage-gsc", cls="btn btn-ghost btn-sm") if base_url else "",
            ),
            Div(cls="grid grid-cols-1 md:grid-cols-2 gap-4 mb-4")(
                Div()(
                    Span("SEO Title", cls="label"),
                    P(store_seo["seo_title"] or "—", cls="font-medium mt-1") if store_seo else P("Could not load from API", cls="text-sm text-error mt-1"),
                ),
                Div()(
                    Span("SEO Description", cls="label"),
                    P(store_seo["seo_description"] or "—", cls="text-base-content/70 mt-1 line-clamp-3") if store_seo else P("Could not load from API", cls="text-sm text-error mt-1"),
                ),
            ),
            Div(cls="flex flex-wrap gap-2 pt-3 border-t border-base-200")(
                A("✏️ Edit SEO", href=f"/wuILT/{store_pk}/edit", cls="btn btn-sm btn-outline"),
                A("🌐 View Store", href=f"https://{store.store_domain}" if store.store_domain else "#",
                  target="_blank", cls="btn btn-sm btn-ghost" + ("" if store.store_domain else " btn-disabled")),
            ) if store_seo else "",
        ),
        Div(cls="mb-6")(
            hint_box([
                Span(Strong("🔗 Linked to Website: "), cls=""),
                A(website.name or website.url, href=site.to(id=website.id), cls="link link-primary font-medium") if website and store.website_id
                else Span(f"Using store_domain: {store.store_domain or '(not set)'}. Link a Website for better GSC correlation.", cls="text-base-content/70"),
            ], icon="ℹ️"),
        ) if (website or store.store_domain or not store.website_id) else "",
        Div(cls="section mb-6")(
            Div(cls="flex items-center justify-between mb-4")(
                Div()(
                    H2("📊 GSC Performance (30d)", cls="section-title"),
                    P("Google Search Console metrics for this store's domain", cls="section-subtitle"),
                ),
            ),
            Div(cls="grid grid-cols-2 lg:grid-cols-4 gap-4")(
                stat_card("Clicks", f"{store_metrics['clicks']:,}" if store_metrics else "—", icon="👆"),
                stat_card("Impressions", f"{store_metrics['impressions']:,}" if store_metrics else "—", icon="👁️"),
                stat_card("Avg Position", f"{store_metrics['avg_position']}" if store_metrics else "—", icon="📍"),
                stat_card("Avg CTR", f"{store_metrics['avg_ctr']}%" if store_metrics else "—", icon="📈"),
            ) if store_metrics else Div(cls="text-center py-6 text-base-content/50")(
                Span("📊", cls="text-3xl block mb-2 opacity-50"),
                P("No GSC data synced yet", cls="font-medium"),
                P("Sync GSC data from the linked Website dashboard first.", cls="text-sm mt-1"),
            ),
        ) if site_url else "",
        Div(cls="section mb-6", id="products")(
            Div(cls="flex items-center justify-between mb-4")(
                Div()(
                    H2(f"📦 Products ({len(products)})", cls="section-title"),
                    P("Your synced product catalog", cls="section-subtitle"),
                ),
                A("Sync →", href=f"/wuILT/{store_pk}/sync", cls="btn btn-ghost btn-sm"),
            ),
            Div(cls="overflow-x-auto")(
                Table(cls="table table-zebra table-hover")(
                    Thead(Tr(
                        Th("Title"),
                        Th("Handle"),
                        Th("Price", cls="text-right"),
                        Th("Optimized", cls="text-center"),
                        Th("GSC", cls="text-center"),
                        Th("Schema", cls="text-center"),
                        Th("Synced", cls="text-right"),
                    )),
                    Tbody(*[
                        Tr(
                            Td(A(p.title, href=f"/wuILT/{store_pk}/product-gsc/{p.id}" if base_url else "#",
                                cls="link link-hover font-medium text-sm"),
                               cls="max-w-64"),
                            Td(Span(p.handle, cls="text-xs text-base-content/50 font-mono"), cls="max-w-40"),
                            Td(Span(f"{p.price:.2f}" if p.price is not None else "—", cls="font-mono text-sm"),
                               cls="text-right"),
                            Td(
                                Span("✅", cls="text-success", title=f"Optimized {p.last_optimized_at.strftime('%Y-%m-%d %H:%M')}") if p.last_optimized_at
                                else Span("—", cls="text-base-content/30"),
                                cls="text-center",
                            ),
                            Td(
                                A("📊", href=f"/wuILT/{store_pk}/product-gsc/{p.id}", cls="btn btn-xs btn-ghost",
                                  title="View GSC analytics for this product") if base_url
                                else Span("—", cls="text-base-content/30", title="Set store_domain or link a Website to enable GSC"),
                                cls="text-center",
                            ),
                            Td(
                                A("🔍", href="#", cls="btn btn-xs btn-ghost",
                                  hx_get=f"/wuILT/{store_pk}/product-schema/{p.id}",
                                  hx_target="#wuilt-modal-content",
                                  hx_swap="innerHTML",
                                  title="Validate schema & predict schemas"),
                                cls="text-center",
                            ),
                            Td(p.synced_at.strftime("%b %d") if p.synced_at else "—", cls="text-right text-xs text-base-content/50"),
                        ) for p in products
                    ]) if products else Tr(Td(empty_state("📦", "No products yet", "Sync your store to import products from Wuilt.", cta_text="Sync Products", cta_href=f"/wuILT/{store_pk}/sync"), colspan="7")),
                ),
            ),
        ),
        H2(f"Collections ({len(collections)})", cls="text-lg font-semibold mb-3"),
        Div(cls="overflow-x-auto")(
            Table(cls="table table-zebra")(
                Thead(Tr(Th("Title"), Th("Handle"), Th("SEO Title"), Th("GSC"))),
                Tbody(*[
                    Tr(
                        Td(c["title"], cls="font-medium"),
                        Td(c["handle"], cls="text-sm text-base-content/60"),
                        Td((c.get("seo") or {}).get("title") or "—", cls="text-sm"),
                        Td(
                            A("📊", href=f"/wuILT/{store_pk}/collection-gsc/{c['handle']}", cls="btn btn-xs btn-ghost",
                              title="View GSC analytics for this collection") if base_url
                            else Span("—", cls="text-base-content/30"),
                        ),
                    ) for c in collections
                ]) if collections else Tr(Td("No collections synced yet.", colspan="4")),
            ),
        ),
        H2(f"Pages ({len(pages)})", cls="text-lg font-semibold mb-3"),
        Div(cls="overflow-x-auto")(
            Table(cls="table table-zebra")(
                Thead(Tr(Th("Name"), Th("Handle"), Th("Type"), Th("Status"), Th("SEO Title"), Th("GSC"))),
                Tbody(*[
                    Tr(
                        Td(p.name),
                        Td(p.handle, cls="text-sm text-base-content/60"),
                        Td(p.page_type, cls="text-sm"),
                        Td(Span(p.status, cls="badge badge-soft badge-sm " + ("badge-success" if p.status == "PUBLISHED" else "badge-ghost"))),
                        Td(p.seo_title or "—", cls="text-sm"),
                        Td(
                            A("📊", href=f"/wuILT/{store_pk}/page-gsc/{p.id}", cls="btn btn-xs btn-ghost",
                              title="View GSC analytics for this page") if base_url
                            else Span("—", cls="text-base-content/30", title="Set store_domain or link a Website to enable GSC"),
                        ),
                    ) for p in pages
                ]) if pages else Tr(Td("No pages synced yet.", colspan="6")),
            ),
        ),
        Dialog(id="wuilt-modal", cls="modal")(
            Div(cls="modal-box max-w-3xl")(
                Div(cls="flex justify-end")(
                    Form(method="dialog")(Button("✕", cls="btn btn-sm btn-circle btn-ghost")),
                ),
                Div(id="wuilt-modal-content"),
            ),
            Form(method="dialog", cls="modal-backdrop")(Button("close")),
        ),
    )


def _store_base_url(store) -> str:
    """Build clean base URL from store_domain, stripping any locale path to avoid duplication."""
    raw = store.store_domain or ""
    raw = raw.replace("https://", "").replace("http://", "").strip().rstrip("/")
    for loc in ("/ar", "/en"):
        if raw.endswith(loc):
            raw = raw[: -len(loc)]
            break
    return f"https://{raw}"


def _resolve_store_site(store, session):
    """Resolve site_url, base_url, and website from a store record."""
    website = None
    site_url = None
    base_url = None
    if store.website_id:
        website = session.get(Website, store.website_id)
    if not website and store.store_domain:
        raw = store.store_domain.replace("https://", "").replace("http://", "").strip().rstrip("/")
        for loc in ("/ar", "/en"):
            if raw.endswith(loc):
                raw = raw[: -len(loc)]
                break
        website = session.exec(select(Website).where(Website.url.contains(raw))).first()
    if website:
        domain = website.url.replace("https://", "").replace("http://", "").strip().rstrip("/")
        site_url = get_site_url(website.url)
        base_url = website.url.strip().rstrip("/")
    elif store.store_domain:
        base_url = _store_base_url(store)
        raw_domain = base_url.replace("https://", "")
        site_url = f"sc-domain:{raw_domain}"
    return website, site_url, base_url


def _render_gsc_analysis_modal(session, site_url, page_url, title, page_type=""):
    """Render full GSC analysis for any page URL (analytics, queries, trends, wins, green keywords, FAQ)."""
    start, end = get_date_range("last_days", days=30)
    analytics = get_page_analytics(session, site_url, page_url, start, end)
    queries = get_top_queries(session, site_url, start, end, page_path=page_url, limit=20)
    trends = detect_query_trends(session, site_url, page_path=page_url, days=30, limit=50)
    wins = get_wins(session, site_url, start, end, page_url=page_url)

    trend_map = {t["query"]: t for t in trends}
    rising = [t for t in trends if t["trend"] == "rising"][:5]
    declining = [t for t in trends if t["trend"] == "declining"][:5]
    green = [t for t in trends if t["trend"] == "rising" and t["prev_impressions"] <= 5][:5]
    faq_queries = extract_faq_queries(queries)
    wins_page = [w for w in wins if w["avg_position"] >= 10][:5]

    has_data = analytics['total_clicks'] > 0 or analytics['total_impressions'] > 0 or queries

    if not has_data:
        return empty_state(
            "📊",
            "No GSC data yet",
            "This page hasn't accumulated enough Google Search Console data. Either this page is new, hasn't been indexed yet, or you need to sync GSC data from the linked Website dashboard.",
        )

    return Div(cls="space-y-6")(
        Div(cls="section")(
            Div(cls="flex items-center justify-between mb-4")(
                Div()(
                    H2("📈 Performance (30d)", cls="section-title"),
                    P("Aggregate metrics from Google Search Console", cls="section-subtitle"),
                ),
            ),
            Div(cls="grid grid-cols-2 lg:grid-cols-4 gap-4")(
                stat_card("Clicks", f"{analytics['total_clicks']:,}", icon="👆"),
                stat_card("Impressions", f"{analytics['total_impressions']:,}", icon="👁️"),
                stat_card("Avg Position", f"{analytics['avg_position']:.1f}", icon="📍"),
                stat_card("Avg CTR", f"{analytics['avg_ctr']*100:.1f}%" if analytics['avg_ctr'] else "0%", icon="📈"),
            ),
        ),
        Div(cls="section")(
            H2("🔍 Query Analysis", cls="section-title mb-1"),
            P("Trends, opportunities, and patterns from search queries", cls="section-subtitle mb-4"),
            Div(cls="grid grid-cols-1 md:grid-cols-2 gap-4")(
                Div(cls="p-4 rounded-lg border border-success/20 bg-success/5")(
                    Div(cls="flex items-center justify-between mb-2")(
                        Span("📈 Rising", cls="font-semibold text-sm text-success"),
                        Span(f"{len(rising)}", cls="badge badge-soft-success badge-sm"),
                    ),
                    Ul(*[Li(Span(t["query"], cls="font-medium text-sm"), Br(), Span(f"pos {t['recent_position']:.1f} · Δ{t['position_change']:+.1f}", cls="text-xs text-base-content/50")) for t in rising],
                       cls="space-y-1.5") if rising else P("No rising queries yet", cls="text-sm text-base-content/50 italic"),
                ),
                Div(cls="p-4 rounded-lg border border-warning/20 bg-warning/5")(
                    Div(cls="flex items-center justify-between mb-2")(
                        Span("📉 Declining", cls="font-semibold text-sm text-warning"),
                        Span(f"{len(declining)}", cls="badge badge-soft-warning badge-sm"),
                    ),
                    Ul(*[Li(Span(t["query"], cls="font-medium text-sm"), Br(), Span(f"pos {t['recent_position']:.1f} · Δ{t['position_change']:+.1f}", cls="text-xs text-base-content/50")) for t in declining],
                       cls="space-y-1.5") if declining else P("No declining queries", cls="text-sm text-base-content/50 italic"),
                ),
                Div(cls="p-4 rounded-lg border border-accent/20 bg-accent/5")(
                    Div(cls="flex items-center justify-between mb-2")(
                        Span("🟢 Green Keywords", cls="font-semibold text-sm text-accent"),
                        Span(f"{len(green)}", cls="badge badge-soft-info badge-sm"),
                    ),
                    Ul(*[Li(Span(t["query"], cls="font-medium text-sm"), Br(), Span(f"{t['recent_impressions']} impr · pos {t['recent_position']:.1f}", cls="text-xs text-base-content/50")) for t in green],
                       cls="space-y-1.5") if green else P("No green keywords yet", cls="text-sm text-base-content/50 italic"),
                ),
                Div(cls="p-4 rounded-lg border border-info/20 bg-info/5")(
                    Div(cls="flex items-center justify-between mb-2")(
                        Span("🏆 Wins (low rank)", cls="font-semibold text-sm text-info"),
                        Span(f"{len(wins_page)}", cls="badge badge-soft-info badge-sm"),
                    ),
                    Ul(*[Li(Span(w["query"], cls="font-medium text-sm"), Br(), Span(f"{w['total_impressions']} impr · pos {w['avg_position']:.1f}", cls="text-xs text-base-content/50")) for w in wins_page],
                       cls="space-y-1.5") if wins_page else P("No high-impression / low-rank queries", cls="text-sm text-base-content/50 italic"),
                ),
            ),
        ),
        Div(cls="section")(
            Div(cls="flex items-center justify-between mb-3")(
                H2("❓ FAQ Opportunities", cls="section-title mb-0"),
                Span(f"{len(faq_queries)}", cls="badge badge-soft-info badge-sm"),
            ),
            P("Question queries that could become FAQ schema on this page", cls="section-subtitle mb-3"),
            Div(cls="flex flex-wrap gap-2")(
                *[Span(q, cls="badge badge-soft badge-sm cursor-default", title=q) for q in faq_queries[:15]]
            ) if faq_queries else P("No question queries found", cls="text-sm text-base-content/50 italic"),
        ),
        Div(cls="section")(
            Div(cls="flex items-center justify-between mb-3")(
                H2(f"🔑 Top Queries ({len(queries)})", cls="section-title mb-0"),
            ),
            P("All search queries driving traffic to this page", cls="section-subtitle mb-3"),
            Div(cls="overflow-x-auto")(
                Table(cls="table table-zebra table-hover table-sm")(
                    Thead(Tr(
                        Th("#", cls="w-8"),
                        Th("Query"),
                        Th("Clicks", cls="text-right"),
                        Th("Impr", cls="text-right"),
                        Th("Pos", cls="text-right"),
                        Th("CTR", cls="text-right"),
                    )),
                    Tbody(*[
                        Tr(
                            Td(Span(str(i + 1), cls="text-base-content/40 text-xs font-mono")),
                            Td(A(q["query"], href=f"https://www.google.com/search?q={q['query']}", target="_blank", cls="link link-hover text-sm")),
                            Td(f"{q['total_clicks']:,}", cls="text-right font-medium"),
                            Td(f"{q['total_impressions']:,}", cls="text-right text-base-content/70"),
                            Td(Span(f"{q['avg_position']:.1f}", cls="badge badge-ghost badge-sm"), cls="text-right"),
                            Td(Span(f"{q['avg_ctr']*100:.1f}%", cls=("text-success" if q['avg_ctr'] * 100 >= 5 else "text-base-content/70")), cls="text-right"),
                        ) for i, q in enumerate(queries)
                    ]) if queries else Tr(Td(P("No queries found for this page in the last 30 days.", cls="text-sm text-base-content/50 italic text-center py-4"), colspan="6")),
                ),
            ),
        ),
    )


@rt("/wuILT/{store_pk:int}/product-gsc/{pid:int}")
def wuilt_product_gsc(store_pk: int, pid: int):
    try:
        with get_session() as session:
            store = session.get(WuiltStore, store_pk)
            product = session.get(WuiltProduct, pid)
            if not store or not product or product.wuilt_store_id != store_pk:
                return Title("Not found"), Main(cls="container")(empty_state("🔍", "Product not found", "This product doesn't exist or has been deleted.", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
            _, site_url, base_url = _resolve_store_site(store, session)
            if not site_url:
                return Title("No GSC"), Main(cls="container")(empty_state("📊", "No GSC connection", "Set the store domain or link a Website to enable Google Search Console data.", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
            product_url = f"{base_url}/{store.locale}/product/all/{product.handle}"
            analysis = _render_gsc_analysis_modal(session, site_url, product_url, product.title, "product")
    except Exception as e:
        return Title("Error"), Main(cls="container")(empty_state("❌", "Something went wrong", f"Error: {e}", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
    return Title(f"{product.title} - GSC - {store.name}"), Main(cls="container")(
        page_header(
            f"📊 {product.title}",
            f"Google Search Console analysis · {store.name}",
            back=f"/wuILT/{store_pk}",
            actions=[A("🌐 Open page", href=product_url, target="_blank", cls="btn btn-ghost btn-sm")],
        ),
        analysis,
    )


@rt("/wuILT/{store_pk:int}/page-gsc/{pid:int}")
def wuilt_page_gsc(store_pk: int, pid: int):
    try:
        with get_session() as session:
            store = session.get(WuiltStore, store_pk)
            page = session.get(WuiltPage, pid)
            if not store or not page or page.wuilt_store_id != store_pk:
                return Title("Not found"), Main(cls="container")(empty_state("🔍", "Page not found", "This page doesn't exist or has been deleted.", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
            _, site_url, base_url = _resolve_store_site(store, session)
            if not site_url:
                return Title("No GSC"), Main(cls="container")(empty_state("📊", "No GSC connection", "Set the store domain or link a Website to enable Google Search Console data.", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
            page_url = f"{base_url}/{store.locale}/{page.handle}"
            analysis = _render_gsc_analysis_modal(session, site_url, page_url, page.name, "page")
    except Exception as e:
        return Title("Error"), Main(cls="container")(empty_state("❌", "Something went wrong", f"Error: {e}", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
    return Title(f"{page.name} - GSC - {store.name}"), Main(cls="container")(
        page_header(
            f"📄 {page.name}",
            f"Google Search Console analysis · {store.name}",
            back=f"/wuILT/{store_pk}",
            actions=[A("🌐 Open page", href=page_url, target="_blank", cls="btn btn-ghost btn-sm")],
        ),
        analysis,
    )


@rt("/wuILT/{store_pk:int}/homepage-gsc")
def wuilt_homepage_gsc(store_pk: int):
    try:
        with get_session() as session:
            store = session.get(WuiltStore, store_pk)
            if not store:
                return Title("Not found"), Main(cls="container")(empty_state("🔍", "Store not found", "This store doesn't exist or has been deleted.", cta_text="← All Stores", cta_href="/wuILT"))
            _, site_url, base_url = _resolve_store_site(store, session)
            if not site_url:
                return Title("No GSC"), Main(cls="container")(empty_state("📊", "No GSC connection", "Set the store domain or link a Website to enable Google Search Console data.", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
            homepage_url = f"{base_url}/{store.locale}"
            analysis = _render_gsc_analysis_modal(session, site_url, homepage_url, f"{store.name} — Homepage", "homepage")
    except Exception as e:
        return Title("Error"), Main(cls="container")(empty_state("❌", "Something went wrong", f"Error: {e}", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
    return Title(f"Homepage GSC - {store.name}"), Main(cls="container")(
        page_header(
            "🏠 Homepage",
            f"Google Search Console analysis · {store.name}",
            back=f"/wuILT/{store_pk}",
            actions=[A("🌐 Open homepage", href=homepage_url, target="_blank", cls="btn btn-ghost btn-sm")],
        ),
        analysis,
    )


@rt("/wuILT/{store_pk:int}/collection-gsc/{handle}")
def wuilt_collection_gsc(store_pk: int, handle: str):
    try:
        with get_session() as session:
            store = session.get(WuiltStore, store_pk)
            if not store:
                return Title("Not found"), Main(cls="container")(empty_state("🔍", "Store not found", "This store doesn't exist or has been deleted.", cta_text="← All Stores", cta_href="/wuILT"))
            _, site_url, base_url = _resolve_store_site(store, session)
            if not site_url:
                return Title("No GSC"), Main(cls="container")(empty_state("📊", "No GSC connection", "Set the store domain or link a Website to enable Google Search Console data.", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
            collection_url = f"{base_url}/{store.locale}/collection/{handle}"
            analysis = _render_gsc_analysis_modal(session, site_url, collection_url, f"Collection: {handle}", "collection")
    except Exception as e:
        return Title("Error"), Main(cls="container")(empty_state("❌", "Something went wrong", f"Error: {e}", cta_text="← Back to Store", cta_href=f"/wuILT/{store_pk}"))
    return Title(f"Collection GSC - {store.name}"), Main(cls="container")(
        page_header(
            f"📚 {handle}",
            f"Collection · {store.name}",
            back=f"/wuILT/{store_pk}",
            actions=[A("🌐 Open collection", href=collection_url, target="_blank", cls="btn btn-ghost btn-sm")],
        ),
        analysis,
    )


@rt("/wuILT/{store_pk:int}/product-schema/{pid:int}")
def wuilt_product_schema(store_pk: int, pid: int):
    try:
        with get_session() as session:
            store = session.get(WuiltStore, store_pk)
            product = session.get(WuiltProduct, pid)
            if not store or not product or product.wuilt_store_id != store_pk:
                return Div(cls="alert alert-error")(Span("Product not found"))
            website = session.get(Website, store.website_id) if store.website_id else None
            if not website and store.store_domain:
                raw = store.store_domain.replace("https://", "").replace("http://", "").strip().rstrip("/")
                for loc in ("/ar", "/en"):
                    if raw.endswith(loc):
                        raw = raw[: -len(loc)]
                        break
                website = session.exec(select(Website).where(Website.url.contains(raw))).first()
            if not website and not store.store_domain:
                return Div(cls="alert alert-warning")(Span("Set store_domain or link a Website."))
        product_url = f"{_store_base_url(store)}/{store.locale}/product/all/{product.handle}"
        validation = validate_page(product_url)
        text_content = re.sub(r"<[^>]+>", "", product.description_html or "") if product.description_html else ""
        content = f"{product.title}\n{text_content[:2000]}"
        predictions = predict_schemas(content) if content.strip() else {"suggestions": [], "reasoning": "No content"}
    except Exception as e:
        return Div(cls="alert alert-error")(Span(f"Schema check failed: {e}"))
    return Div(cls="card card-border bg-base-100 shadow-sm mb-4")(
        Div(cls="card-body")(
            Div(cls="flex items-start justify-between")(
                H3("🔍 Schema Analysis", cls="card-title text-base"),
                Span(product.title, cls="text-sm text-base-content/60"),
            ),
            H4("Validation", cls="text-sm font-semibold mt-2 mb-1"),
            Div(cls="stats shadow-sm")(
                Div(cls="stat py-2")(
                    Div("Status", cls="stat-title text-xs"),
                    Div(Span("✅ Fetched" if validation.get("fetch_status") == 200 else f"❌ HTTP {validation.get('fetch_status', 'error')}", cls="text-sm")),
                ),
                Div(cls="stat py-2")(
                    Div("Schemas", cls="stat-title text-xs"),
                    Div(str(validation["summary"]["total_schemas"]), cls="stat-value text-lg"),
                ),
                Div(cls="stat py-2")(
                    Div("Valid", cls="stat-title text-xs"),
                    Div(f"{validation['summary']['valid_count']}/{validation['summary']['total_schemas']}", cls="stat-value text-sm " + ("text-success" if validation['summary']['valid_count'] == validation['summary']['total_schemas'] else "text-warning")),
                ),
            ),
            Div(cls="mt-2 space-y-1")(
                *[Div(cls="flex items-center gap-2 text-xs")(
                    Span("✅" if s["google_supported"] else "⚠️", cls="text-sm"),
                    Span(s["type"], cls="font-medium"),
                    Span("Google Supported" if s["google_supported"] else "Unknown type", cls="text-base-content/60"),
                    Span("✓ Valid" if s["is_valid"] else "✗ Issues", cls=("text-success" if s["is_valid"] else "text-error")),
                ) for s in validation.get("schemas_found", [])],
            ) if validation.get("schemas_found") else P("No structured data found.", cls="text-xs text-warning mt-1"),
            H4("🧠 Schema Predictions", cls="text-sm font-semibold mt-3 mb-1"),
            Div(cls="flex flex-wrap gap-2")(
                *[Span(f"{s['type']}: {s['score']}%", cls="badge badge-sm " + ("badge-primary" if s['score'] >= 70 else "badge-soft")) for s in predictions.get("suggestions", [])],
            ) if predictions.get("suggestions") else P("No predictions available.", cls="text-xs text-base-content/60"),
            P(predictions.get("reasoning", ""), cls="text-xs text-base-content/50 mt-1 italic"),
            Div(cls="mt-2")(
                A("View Live Page", href=product_url, target="_blank", cls="link link-primary text-xs"),
            ),
        ),
    ) + Script("document.getElementById('wuilt-modal').showModal()")


@rt("/wuILT/{store_pk:int}/product-optimize/{pid:int}")
def wuilt_product_optimize(store_pk: int, pid: int):
    try:
        with get_session() as session:
            store = session.get(WuiltStore, store_pk)
            product = session.get(WuiltProduct, pid)
            if not store or not product or product.wuilt_store_id != store_pk:
                return Div(cls="alert alert-error")(Span("Product not found"))
        optimized = optimize_product(
            product_title=product.title,
            description_html=product.description_html or "",
            short_description=product.short_description or "",
            language=store.locale,
        )
    except Exception as e:
        return Div(cls="alert alert-error")(Span(f"DSPy generation failed: {e}"))
    return Div(
        H3(f"✨ Optimize SEO — {product.title}", cls="text-lg font-bold mb-4"),
        Form(
            Fieldset(FieldsetLegend("SEO Title"),
                Input(name="seo_title", value=optimized["seo_title"], cls="input w-full")),
            Fieldset(FieldsetLegend("SEO Description"),
                Textarea(optimized["seo_description"], name="seo_description", cls="textarea w-full", rows=3)),
            Fieldset(FieldsetLegend("Description HTML"),
                Textarea(optimized["optimized_description_html"], name="description_html", cls="textarea w-full font-mono text-xs", rows=6)),
            Fieldset(FieldsetLegend("Short Description"),
                Textarea(optimized["optimized_short_description"], name="short_description", cls="textarea w-full", rows=2)),
            Input(type="hidden", name="pid", value=str(pid)),
            Div(cls="modal-action")(
                Form(method="dialog")(Button("Cancel", cls="btn btn-ghost")),
                Button("Save & Push to Wuilt", cls="btn btn-primary", type="submit",
                       hx_post=f"/wuILT/{store_pk}/product-save-seo",
                       hx_target="#wuilt-modal-content",
                       hx_swap="innerHTML"),
            ),
            action="#", method="post",
        ),
    ) + Script("document.getElementById('wuilt-modal').showModal()")


@rt("/wuILT/{store_pk:int}/product-save-seo")
def wuilt_product_save_seo(store_pk: int, seo_title: str, seo_description: str, description_html: str, short_description: str, pid: int):
    with get_session() as session:
        store = session.get(WuiltStore, store_pk)
        product = session.get(WuiltProduct, pid)
        if not store or not product or product.wuilt_store_id != store_pk:
            return Div(cls="alert alert-error")(Span("Product not found"))
    client = WuiltClient(api_key=store.api_key, store_id=store.store_id, locale=store.locale)
    seo_ok = False; seo_err = ""
    desc_ok = False; desc_err = ""
    try:
        client.update_product_seo(
            product_id=product.wuilt_product_id,
            seo_title=seo_title,
            seo_description=seo_description,
            title=product.title,
            handle=product.handle,
        )
        seo_ok = True
    except Exception as e:
        seo_err = str(e)
    try:
        client.update_product_seo(
            product_id=product.wuilt_product_id,
            description_html=description_html,
            short_description=short_description,
            title=product.title,
            handle=product.handle,
        )
        desc_ok = True
    except Exception as e:
        desc_err = str(e)
    with get_session() as session:
        p = session.get(WuiltProduct, pid)
        if p and seo_ok:
            p.optimized_seo_title = seo_title
            p.optimized_seo_description = seo_description
            p.optimized_description_html = description_html
            p.last_optimized_at = datetime.now()
            session.commit()
    status = []
    if seo_ok: status.append("✅ SEO title & description")
    else: status.append(f"❌ SEO fields failed: {seo_err}")
    if desc_ok: status.append("✅ Description & short desc")
    else: status.append(f"⚠️ Description skipped: {desc_err}")
    return Div(cls="card card-border bg-base-100 shadow-sm")(
        Div(cls="card-body")(
            Div(cls="flex items-center gap-2")(
                Span("✨" if seo_ok else "❌", cls="text-lg"),
                H3(product.title, cls="card-title text-base"),
            ),
            Ul(*[Li(s, cls="text-xs") for s in status], cls="list-disc list-inside mt-2"),
        ),
    )


@rt("/wuILT/{store_pk:int}/sync")
def wuilt_sync_products(store_pk: int):
    with get_session() as session:
        store = session.get(WuiltStore, store_pk)
        if not store:
            return Titled("Store not found", P("Store not found"))
    result = sync_wuilt_products(store.api_key, store.store_id, store.name, store.locale)
    return Title(f"Sync Complete - {store.name}"), Main(cls="container")(
        A("← Back to Store", href=f"/wuILT/{store_pk}", cls="link link-primary mb-4"),
        H1("Product Sync Complete", cls="text-2xl font-bold mb-4"),
        Div(cls="stats shadow")(
            Div(cls="stat")(
                Div("Total", cls="stat-title"),
                Div(str(result["total"]), cls="stat-value"),
            ),
            Div(cls="stat")(
                Div("Created", cls="stat-title"),
                Div(str(result["created"]), cls="stat-value text-success"),
            ),
            Div(cls="stat")(
                Div("Updated", cls="stat-title"),
                Div(str(result["updated"]), cls="stat-value text-info"),
            ),
        ),
        Div(cls="mt-4")(
            A("View Store", href=f"/wuILT/{store_pk}", cls="btn btn-primary"),
        ),
    )


@rt("/wuILT/{store_pk:int}/sync-pages")
def wuilt_sync_pages(store_pk: int):
    with get_session() as session:
        store = session.get(WuiltStore, store_pk)
        if not store:
            return Titled("Store not found", P("Store not found"))
    result = sync_wuilt_pages(store.api_key, store.store_id, store.name, store.locale)
    return Title(f"Pages Sync Complete - {store.name}"), Main(cls="container")(
        A("← Back to Store", href=f"/wuILT/{store_pk}", cls="link link-primary mb-4"),
        H1("Pages Sync Complete", cls="text-2xl font-bold mb-4"),
        Div(cls="stats shadow")(
            Div(cls="stat")(
                Div("Total", cls="stat-title"),
                Div(str(result["total"]), cls="stat-value"),
            ),
            Div(cls="stat")(
                Div("Created", cls="stat-title"),
                Div(str(result["created"]), cls="stat-value text-success"),
            ),
            Div(cls="stat")(
                Div("Updated", cls="stat-title"),
                Div(str(result["updated"]), cls="stat-value text-info"),
            ),
        ),
        Div(cls="mt-4")(
            A("View Store", href=f"/wuILT/{store_pk}", cls="btn btn-primary"),
        ),
    )


@rt("/wuILT/{store_pk:int}/optimize")
def wuilt_optimize_products(store_pk: int):
    with get_session() as session:
        store = session.get(WuiltStore, store_pk)
        if not store:
            return Titled("Store not found", P("Store not found"))

    results = batch_optimize_products(store.api_key, store.store_id, store.locale)

    with get_session() as session:
        updated = 0
        for r in results:
            if "error" in r:
                continue
            pid = r["product_id"]
            product = session.exec(
                select(WuiltProduct).where(
                    WuiltProduct.wuilt_store_id == store_pk,
                    WuiltProduct.wuilt_product_id == pid
                )
            ).first()
            if product:
                product.optimized_seo_title = r["optimized"]["seo_title"]
                product.optimized_seo_description = r["optimized"]["seo_description"]
                product.optimized_description_html = r["optimized"]["optimized_description_html"]
                product.last_optimized_at = datetime.now()
                updated += 1
        session.commit()

    errors = [r for r in results if "error" in r]
    return Title(f"Optimization Complete - {store.name}"), Main(cls="container")(
        A("← Back to Store", href=f"/wuILT/{store_pk}", cls="link link-primary mb-4"),
        H1("SEO Optimization Complete", cls="text-2xl font-bold mb-4"),
        P(f"Products processed via DSPy and pushed to Wuilt.", cls="text-base-content/60 mb-4"),
        Div(cls="stats shadow mb-6")(
            Div(cls="stat")(
                Div("Total", cls="stat-title"),
                Div(str(len(results)), cls="stat-value"),
            ),
            Div(cls="stat")(
                Div("Updated Locally", cls="stat-title"),
                Div(str(updated), cls="stat-value text-success"),
            ),
            Div(cls="stat")(
                Div("Errors", cls="stat-title"),
                Div(str(len(errors)), cls="stat-value text-error"),
            ),
        ),
        Div(cls="overflow-x-auto")(
            Table(cls="table table-zebra")(
                Thead(Tr(Th("Product"), Th("Status"), Th("New SEO Title"), Th("New SEO Desc"))),
                Tbody(*[
                    Tr(
                        Td(r.get("title", "?")),
                        Td(
                            Span("✅", cls="text-success") if "error" not in r
                            else Span(f"❌ {r['error']}", cls="text-error"),
                        ),
                        Td(r["optimized"]["seo_title"][:60] if "error" not in r else "—", cls="text-sm"),
                        Td(r["optimized"]["seo_description"][:80] if "error" not in r else "—", cls="text-sm text-base-content/60"),
                    ) for r in results
                ]),
            ),
        ),
        Div(cls="mt-4")(
            A("View Store", href=f"/wuILT/{store_pk}", cls="btn btn-primary"),
        ),
    )


@rt("/wuILT/{store_pk:int}/edit")
def wuilt_edit_form(store_pk: int):
    with get_session() as session:
        store = session.get(WuiltStore, store_pk)
        if not store:
            return Titled("Store not found", P("Store not found"))
    return Title(f"Edit {store.name}"), Main(cls="container")(
        A("← Back to Store", href=f"/wuILT/{store_pk}", cls="link link-primary mb-4"),
        H1(f"Edit: {store.name}", cls="text-2xl font-bold mb-4"),
        Form(method="post", action=f"/wuILT/{store_pk}/edit/save", cls="max-w-lg")(
            Fieldset(FieldsetLegend("Store Name"),
                Input(name="name", value=store.name, required=True, cls="input w-full")),
            Fieldset(FieldsetLegend("Store ID"),
                Input(name="store_id", value=store.store_id, required=True, cls="input w-full")),
            Fieldset(FieldsetLegend("API Key"),
                Input(name="api_key", type="password", placeholder="Leave blank to keep current", cls="input w-full"),
                P("Leave blank to keep current API key.", cls="text-xs text-base-content/60 mt-1")),
            Fieldset(FieldsetLegend("Locale"),
                Select(
                    Option("Arabic", value="ar", selected=(store.locale == "ar")),
                    Option("English", value="en", selected=(store.locale == "en")),
                    name="locale", cls="select w-full")),
            Fieldset(FieldsetLegend("Store Domain"),
                Input(name="store_domain", value=store.store_domain, placeholder="mystore.com", cls="input w-full")),
            Div(cls="mt-4 flex gap-2")(
                A("Cancel", href=f"/wuILT/{store_pk}", cls="btn btn-ghost"),
                Btn("Save Changes", cls="-primary", type="submit"),
            ),
        ),
    )


@rt("/wuILT/{store_pk:int}/edit/save")
def wuilt_edit_store(store_pk: int, name: str, store_id: str, api_key: str = "", locale: str = "ar", store_domain: str = ""):
    with get_session() as session:
        store = session.get(WuiltStore, store_pk)
        if not store:
            return Titled("Store not found", P("Store not found"))
        new_key = api_key if api_key else store.api_key
        add_or_update_wuilt_store(session, store_id=store_id, name=name, api_key=new_key, locale=locale, store_domain=store_domain, store_pk=store_pk)
    return RedirectResponse(f"/wuILT/{store_pk}", status_code=303)


@rt("/wuILT/{store_pk:int}/delete")
def wuilt_delete_store(store_pk: int):
    with get_session() as session:
        delete_wuilt_store(session, store_pk)
    return RedirectResponse("/wuILT", status_code=303)


import os; serve(port=5003, reload=os.getenv("FASTHTML_RELOAD", "").lower() in ("1", "true", "yes"))

# Trigger reload
