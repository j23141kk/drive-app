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

                # 不要要素（ヘッダー、フッター、ナビ等）の削除
                for element in soup(["script", "style", "header", "footer", "nav", "aside", "iframe"]):
                    element.extract()

                # メインコンテンツの抽出
                main_content = soup.find("main") or soup.find("article") or soup.body
                if main_content:
                    text = main_content.get_text(separator="\n")
                    clean_text = re.sub(r'\n\s*\n', '\n', text)
                    all_pages_text.append(f"--- SOURCE URL: {url} ---\n{clean_text}\n")

                # 下層ページのリンクを収集
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

            # Gemini APIへアップロード（オブジェクト自体をリストに追加）
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
    spot_name: str
    lat: float
    lng: float

@app.post("/api/translate-landscape")
def get_landscape_translation(req: LocationRequest):
    prompt = (
        f"現在地は「{req.spot_name}」（緯度: {req.lat}, 経度: {req.lng}）付近です。\n"
        f"この場所の歴史、文化、または見どころについて添付された資料を参照し、"
        f"ドライブ中の人に語りかけるような150文字程度のわかりやすく魅力的な解説文を作ってください。"
    )

    # アップロードしたファイル群とプロンプトを一緒にcontentsに渡す
    contents = [*uploaded_files, prompt]

    response = client.models.generate_content(
        model="gemini-2.5-flash",
        contents=contents
    )
    
    return {"translation": response.text}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)