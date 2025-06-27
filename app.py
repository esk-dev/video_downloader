import os
import time
import zipfile
import random
import re
import shutil
import subprocess
import glob
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed
from urllib.parse import urlparse

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# --- Selenium Imports ---
from selenium import webdriver
from selenium.common.exceptions import TimeoutException
from selenium.webdriver.chrome.options import Options as ChromeOptions
from selenium.webdriver.chrome.service import Service as ChromeService
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

# --- КОНФИГУРАЦИЯ ---

PLAYLIST_FILE = "playlists.txt"
BASE_OUTPUT_FOLDER = "downloaded_playlists"

# ВАЖНО: Вставьте сюда СВЕЖУЮ PHPSESSID куку!
SESSION_ID_COOKIE = "t8988fgjfm084ehkq6912faoe2" # ЗАМЕНИТЕ ЭТО СВЕЖИМ ЗНАЧЕНИЕМ

BASE_URL = "https://hypnotube.com"
REQUESTS_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/114.0.0.0 Safari/537.36"
}
MAX_DOWNLOAD_THREADS = 4
PERFORM_UPSCALE = False
UPSCALE_FACTOR = 2
UPSCALE_ALGORITHM = 'lanczos'
LOUDNESS_TARGET_I = -16.0
LOUDNESS_TARGET_LRA = 7.0
LOUDNESS_TARGET_TP = -1.5
AUDIO_COMPRESSOR_SETTINGS = "acompressor=threshold=-20dB:ratio=4:attack=20:release=200"
MAX_PROCESS_WORKERS = os.cpu_count() or 1

# --- КОНЕЦ КОНФИГУРАЦИИ ---

def sanitize_filename(name):
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def get_selenium_driver():
    chrome_options = ChromeOptions()
    chrome_options.add_argument("--headless")
    chrome_options.add_argument("--no-sandbox")
    chrome_options.add_argument("--disable-dev-shm-usage")
    chrome_options.add_argument("--disable-gpu")
    chrome_options.add_argument(f'user-agent={REQUESTS_HEADERS["User-Agent"]}')
    chrome_options.add_argument("window-size=1920x1080")
    chrome_options.add_experimental_option('excludeSwitches', ['enable-logging'])
    service = ChromeService(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=chrome_options)
    driver.set_page_load_timeout(60)
    return driver

def get_playlist_title_and_video_links(playlist_url):
    driver = None
    print(f"🔍 Анализ плейлиста: {playlist_url}")
    try:
        driver = get_selenium_driver()
        driver.get(playlist_url)
        WebDriverWait(driver, 30).until(EC.presence_of_element_located((By.CSS_SELECTOR, 'a[href*="/video/"]')))
        page_html = driver.page_source
        soup = BeautifulSoup(page_html, "html.parser")
        playlist_title = None
        title_tag = soup.select_one('h1.title, .playlist-title')
        if title_tag: playlist_title = title_tag.get_text(strip=True)
        if not playlist_title:
            try:
                path_parts = urlparse(playlist_url).path.strip('/').split('/')
                playlist_title = path_parts[-2].replace('-', ' ').title() if len(path_parts) > 1 else path_parts[-1].replace('-', ' ').title()
            except: pass
        if not playlist_title: playlist_title = "Untitled_Playlist_" + str(random.randint(1000, 9999))
        links, found_urls = [], set()
        video_anchors = soup.select('a[href*="/video/"]')
        for a in video_anchors:
            href = a.get("href")
            if href and "/video/" in href and not any(x in href for x in ["/playlist/", "/user/", "/channel/"]):
                full_url = href if href.startswith("http") else BASE_URL + href
                if full_url not in found_urls: links.append(full_url); found_urls.add(full_url)
        print(f"✅ Название плейлиста: '{playlist_title}'. Найдено видео: {len(links)} шт.")
        return playlist_title, links
    except Exception as e:
        print(f"❌ Ошибка при анализе плейлиста {playlist_url}: {e}")
        return None, []
    finally:
        if driver: driver.quit()

