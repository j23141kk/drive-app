import os
import re
import json
import hashlib
import base64
import secrets
import shutil
import threading
from time import monotonic, sleep
import requests
from pathlib import Path
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()
INDEX_FILE = Path(__file__).parent / "index.html"
PROJECT_RAG_DIR = Path(__file__).parent / "rag_files"
RAG_DIR = Path(os.environ.get("RAG_DATA_DIR", PROJECT_RAG_DIR))
RAG_DIR.mkdir(parents=True, exist_ok=True)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sync_rag_seed_files():
    if RAG_DIR.resolve() == PROJECT_RAG_DIR.resolve():
        return

    manifest_path = RAG_DIR / ".seed-manifest.json"
    try:
        previous_hashes = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        previous_hashes = {}
    current_hashes = dict(previous_hashes)

    for source_file in PROJECT_RAG_DIR.glob("*.*"):
        if not source_file.is_file():
            continue
        destination = RAG_DIR / source_file.name
        source_hash = file_sha256(source_file)
        if not destination.exists():
            shutil.copy2(source_file, destination)
            current_hashes[source_file.name] = source_hash
        elif source_file.name in previous_hashes and file_sha256(destination) == previous_hashes[source_file.name]:
            if source_hash != previous_hashes[source_file.name]:
                shutil.copy2(source_file, destination)
            current_hashes[source_file.name] = source_hash
        elif source_file.name not in previous_hashes and file_sha256(destination) == source_hash:
            current_hashes[source_file.name] = source_hash

    if current_hashes != previous_hashes:
        temporary_manifest = manifest_path.with_suffix(".tmp")
        temporary_manifest.write_text(json.dumps(current_hashes, indent=2), encoding="utf-8")
        temporary_manifest.replace(manifest_path)


sync_rag_seed_files()
URLS_FILE = RAG_DIR / "urls.txt"
app.mount("/icons", StaticFiles(directory=Path(__file__).parent / "icons"), name="icons")

client = None
rag_ready = False
rag_error = False
_rag_lock = threading.Lock()
_geocode_lock = threading.Lock()
_geocode_cache = {}
_last_geocode_request = 0.0


@app.middleware("http")
async def require_basic_auth(request: Request, call_next):
    if request.url.path == "/healthz":
        return await call_next(request)

    expected_username = os.environ.get("APP_USERNAME")
    expected_password = os.environ.get("APP_PASSWORD")
    if not expected_username or not expected_password:
        if os.environ.get("RENDER"):
            return JSONResponse(status_code=503, content={"detail": "APP_USERNAME と APP_PASSWORD を設定してください。"})
        return await call_next(request)

    authorization = request.headers.get("Authorization", "")
    scheme = ""
    try:
        scheme, encoded = authorization.split(" ", 1)
        username, password = base64.b64decode(encoded, validate=True).decode("utf-8").split(":", 1)
    except (ValueError, UnicodeDecodeError):
        username = password = ""

    valid_username = secrets.compare_digest(username.encode("utf-8"), expected_username.encode("utf-8"))
    valid_password = secrets.compare_digest(password.encode("utf-8"), expected_password.encode("utf-8"))
    if scheme.lower() != "basic" or not (valid_username and valid_password):
        return JSONResponse(
            status_code=401,
            content={"detail": "認証が必要です。"},
            headers={"WWW-Authenticate": 'Basic realm="Landscape Translation System", charset="UTF-8"'},
        )
    return await call_next(request)

# --------------------------------------------------
# 逆ジオコーディング（住所取得）
# --------------------------------------------------
def reverse_geocode(lat: float, lng: float) -> str:
    global _last_geocode_request
    rounded_lat, rounded_lng = round(lat, 3), round(lng, 3)
    cache_key = (rounded_lat, rounded_lng)

    with _geocode_lock:
        if cache_key in _geocode_cache:
            return _geocode_cache[cache_key]
        wait = 1.0 - (monotonic() - _last_geocode_request)
        if wait > 0:
            sleep(wait)
        _last_geocode_request = monotonic()

        try:
            url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={rounded_lat}&lon={rounded_lng}&zoom=18&addressdetails=1"
            headers = {"User-Agent": "LandscapeTranslationApp/1.0"}
            res = requests.get(url, headers=headers, timeout=3)
            if res.status_code == 200:
                data = res.json()
                address = data.get("address", {})
                province = address.get("province", address.get("state", ""))
                city = address.get("city", address.get("town", address.get("village", address.get("suburb", ""))))
                suburb = address.get("suburb", address.get("neighbourhood", ""))
                road = address.get("road", "")
                amenity = address.get("amenity", address.get("building", ""))
                location_str = f"{province}{city}{suburb}{road} {amenity}".strip()
                result = location_str if location_str else data.get("display_name", "")
                if result:
                    if len(_geocode_cache) >= 2048:
                        _geocode_cache.pop(next(iter(_geocode_cache)))
                    _geocode_cache[cache_key] = result
                    return result
        except Exception as e:
            print(f"逆ジオコーディング失敗: {type(e).__name__}")
    return f"緯度 {lat}, 経度 {lng}"

