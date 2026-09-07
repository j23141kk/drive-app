import os
import re
import requests
from pathlib import Path
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

app = FastAPI()

# HTMLからの通信(CORS)を許可
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# --------------------------------------------------
# 逆ジオコーディング（緯度経度から住所を取得）
# --------------------------------------------------
def reverse_geocode(lat: float, lng: float) -> str:
    """OpenStreetMapのNominatim APIを使って緯度経度から住所名・ランドマーク名を取得する"""
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lng}&zoom=18&addressdetails=1"
        headers = {"User-Agent": "LandscapeTranslationApp/1.0"}
        res = requests.get(url, headers=headers, timeout=3)
        if res.status_code == 200:
            data = res.json()
            display_name = data.get("display_name", "")
            address = data.get("address", {})
            
            # 都道府県、市区町村、町名などを抽出
            province = address.get("province", address.get("state", ""))
            city = address.get("city", address.get("town", address.get("village", address.get("suburb", ""))))
            suburb = address.get("suburb", address.get("neighbourhood", ""))
            road = address.get("road", "")
            amenity = address.get("amenity", address.get("building", ""))

            location_str = f"{province}{city}{suburb}{road} {amenity}".strip()
            print(f"[Reverse Geocode] 緯度:{lat}, 経度:{lng} -> {location_str}")
            return location_str if location_str else display_name
    except Exception as e:
        print(f"逆ジオコーディング失敗: {e}")
    return f"緯度 {lat}, 経度 {lng}"

# --------------------------------------------------
# 階層構造対応のWebクローラー処理
# --------------------------------------------------
RAG_DIR = Path(__file__).parent / "rag_files"
URLS_FILE = RAG_DIR / "urls.txt"
RAG_DIR.mkdir(exist_ok=True)

uploaded_files = []

def crawl_and_extract_text(start_url: str, max_depth: int = 1) -> str:
    """指定したURLから下層ページも自動巡回して本文テキストを抽出する"""
    visited = set()
    to_visit = {start_url}
    base_domain = urlparse(start_url).netloc
    all_pages_text = []

    for depth in range(max_depth + 1):
        next_visit = set()
        for url in to_visit:
            if url in visited:
                continue
            visited.add(url)

            try:
                print(f"[Crawl Depth {depth}] 取得中: {url}")
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

# --------------------------------------------------
# RAG用ファイルの自動取得・アップロード処理
# --------------------------------------------------
if URLS_FILE.exists():
    with open(URLS_FILE, "r", encoding="utf-8") as f:
        urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]

    for i, url in enumerate(urls):
        site_corpus = crawl_and_extract_text(url, max_depth=1)
        
        if site_corpus:
            downloaded_file_path = RAG_DIR / f"crawled_site_{i}.txt"
            with open(downloaded_file_path, "w", encoding="utf-8") as out:
                out.write(site_corpus)

            ref = client.files.upload(file=str(downloaded_file_path))
            uploaded_files.append(ref)
            print(f"RAGアップロード完了: {url} -> {ref.name}")

# ローカルにあるPDFやTXTもアップロード
for file_path in RAG_DIR.glob("*.*"):
    if file_path.suffix in [".pdf", ".txt"] and not file_path.name.startswith("crawled_site_") and file_path.name != "urls.txt":
        try:
            ref = client.files.upload(file=str(file_path))
            uploaded_files.append(ref)
            print(f"ローカルファイルをアップロード完了: {file_path.name}")
        except Exception as e:
            print(f"ファイルアップロード失敗 ({file_path.name}): {e}")

# --------------------------------------------------
# API リクエスト受け取り処理
# --------------------------------------------------
class LocationRequest(BaseModel):
    lat: float
    lng: float

@app.post("/api/translate-landscape")
def get_landscape_translation(req: LocationRequest):
    # 1. 緯度経度からリアルタイムな住所名を取得
    location_name = reverse_geocode(req.lat, req.lng)

    # 2. 厳格なプロンプトを作成して現在地のみの解説を行わせる
    prompt = (
        f"【現在地情報】: 「{location_name}」（座標: 緯度 {req.lat}, 経度 {req.lng}）\n\n"
        f"【指示】:\n"
        f"1. 対象地点は「{location_name}」のピンポイントな現在地周辺（目視できる範囲）です。\n"
        f"2. 【厳禁】: 「{location_name}」と直接関係のない遠くの地域の話題は絶対に一切出さないでください。\n"
        f"3. 添付された資料から「{location_name}」またはその直近の地域に関連する歴史、文化、地形、見どころの情報を抽出してください。\n"
        f"4. 資料に直接の記述がない場合でも、現在地の地理・地域特性をもとに、ドライブ中の人に車窓の風景を語りかけるような100〜150文字程度の魅力的で自然な解説文を作成してください。"
        f"5.本システムは1人でドライブしているときに使用することを想定しているため、ドライブ中の人に語りかけるときには、皆さんなどの複数の人に語りかける表現は避けてください。"
    )

    contents = [*uploaded_files, prompt]

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents
    )
    
    return {"translation": response.text}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)