def extract_mp4_link_and_title(video_page_url):
    driver = None
    print(f"🔩 Загрузка страницы (Selenium): {video_page_url}")
    try:
        driver = get_selenium_driver()
        driver.get(BASE_URL)
        if SESSION_ID_COOKIE:
            print("   -> 🔑 Добавляем куки для аутентификации...")
            driver.add_cookie({'name': 'PHPSESSID', 'value': SESSION_ID_COOKIE, 'domain': '.hypnotube.com'})
        
        driver.get(video_page_url)
        
        try:
            auth_indicator_selector = "a[href*='logout'], .ucp-col .user-name"
            WebDriverWait(driver, 10).until(EC.presence_of_element_located((By.CSS_SELECTOR, auth_indicator_selector)))
            print("   -> ✅ Авторизация подтверждена.")
        except TimeoutException:
            print("\n" + "="*80)
            print("   -> ❌ ОШИБКА АВТОРИЗАЦИИ! Скрипт не смог подтвердить, что вы залогинены.")
            print("      Скорее всего, ваша кука PHPSESSID истекла. Пожалуйста, обновите ее.")
            print("="*80 + "\n")
            return None, None
        
        WebDriverWait(driver, 20).until(EC.presence_of_element_located((By.TAG_NAME, "video")))
        time.sleep(2)
        
        page_html = driver.page_source
        soup = BeautifulSoup(page_html, "html.parser")

        video_title = None
        title_tag_h1 = soup.select_one('h1.title, .video-title')
        if title_tag_h1: video_title = title_tag_h1.get_text(strip=True)
        if not video_title:
            title_tag_head = soup.select_one('head > title')
            if title_tag_head: video_title = title_tag_head.get_text(strip=True).split('- Hypnotube')[0].strip()
        if not video_title or len(video_title) < 3:
            path = urlparse(video_page_url).path
            video_title = os.path.splitext(os.path.basename(path))[0].replace('-', ' ').title()
        if not video_title: video_title = "Untitled_Video_" + str(random.randint(1000, 9999))
        
        best_mp4_url = None
        source_tags = soup.select("video > source[src*='.mp4']")
        if source_tags:
            quality_options = []
            for tag in source_tags:
                src = tag.get("src")
                if not src: continue
                quality = int(tag.get('size', 0))
                quality_options.append({"quality": quality, "url": src})
            
            if quality_options:
                best_option = sorted(quality_options, key=lambda x: x['quality'], reverse=True)[0]
                best_mp4_url = best_option['url']
                
                if not best_mp4_url.startswith("http"):
                    best_mp4_url = urlparse(video_page_url).scheme + ":" + best_mp4_url if best_mp4_url.startswith("//") else BASE_URL + best_mp4_url
                
                print(f"   - Найдено видео '{video_title}'. Выбрано качество: {best_option['quality']}p.")
                print(f"   -> 🔗 Ссылка для скачивания: {best_mp4_url}")
                return best_mp4_url, video_title

        print(f"   - ⚠️ Не удалось найти MP4 ссылку для видео '{video_title}'.")
        return None, video_title
    
    except Exception as e:
        print(f"❌ Неожиданная ошибка при обработке {video_page_url}: {e}")
        return None, "Untitled_Error_Video"
    finally:
        if driver: driver.quit()

def download_video(mp4_url, title, output_folder, session):
    sanitized_title = sanitize_filename(title)
    filepath = os.path.join(output_folder, f"{sanitized_title}.mp4")
    if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
        print(f"   -> ✅ Файл '{sanitized_title}.mp4' уже существует, пропуск.")
        return filepath
    print(f"   -> 📥 Загрузка: {sanitized_title}.mp4")
    try:
        r = session.get(mp4_url, stream=True, headers=REQUESTS_HEADERS, timeout=(30, 300))
        r.raise_for_status()
        with open(filepath, "wb") as f:
            for chunk in r.iter_content(chunk_size=8192):
                f.write(chunk)
        return filepath
    except Exception as e:
        print(f"   -> ❌ Ошибка при загрузке '{sanitized_title}': {e}")
        if os.path.exists(filepath): os.remove(filepath)
        return None

def process_video_pipeline(video_path):
    basename = os.path.basename(video_path)
    current_path = video_path
    temp_files = []
    try:
        if PERFORM_UPSCALE:
            temp_upscaled_path = video_path + ".upscaled.mp4"
            temp_files.append(temp_upscaled_path)
            scale_filter = f"scale=iw*{UPSCALE_FACTOR}:ih*{UPSCALE_FACTOR}:flags={UPSCALE_ALGORITHM}"
            command_upscale = ["ffmpeg", "-i", current_path, "-vf", scale_filter, "-c:a", "copy", "-y", temp_upscaled_path]
            subprocess.run(command_upscale, check=True, capture_output=True, text=True)
            current_path = temp_upscaled_path
        temp_final_path = video_path + ".processed.mp4"
        temp_files.append(temp_final_path)
        audio_filters = f"loudnorm=I={LOUDNESS_TARGET_I}:LRA={LOUDNESS_TARGET_LRA}:tp={LOUDNESS_TARGET_TP},{AUDIO_COMPRESSOR_SETTINGS}"
        command_audio = ["ffmpeg", "-i", current_path, "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-af", audio_filters, "-y", temp_final_path]
        subprocess.run(command_audio, check=True, capture_output=True, text=True)
        os.replace(temp_final_path, video_path)
        return basename, True, "Обработка завершена"
    except subprocess.CalledProcessError as e:
        error_type = "апскейле" if "scale" in str(e.args) else "обработке аудио"
        return basename, False, f"Ошибка FFmpeg при {error_type}: {e.stderr[:200]}..."
    finally:
        for f in temp_files:
            if os.path.exists(f): os.remove(f)

