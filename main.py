import os
import re
import json
import requests
from pathlib import Path
from urllib.parse import urlparse, urljoin
from bs4 import BeautifulSoup
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from google import genai
from dotenv import load_dotenv

load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

# --------------------------------------------------
# 逆ジオコーディング（住所取得）
# --------------------------------------------------
def reverse_geocode(lat: float, lng: float) -> str:
    try:
        url = f"https://nominatim.openstreetmap.org/reverse?format=json&lat={lat}&lon={lng}&zoom=18&addressdetails=1"
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
            return location_str if location_str else data.get("display_name", "")
    except Exception as e:
        print(f"逆ジオコーディング失敗: {e}")
    return f"緯度 {lat}, 経度 {lng}"

# --------------------------------------------------
# RAGファイルの読み込み・アップロード処理
# --------------------------------------------------
RAG_DIR = Path(__file__).parent / "rag_files"
URLS_FILE = RAG_DIR / "urls.txt"
RAG_DIR.mkdir(exist_ok=True)

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

for file_path in RAG_DIR.glob("*.*"):
    if file_path.suffix in [".pdf", ".txt"] and not file_path.name.startswith("crawled_site_") and file_path.name != "urls.txt":
        try:
            ref = client.files.upload(file=str(file_path))
            uploaded_files.append(ref)
        except Exception as e:
            print(f"ファイルアップロード失敗 ({file_path.name}): {e}")

# --------------------------------------------------
# API リクエスト受取
# --------------------------------------------------
class LocationRequest(BaseModel):
    lat: float
    lng: float

@app.post("/api/translate-landscape")
def get_landscape_translation(req: LocationRequest):
    location_name = reverse_geocode(req.lat, req.lng)

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

    contents = [*uploaded_files, prompt]

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=contents
        )
        
        # JSONレスポンスのパース処理
        res_text = response.text.strip()
        # ```json 〜 ``` の囲みがあれば除去
        if res_text.startswith("```"):
            res_text = re.sub(r"^```(?:json)?\n|\n```$", "", res_text, flags=re.MULTILINE)
            
        data = json.loads(res_text)
        return {
            "translation": data.get("translation", ""),
            "nearby_spots": data.get("nearby_spots", [])
        }
        
    except Exception as e:
        print(f"Gemini API / JSON Parse Error: {e}")
        return {
            "translation": "現在地の解説を取得できませんでした。",
            "nearby_spots": []
        }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)