# --------------------------------------------------
# RAGファイルの読み込み・アップロード処理
# --------------------------------------------------
uploaded_files = []

def crawl_and_extract_text(start_url: str, max_depth: int = 1) -> str:
    visited = set()
    to_visit = {start_url}
    base_domain = urlparse(start_url).netloc
    all_pages_text = []

    for depth in range(max_depth + 1):
        next_visit = set()
        for url in to_visit:
            if url in visited: continue
            visited.add(url)
            try:
                res = requests.get(url, timeout=5)
                res.encoding = res.apparent_encoding
                soup = BeautifulSoup(res.text, "html.parser")
                for element in soup(["script", "style", "header", "footer", "nav", "aside", "iframe"]):
                    element.extract()
                main_content = soup.find("main") or soup.find("article") or soup.body
                if main_content:
                    text = main_content.get_text(separator="\n")
                    clean_text = re.sub(r'\n\s*\n', '\n', text)
                    all_pages_text.append(f"--- SOURCE URL: {url} ---\n{clean_text}\n")
                if depth < max_depth:
                    for a_tag in soup.find_all("a", href=True):
                        link = urljoin(url, a_tag["href"])
                        if urlparse(link).netloc == base_domain and link.startswith("http"):
                            clean_link = link.split('#')[0]
                            if clean_link not in visited:
                                next_visit.add(clean_link)
            except Exception as e:
                print(f"取得失敗 ({url}): {e}")
        to_visit = next_visit
    return "\n\n".join(all_pages_text)

def initialize_rag(refresh_crawl: bool = True) -> bool:
    global client, rag_ready, rag_error
    initialization_started_at = monotonic()
    phase = "refresh" if refresh_crawl else "bundled"
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(f"RAG_INIT phase={phase} status=missing_api_key")
        return False

    try:
        rag_client = client or genai.Client(api_key=api_key)
        new_uploaded_files = []
        if refresh_crawl and URLS_FILE.exists():
            with open(URLS_FILE, "r", encoding="utf-8") as f:
                urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

            for i, url in enumerate(urls):
                crawl_started_at = monotonic()
                site_corpus = crawl_and_extract_text(url, max_depth=1)
                crawl_ms = (monotonic() - crawl_started_at) * 1000
                if site_corpus:
                    downloaded_file_path = RAG_DIR / f"crawled_site_{i}.txt"
                    with open(downloaded_file_path, "w", encoding="utf-8") as out:
                        out.write(site_corpus)
                    upload_started_at = monotonic()
                    ref = rag_client.files.upload(file=str(downloaded_file_path))
                    new_uploaded_files.append(ref)
                    print(
                        f"RAG_SOURCE source={i + 1}/{len(urls)} status=updated "
                        f"crawl_ms={crawl_ms:.0f} "
                        f"upload_ms={(monotonic() - upload_started_at) * 1000:.0f}"
                    )
                else:
                    # A failed live crawl must not discard the bundled/previous snapshot.
                    cached_path = RAG_DIR / f"crawled_site_{i}.txt"
                    if cached_path.exists():
                        upload_started_at = monotonic()
                        ref = rag_client.files.upload(file=str(cached_path))
                        new_uploaded_files.append(ref)
                        upload_ms = (monotonic() - upload_started_at) * 1000
                    else:
                        upload_ms = 0
                    print(
                        f"RAG_SOURCE source={i + 1}/{len(urls)} status=using_cached "
                        f"crawl_ms={crawl_ms:.0f} upload_ms={upload_ms:.0f}"
                    )

        for file_path in RAG_DIR.glob("*.*"):
            should_upload = file_path.suffix in [".pdf", ".txt"] and file_path.name != "urls.txt"
            if refresh_crawl and file_path.name.startswith("crawled_site_"):
                should_upload = False
            if should_upload:
                try:
                    upload_started_at = monotonic()
                    ref = rag_client.files.upload(file=str(file_path))
                    new_uploaded_files.append(ref)
                    print(
                        f"RAG_UPLOAD phase={phase} status=ok "
                        f"elapsed_ms={(monotonic() - upload_started_at) * 1000:.0f}"
                    )
                except Exception as e:
                    print(f"RAG file upload failed ({file_path.name}): {type(e).__name__}")
                    raise
        if not new_uploaded_files:
            raise RuntimeError("No RAG files were uploaded")
        with _rag_lock:
            client = rag_client
            uploaded_files[:] = new_uploaded_files
            rag_ready = True
            rag_error = False
        print(
            f"RAG_INIT phase={phase} status=ready files={len(new_uploaded_files)} "
            f"elapsed_ms={(monotonic() - initialization_started_at) * 1000:.0f}"
        )
        return True
    except Exception as e:
        with _rag_lock:
            # Keep serving the last complete snapshot if a refresh fails.
            if not rag_ready:
                rag_error = True
        print(
            f"RAG_INIT phase={phase} status=failed error={type(e).__name__} "
            f"still_ready={rag_ready} "
            f"elapsed_ms={(monotonic() - initialization_started_at) * 1000:.0f}"
        )
        return False


