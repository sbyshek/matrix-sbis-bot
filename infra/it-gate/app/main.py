from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from pathlib import Path
from ipaddress import ip_address, ip_network
import json
import markdown
import re

app = FastAPI(title="IT Services Portal", docs_url=None, redoc_url=None, openapi_url=None)
templates = Jinja2Templates(directory="templates")

SERVICES_FILE = Path("services.json")
DOCS_DIR = Path("/docs")  # смонтированный volume

TRUSTED = [ip_network("127.0.0.0/8"), ip_network("172.16.0.0/12")]


def get_client_ip(request: Request) -> str:
    peer = request.client.host
    xff = request.headers.get("X-Forwarded-For")
    if xff:
        try:
            if any(ip_address(peer) in net for net in TRUSTED):
                return xff.split(",")[0].strip()
        except ValueError:
            pass
    return peer


def is_ip_allowed(client_ip: str, allowed_ips: list) -> bool:
    if not allowed_ips:
        return True
    try:
        addr = ip_address(client_ip)
        return any(addr in ip_network(a, strict=False) for a in allowed_ips)
    except ValueError:
        return False


def load_services():
    with open(SERVICES_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def get_service_by_id(service_id: str):
    """Найти сервис по ID."""
    data = load_services()
    for cat in data["categories"]:
        for svc in cat["services"]:
            if svc["id"] == service_id:
                return svc, cat
    return None, None


def rewrite_md_links(md_text: str, service_id: str) -> str:
    """
    Преобразует относительные ссылки в MD:
    - [Текст](page.md) → [Текст](/docs/{service_id}/page)
    - ![Alt](assets/img.png) → ![Alt](/docs/{service_id}/assets/img.png)
    """
    # Ссылки на другие MD-файлы: [text](page.md)
    md_text = re.sub(
        r'\[([^\]]+)\]\(([^)]+\.md)\)',
        lambda m: f'[{m.group(1)}](/docs/{service_id}/{m.group(2).replace(".md", "")})',
        md_text
    )
    
    # Картинки: ![alt](assets/file.png)
    md_text = re.sub(
        r'!\[([^\]]*)\]\((assets/[^)]+)\)',
        lambda m: f'![{m.group(1)}](/docs/{service_id}/{m.group(2)})',
        md_text
    )
    
    return md_text


# ── Каталог сервисов ──
@app.get("/", response_class=HTMLResponse)
async def portal(request: Request):
    client_ip = get_client_ip(request)
    data = load_services()

    categories = []
    for category in data["categories"]:
        available = [s for s in category["services"]
                     if is_ip_allowed(client_ip, s.get("allowed_ips", []))]
        if available:
            categories.append({**category, "services": available})

    return templates.TemplateResponse("index.html", {
        "request": request,
        "categories": categories,
        "client_ip": client_ip
    })


# ── Страница сервиса (детальная) ──
@app.get("/service/{service_id}", response_class=HTMLResponse)
async def service_page(request: Request, service_id: str):
    client_ip = get_client_ip(request)
    service, category = get_service_by_id(service_id)

    if not service:
        return HTMLResponse("<h1>Сервис не найден</h1>", status_code=404)

    if not is_ip_allowed(client_ip, service.get("allowed_ips", [])):
        return HTMLResponse("<h1>Доступ запрещён</h1>", status_code=403)

    docs_file = DOCS_DIR / service_id / "index.md"
    has_docs = docs_file.exists()

    return templates.TemplateResponse("service.html", {
        "request": request,
        "service": service,
        "category": category,
        "client_ip": client_ip,
        "client_allowed": True,
        "has_docs": has_docs
    })


# ── Документация: главная страница сервиса ──
@app.get("/docs/{service_id}/", response_class=HTMLResponse)
@app.get("/docs/{service_id}", response_class=HTMLResponse)
async def docs_index(request: Request, service_id: str):
    return await render_doc_page(request, service_id, "index")


# ── Документация: конкретная страница ──
@app.get("/docs/{service_id}/{page}", response_class=HTMLResponse)
async def docs_page(request: Request, service_id: str, page: str):
    return await render_doc_page(request, service_id, page)


async def render_doc_page(request: Request, service_id: str, page: str):
    """Рендерит MD-страницу документации."""
    client_ip = get_client_ip(request)
    service, category = get_service_by_id(service_id)

    if not service:
        return HTMLResponse("<h1>Сервис не найден</h1>", status_code=404)

    if not is_ip_allowed(client_ip, service.get("allowed_ips", [])):
        return HTMLResponse("<h1>Доступ запрещён</h1>", status_code=403)

    md_file = DOCS_DIR / service_id / f"{page}.md"
    if not md_file.exists():
        return HTMLResponse("<h1>📄 Страница не найдена</h1>", status_code=404)

    md_text = md_file.read_text(encoding="utf-8")
    
    # Преобразуем относительные ссылки
    md_text = rewrite_md_links(md_text, service_id)
    
    # Рендерим MD → HTML
    html_content = markdown.markdown(
        md_text,
        extensions=["fenced_code", "tables", "toc", "codehilite"]
    )

    # Находим все MD-файлы для навигации
    doc_pages = []
    docs_folder = DOCS_DIR / service_id
    if docs_folder.exists():
        for f in sorted(docs_folder.glob("*.md")):
            page_name = f.stem
            title = page_name.replace("-", " ").replace("_", " ").title()
            # Читаем первый H1 из файла как заголовок
            try:
                content = f.read_text(encoding="utf-8")
                h1_match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
                if h1_match:
                    title = h1_match.group(1)
            except:
                pass
            doc_pages.append({
                "slug": page_name,
                "title": title,
                "active": page_name == page
            })

    return templates.TemplateResponse("docs.html", {
        "request": request,
        "service": service,
        "content": html_content,
        "current_page": page,
        "pages": doc_pages,
        "client_ip": client_ip
    })


# ── Статика документации (картинки, файлы) ──
@app.get("/docs/{service_id}/assets/{file_path:path}")
async def docs_asset(request: Request, service_id: str, file_path: str):
    client_ip = get_client_ip(request)
    service, _ = get_service_by_id(service_id)

    if not service:
        return HTMLResponse("Not found", status_code=404)

    if not is_ip_allowed(client_ip, service.get("allowed_ips", [])):
        return HTMLResponse("Forbidden", status_code=403)

    asset_file = DOCS_DIR / service_id / "assets" / file_path
    if not asset_file.exists() or not asset_file.is_file():
        return HTMLResponse("Not found", status_code=404)

    return FileResponse(asset_file)