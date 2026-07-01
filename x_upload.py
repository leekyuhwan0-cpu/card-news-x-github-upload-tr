"""
X (Twitter) 자동 업로드
- Drive 폴더에서 PNG + TXT 세트 랜덤 선택
- 이미지로 메인 트윗 게시
- 업로드 성공 시 Drive에서 파일 삭제
"""

import sys
import random
import tempfile
import tweepy
import requests
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import io

import os
import fal_client
from x_config import ACCOUNTS, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET, GOOGLE_REFRESH_TOKEN, FAL_KEY

os.environ["FAL_KEY"] = FAL_KEY


# ── 텍스트 요약 (gpt-4o-mini) ────────────────────────────
def summarize_text(text, max_chars=200):
    result = fal_client.run(
        "fal-ai/any-llm",
        arguments={
            "model": "openai/gpt-4o-mini",
            "prompt": f"Summarize the following text in the same language, within {max_chars} characters. Output only the summary, nothing else:\n\n{text}"
        }
    )
    return result.get("output", text[:max_chars])


# ── Google Drive 인증 ─────────────────────────────────────
def get_drive_service():
    creds = Credentials(
        token=None,
        refresh_token=GOOGLE_REFRESH_TOKEN,
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        token_uri="https://oauth2.googleapis.com/token"
    )
    return build("drive", "v3", credentials=creds)


# ── Drive 폴더 스캔 ───────────────────────────────────────
def scan_drive_folder(folder_id):
    import re
    service = get_drive_service()
    results = service.files().list(
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id, name, mimeType)",
        pageSize=1000,
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()

    files = results.get("files", [])
    groups = {}

    for f in files:
        name = f["name"]
        file_id = f["id"]

        # 10-1.png / 10-2.png (다중 이미지)
        m = re.match(r'^(\d+)-(\d+)\.png$', name)
        if m:
            base = int(m.group(1))
            idx = int(m.group(2))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "png", "idx": idx})
            continue

        # keyword-1.png / keyword-2.png
        m = re.match(r'^(.+)-(\d+)\.png$', name)
        if m:
            base_key = m.group(1)
            idx = int(m.group(2))
            groups.setdefault(base_key, [])
            groups[base_key].append({"id": file_id, "name": name, "type": "png", "idx": idx})
            continue

        # 10.png
        m = re.match(r'^(\d+)\.png$', name)
        if m:
            base = int(m.group(1))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "png", "idx": 0})
            continue

        # keyword.png
        m = re.match(r'^(.+)\.png$', name)
        if m and not name.startswith('_'):
            base_key = m.group(1)
            groups.setdefault(base_key, [])
            groups[base_key].append({"id": file_id, "name": name, "type": "png", "idx": 0})
            continue

        # mp4 영상
        m = re.match(r'^(\d+)영상\.mp4$', name)
        if m:
            base = int(m.group(1))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "video", "idx": 0})
            continue

        m = re.match(r'^(\d+)\.mp4$', name)
        if m:
            base = int(m.group(1))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "video", "idx": 0})
            continue

        m = re.match(r'^(.+)\.mp4$', name)
        if m and not name.startswith('_'):
            base_key = m.group(1)
            groups.setdefault(base_key, [])
            groups[base_key].append({"id": file_id, "name": name, "type": "video", "idx": 0})
            continue

        # txt 파일
        m = re.match(r'^(\d+)\.txt$', name)
        if m:
            base = int(m.group(1))
            groups.setdefault(base, [])
            groups[base].append({"id": file_id, "name": name, "type": "txt", "idx": 0})
            continue

        m = re.match(r'^(.+)\.txt$', name)
        if m and not name.startswith('_'):
            base_key = m.group(1)
            groups.setdefault(base_key, [])
            groups[base_key].append({"id": file_id, "name": name, "type": "txt", "idx": 0})
            continue

    # 이미지 정렬 (idx 기준)
    for base in groups:
        groups[base].sort(key=lambda x: (x["type"] != "png", x["idx"]))

    return groups