def rag_refresh_loop():
    needs_initial_crawl = True
    while True:
        if not rag_ready:
            # Upload the bundled snapshot first so the app can answer while the
            # initial live crawl and its uploads run in the background thread.
            if not initialize_rag(refresh_crawl=False):
                sleep(60)
                continue

        refreshed = initialize_rag(refresh_crawl=needs_initial_crawl)
        if refreshed:
            needs_initial_crawl = False
            sleep(24 * 60 * 60)
        else:
            sleep(60 * 60)


@app.on_event("startup")
def on_startup():
    threading.Thread(target=rag_refresh_loop, daemon=True).start()

# --------------------------------------------------
# API リクエスト受取
# --------------------------------------------------
class LocationRequest(BaseModel):
    lat: float
    lng: float

@app.get("/", include_in_schema=False)
def serve_index():
    return FileResponse(INDEX_FILE)

@app.get("/manifest.webmanifest", include_in_schema=False)
def serve_manifest():
    return FileResponse(Path(__file__).parent / "manifest.webmanifest", media_type="application/manifest+json")

@app.get("/healthz", include_in_schema=False)
def health_check():
    return {"status": "ok"}

@app.post("/api/translate-landscape")
def get_landscape_translation(req: LocationRequest):
    request_started_at = monotonic()
    if not (-90 <= req.lat <= 90 and -180 <= req.lng <= 180):
        raise HTTPException(status_code=422, detail="Invalid coordinates")
    if not os.environ.get("GEMINI_API_KEY"):
        raise HTTPException(
            status_code=503,
            detail="サーバー設定エラー: Gemini APIキーが登録されていません。管理者がGEMINI_API_KEYを設定してください。",
        )
    with _rag_lock:
        rag_client = client
        rag_files = list(uploaded_files)
        ready = rag_ready
        has_rag_error = rag_error
    if has_rag_error:
        raise HTTPException(status_code=503, detail="資料の読み込みに失敗しました。サービス管理者へお問い合わせください。")
    if not ready or rag_client is None:
        raise HTTPException(status_code=503, detail="資料を読み込んでいます。しばらくしてから再度お試しください。")
    geocode_started_at = monotonic()
    location_name = reverse_geocode(req.lat, req.lng)
    geocode_ms = (monotonic() - geocode_started_at) * 1000

    prompt = (
        f"【現在地情報】: 「{location_name}」（座標: 緯度 {req.lat}, 経度 {req.lng}）\n\n"
        f"添付されたRAG資料を参照し、以下のフォーマットのJSON形式のみで回答してください（余計な解説文やバックトラック記号 ```json 等は含めないでください）。\n\n"
        f"{{\n"
        f'  "translation": "（ドライブ中の人に対する100〜150文字程度の車窓解説文。現在地周辺に直接関係のある話題のみ）",\n'
        f'  "nearby_spots": [\n'
        f'    {{"name": "（資料から検出された周辺スポット名1）", "category": "（カテゴリ名：例 史跡、自然、観光施設など）"}},\n'
        f'    {{"name": "（周辺スポット名2）", "category": "（カテゴリ名）"}}\n'
        f'  ]\n'
        f"}}\n\n"
        f"【注意事項】:\n"
        f"1. 対象地点は「{location_name}」周辺です。関係のない遠い地域の情報は除外してください。\n"
        f"2. 語りかけは1人のドライバー向けにし、「皆さん」などの複数形の表現は避けてください。\n"
        f"3. RAG資料から現在地近くの関連スポットが抽出できない場合は、nearby_spots を空配列 [] にしてください。"
    )

    contents = [*rag_files, prompt]

    try:
        generation_started_at = monotonic()
        response = rag_client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents
        )
        generation_ms = (monotonic() - generation_started_at) * 1000
        
        # JSONレスポンスのパース処理
        res_text = response.text.strip()
        # ```json 〜 ``` の囲みがあれば除去
        if res_text.startswith("```"):
            res_text = re.sub(r"^```(?:json)?\n|\n```$", "", res_text, flags=re.MULTILINE)
            
        data = json.loads(res_text)
        print(
            "LANDSCAPE_TIMING "
            f"geocode_ms={geocode_ms:.0f} "
            f"gemini_ms={generation_ms:.0f} "
            f"server_total_ms={(monotonic() - request_started_at) * 1000:.0f} "
            f"rag_file_count={len(rag_files)}"
        )
        return {
            "translation": data.get("translation", ""),
            "nearby_spots": data.get("nearby_spots", [])
        }
        
    except Exception as e:
        print(
            f"Gemini API / JSON Parse Error: {type(e).__name__}; "
            f"geocode_ms={geocode_ms:.0f}; "
            f"gemini_ms={(monotonic() - generation_started_at) * 1000:.0f}; "
            f"server_total_ms={(monotonic() - request_started_at) * 1000:.0f}"
        )
        return {
            "translation": "現在地の解説を取得できませんでした。",
            "nearby_spots": []
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")))