def process_all_downloaded_videos(folder_path):
    print("\n⚙️ Начинается постобработка видео...")
    video_files = glob.glob(os.path.join(folder_path, "*.mp4"))
    if not video_files:
        print("   - Видео для обработки не найдены.")
        return
    total_files = len(video_files)
    print(f"   - Найдено {total_files} видео. Запуск в {MAX_PROCESS_WORKERS} потоков...")
    with ProcessPoolExecutor(max_workers=MAX_PROCESS_WORKERS) as executor:
        futures = [executor.submit(process_video_pipeline, path) for path in video_files]
        for future in as_completed(futures):
            filename, success, message = future.result()
            status = "✅" if success else "❌"
            print(f"   - {status} {filename}: {message}")

def create_zip_archive(folder_to_zip, zip_name):
    print(f"\n📦 Архивирование папки '{os.path.basename(folder_to_zip)}' в '{zip_name}'...")
    try:
        shutil.make_archive(zip_name.replace('.zip', ''), 'zip', folder_to_zip)
        print("✅ Архив успешно создан.")
    except Exception as e:
        print(f"❌ Не удалось создать архив: {e}")

def get_requests_session_with_retries():
    session = requests.Session()
    retry_strategy = Retry(total=3, read=3, connect=3, backoff_factor=1.0, status_forcelist=(429, 500, 502, 503, 504))
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    if SESSION_ID_COOKIE:
        session.cookies.set('PHPSESSID', SESSION_ID_COOKIE, domain='.hypnotube.com')
    return session

def main():
    if not os.path.exists(PLAYLIST_FILE):
        with open(PLAYLIST_FILE, "w") as f: f.write("# Вставьте сюда ссылки на плейлисты, по одной на строку\n")
        print(f"💡 Создан файл '{PLAYLIST_FILE}'. Пожалуйста, добавьте в него ссылки.")
        return
    if not shutil.which("ffmpeg"):
        print("❌ ОШИБКА: FFmpeg не найден. Убедитесь, что он установлен и доступен в PATH.")
        return
    with open(PLAYLIST_FILE, "r") as f:
        playlist_urls = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if not playlist_urls:
        print(f"⚠️ Файл '{PLAYLIST_FILE}' пуст.")
        return
    os.makedirs(BASE_OUTPUT_FOLDER, exist_ok=True)
    requests_session = get_requests_session_with_retries()
    
    auth_failed = False
    for url in playlist_urls:
        if auth_failed: break
        print(f"\n{'='*20} НАЧАЛО РАБОТЫ С ПЛЕЙЛИСТОМ: {url} {'='*20}")
        playlist_title, video_page_links = get_playlist_title_and_video_links(url)
        if not video_page_links:
            print(f"❌ Не удалось получить ссылки на видео. Переход к следующему.")
            continue
        sanitized_playlist_title = sanitize_filename(playlist_title)
        playlist_folder = os.path.join(BASE_OUTPUT_FOLDER, sanitized_playlist_title)
        os.makedirs(playlist_folder, exist_ok=True)
        videos_to_download = []
        for page_url in video_page_links:
            mp4_url, video_title = extract_mp4_link_and_title(page_url)
            if mp4_url is None and video_title is None:
                 print("   -> 🛑 Прерываем обработку из-за ошибки авторизации.")
                 auth_failed = True
                 break
            if mp4_url and video_title:
                videos_to_download.append({"url": mp4_url, "title": video_title})
            time.sleep(1)
        
        if auth_failed: continue
            
        print(f"\n📥 Начинается загрузка {len(videos_to_download)} видео в {MAX_DOWNLOAD_THREADS} потоков...")
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_THREADS) as executor:
            futures = [executor.submit(download_video, video['url'], video['title'], playlist_folder, requests_session) for video in videos_to_download]
            for future in as_completed(futures):
                future.result()
        
        # *** ИСПРАВЛЕНИЕ ОШИБКИ ***
        process_all_downloaded_videos(playlist_folder) 
        zip_path = os.path.join(BASE_OUTPUT_FOLDER, f"{sanitized_playlist_title}.zip")
        create_zip_archive(playlist_folder, zip_path)
        # *** КОНЕЦ ИСПРАВЛЕНИЯ ***

        print(f"\n{'='*20} РАБОТА С ПЛЕЙЛИСТОМ '{playlist_title}' ЗАВЕРШЕНА {'='*20}")
    
    print("\n\n🎉🎉🎉 ВСЕ ЗАДАЧИ ВЫПОЛНЕНЫ! 🎉🎉🎉")

if __name__ == "__main__":
    main()