# ── Drive 파일 다운로드 ───────────────────────────────────
def download_from_drive(file_id, filename, tmp_dir):
    import os
    service = get_drive_service()
    request = service.files().get_media(fileId=file_id, supportsAllDrives=True)
    fpath = os.path.join(tmp_dir, filename)
    with open(fpath, "wb") as f:
        downloader = MediaIoBaseDownload(f, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return fpath


# ── Drive 파일 삭제 ───────────────────────────────────────
def delete_from_drive(file_id, filename):
    service = get_drive_service()
    service.files().delete(fileId=file_id, supportsAllDrives=True).execute()
    print(f"  Drive 삭제: {filename}")


# ── Tweepy 클라이언트 ─────────────────────────────────────
def get_tweepy_client(cfg):
    return tweepy.Client(
        consumer_key=cfg["api_key"],
        consumer_secret=cfg["api_secret"],
        access_token=cfg["access_token"],
        access_token_secret=cfg["access_token_secret"],
    )

def get_tweepy_api(cfg):
    auth = tweepy.OAuth1UserHandler(
        cfg["api_key"], cfg["api_secret"],
        cfg["access_token"], cfg["access_token_secret"]
    )
    return tweepy.API(auth)


# ── 메인 업로드 함수 ──────────────────────────────────────
def post_group(lang, base, file_items):
    cfg = ACCOUNTS[lang]
    client = get_tweepy_client(cfg)
    api = get_tweepy_api(cfg)

    png_items   = [f for f in file_items if f["type"] == "png"]
    video_items = [f for f in file_items if f["type"] == "video"]
    txt_items   = [f for f in file_items if f["type"] == "txt"]

    if not png_items and not video_items:
        print(f"  [오류] 미디어 없음")
        return False

    print(f"\n[{lang}] {base} 업로드 시작 (영상:{len(video_items)} 이미지:{len(png_items)})")

    with tempfile.TemporaryDirectory() as tmp_dir:
        media_ids = []

        if video_items:
            vid_item = video_items[0]
            vid_path = download_from_drive(vid_item["id"], vid_item["name"], tmp_dir)
            media = api.media_upload(vid_path, media_category="tweet_video")
            media_ids.append(media.media_id)
            print(f"  영상 업로드 완료: {vid_item['name']} (ID: {media.media_id})")
        else:
            for img_item in png_items[:4]:
                img_path = download_from_drive(img_item["id"], img_item["name"], tmp_dir)
                media = api.media_upload(img_path)
                media_ids.append(media.media_id)
                print(f"  이미지 업로드: {img_item['name']} (ID: {media.media_id})")

        # 본문 읽기 & 요약
        caption = ""
        if txt_items:
            txt_path = download_from_drive(txt_items[0]["id"], txt_items[0]["name"], tmp_dir)
            raw_text = open(txt_path, encoding="utf-8").read().strip()
            if len(raw_text) > 200:
                print(f"  텍스트 요약 중...")
                caption = summarize_text(raw_text, max_chars=200)
            else:
                caption = raw_text
            if len(caption) > 280:
                caption = caption[:277] + "..."
            print(f"  캡션 길이: {len(caption)}자")

        # 트윗 게시
        main_tweet = client.create_tweet(text=caption, media_ids=media_ids)
        main_id = main_tweet.data["id"]
        print(f"  트윗 ID: {main_id}")

    # 업로드 성공 → Drive 파일 삭제
    for item in file_items:
        try:
            delete_from_drive(item["id"], item["name"])
        except Exception as e:
            print(f"  Drive 삭제 실패 ({item['name']}): {e}")

    print(f"  [{lang}] {base} 업로드 완료!")
    return True


# ── 단건 업로드 ───────────────────────────────────────────
def post_one(lang):
    folder_id = ACCOUNTS[lang]["drive_folder_id"]
    groups = scan_drive_folder(folder_id)

    available = [
        b for b, items in groups.items()
        if any(f["type"] == "png" for f in items)
    ]

    if not available:
        print(f"[{lang}] 업로드 가능한 파일 없음")
        return

    print(f"[{lang}] 업로드 가능: {len(available)}개")
    base = random.choice(available)
    post_group(lang, base, groups[base])


if __name__ == "__main__":
    lang = sys.argv[1] if len(sys.argv) > 1 else "tr"
    post_one(lang)
